"""Fakes for every port, plus fixture loading helpers."""

from __future__ import annotations

import dataclasses
import io
import queue
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from sidetap_live.graph import PLAYBACK_STREAM, SINK, PwGraph, PwLink, parse_graph
from sidetap_live.ports import LinkResult, LoopbackSpec
from sidetap_live.types import TARGET_RATE

FIXTURES = Path(__file__).parent / "fixtures"


def load_graph(name: str) -> PwGraph:
    return parse_graph((FIXTURES / name).read_text())


def playing_on(graph: PwGraph, sink_name: str | None, app: str = "zoom") -> PwGraph:
    """The same graph with the app's stream linked to another sink, or none.

    Channels pair by index, as WirePlumber links them. The stream's links to
    anything that is not a sink are kept.
    """
    stream = graph.find(app, PLAYBACK_STREAM)
    by_id = {n.id: n for n in graph.nodes}
    kept = tuple(
        link
        for link in graph.links
        if link.output_node_id != stream.id
        or getattr(by_id.get(link.input_node_id), "media_class", None) != SINK
    )
    if sink_name is None:
        return dataclasses.replace(graph, links=kept)
    sink = graph.node_by_name(sink_name)
    inputs = graph.ports_of(sink.id, "in")
    next_id = max((link.id for link in graph.links), default=900) + 1
    added = tuple(
        PwLink(next_id + i, stream.id, out.id, sink.id, inputs[min(i, len(inputs) - 1)].id)
        for i, out in enumerate(graph.ports_of(stream.id, "out"))
    )
    return dataclasses.replace(graph, links=kept + added)


class LiveLinks:
    """GraphSource, Linker and Unlinker over ONE set of links.

    pw-dump and pw-link talk to the same daemon, so a link made or removed is
    in the next snapshot. Unlinking an absent link fails, as `pw-link -d` does
    ("No such file or directory"); linking a present one is ALREADY_LINKED.
    """

    def __init__(self, graph: PwGraph, fail_links: int = 0):
        self._graph = graph
        self.pairs: set[tuple[int, int]] = {
            (link.output_port_id, link.input_port_id) for link in graph.links
        }
        # The next this many link() calls fail, as a pw-link timeout would.
        self.fail_links = fail_links

    def snapshot(self) -> PwGraph:
        node_of = {p.id: p.node_id for p in self._graph.ports}
        links = tuple(
            PwLink(9000 + i, node_of[src], src, node_of[dst], dst)
            for i, (src, dst) in enumerate(sorted(self.pairs))
        )
        return dataclasses.replace(self._graph, links=links)

    def link(self, src_port: int, dst_port: int) -> LinkResult:
        if self.fail_links:
            self.fail_links -= 1
            return LinkResult.FAILED
        if (src_port, dst_port) in self.pairs:
            return LinkResult.ALREADY_LINKED
        self.pairs.add((src_port, dst_port))
        return LinkResult.LINKED

    def unlink(self, src_port: int, dst_port: int) -> LinkResult:
        if (src_port, dst_port) not in self.pairs:
            return LinkResult.FAILED
        self.pairs.discard((src_port, dst_port))
        return LinkResult.LINKED


class ChunkedBytesIO(io.BytesIO):
    """A stream whose read() returns short, the way a real pipe does."""

    def __init__(self, data: bytes, max_read: int):
        super().__init__(data)
        self._max_read = max_read

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            return super().read()
        return super().read(min(size, self._max_read))


class FakeProcess:
    def __init__(self, stdout: io.BytesIO, stderr: str = ""):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = None
        self.terminated = False

    @property
    def stdout(self) -> io.BytesIO:
        return self._stdout

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        # A real SIGTERM exit is -15, not a clean 0. Recorder.failure() treats
        # 0 as "no failure", so reporting 0 here would hide the shutdown path
        # capture.py's dead-track check depends on.
        self.returncode = -15

    def stderr_text(self) -> str:
        return self._stderr

    def die(self, returncode: int = 1, stderr: str = "boom") -> None:
        """Simulate the process exiting mid-session."""
        self.returncode = returncode
        self._stderr = stderr


class FakeWritableProcess:
    """Stands in for pw-cat --playback and pw-loopback."""

    def __init__(self, stderr: str = ""):
        self._stdin = io.BytesIO()
        self._stderr = stderr
        self.returncode: int | None = None
        self.terminated = False

    @property
    def stdin(self) -> io.BytesIO:
        return self._stdin

    @property
    def written(self) -> bytes:
        return self._stdin.getvalue()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def stderr_text(self) -> str:
        return self._stderr

    def die(self, returncode: int = 1, stderr: str = "boom") -> None:
        self.returncode = returncode
        self._stderr = stderr


class FakeLauncher:
    def __init__(self, script: bytes = b"", max_read: int | None = None):
        self.script = script
        self.max_read = max_read
        self.calls: list[list[str]] = []
        self.writer_calls: list[list[str]] = []
        self.processes: list[FakeProcess] = []
        self.writers: list[FakeWritableProcess] = []

    def spawn(self, argv: Sequence[str]) -> FakeProcess:
        self.calls.append(list(argv))
        stream: io.BytesIO
        if self.max_read is None:
            stream = io.BytesIO(self.script)
        else:
            stream = ChunkedBytesIO(self.script, self.max_read)
        process = FakeProcess(stream)
        self.processes.append(process)
        return process

    def spawn_writer(self, argv: Sequence[str]) -> FakeWritableProcess:
        self.writer_calls.append(list(argv))
        writer = FakeWritableProcess()
        self.writers.append(writer)
        return writer


class FakeGraphSource:
    """Returns each snapshot in turn, then repeats the last one forever."""

    def __init__(self, *snapshots: PwGraph):
        assert snapshots, "give FakeGraphSource at least one snapshot"
        self._queue = list(snapshots)
        self.calls = 0

    def snapshot(self) -> PwGraph:
        self.calls += 1
        if len(self._queue) > 1:
            return self._queue.pop(0)
        return self._queue[0]


class FakeLinker:
    """Serves as both Linker and Unlinker."""

    def __init__(self, result: LinkResult = LinkResult.LINKED):
        self.result = result
        self.links: list[tuple[int, int]] = []
        self.unlinks: list[tuple[int, int]] = []

    def link(self, src_port: int, dst_port: int) -> LinkResult:
        self.links.append((src_port, dst_port))
        return self.result

    def unlink(self, src_port: int, dst_port: int) -> LinkResult:
        self.unlinks.append((src_port, dst_port))
        return self.result


class FakeVolumeControl:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[int, float]] = []

    def set_volume(self, object_id: int, fraction: float) -> bool:
        self.calls.append((object_id, fraction))
        return self.ok

    @property
    def current(self) -> float | None:
        return self.calls[-1][1] if self.calls else None


class FakeLoopbackFactory:
    def __init__(self):
        self.specs: list[LoopbackSpec] = []
        self.processes: list[FakeWritableProcess] = []

    def create(self, spec: LoopbackSpec) -> FakeWritableProcess:
        self.specs.append(spec)
        process = FakeWritableProcess()
        self.processes.append(process)
        return process


class FakeAudioSink:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, pcm: bytes) -> None:
        self.chunks.append(pcm)

    def close(self) -> None:
        self.closed = True

    @property
    def written(self) -> bytes:
        return b"".join(self.chunks)


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start
        self.slept: list[float] = []
        self.waited: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def wait(self, event: threading.Event, timeout: float) -> bool:
        """Fake `Clock.wait`.

        If `event` is already set, return True at once without advancing the
        fake clock or recording a wait - mirroring the real Clock's prompt
        wakeup. Otherwise behaves like `sleep`: advances the fake clock by
        `timeout` and returns False, as if the interval elapsed with `event`
        still unset.
        """
        if event.is_set():
            return True
        self.waited.append(timeout)
        self.now += timeout
        return False

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def fake_linker() -> FakeLinker:
    return FakeLinker()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_volume() -> FakeVolumeControl:
    return FakeVolumeControl()


@pytest.fixture
def idle_graph() -> PwGraph:
    return load_graph("pw_dump_idle.json")


@pytest.fixture
def zoom_graph() -> PwGraph:
    return load_graph("pw_dump_zoom_active.json")


@pytest.fixture
def routing_graph() -> PwGraph:
    return load_graph("pw_dump_routing.json")


class FakeSession:
    """Scriptable InterpreterSession.

    Events queued before or during the test are yielded by events(); close()
    makes the iterator return, which is what lets a rotation test assert the
    old receive thread actually exits.
    """

    def __init__(self, *events):
        self.sent = bytearray()
        self.closed = False
        self._events: queue.Queue = queue.Queue()
        for event in events:
            self._events.put(event)

    def send(self, pcm: bytes) -> None:
        assert not self.closed, "sent to a closed session"
        self.sent.extend(pcm)

    def emit(self, event) -> None:
        """Push an event from the test, mid-run."""
        self._events.put(event)

    def events(self):
        while True:
            try:
                yield self._events.get(timeout=0.02)
            except queue.Empty:
                if self.closed:
                    return

    def close(self) -> None:
        self.closed = True

    @property
    def sent_s(self) -> float:
        return len(self.sent) / (TARGET_RATE * 2)


class FakeSessionFactory:
    def __init__(self):
        self.sessions: list[FakeSession] = []
        self.opens: list[tuple[str, bool, str | None]] = []

    def open(self, target_lang: str, *, echo: bool, handle: str | None = None):
        self.opens.append((target_lang, echo, handle))
        session = FakeSession()
        self.sessions.append(session)
        return session


@pytest.fixture
def fake_sessions() -> FakeSessionFactory:
    return FakeSessionFactory()
