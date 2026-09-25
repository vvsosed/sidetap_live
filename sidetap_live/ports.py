"""The boundaries between this program and the outside world.

Every Protocol here has exactly one real implementation and one fake in
tests/conftest.py. Nothing else in the package touches a subprocess, a socket
or the wall clock.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO, Protocol, runtime_checkable

from .graph import PwGraph
from .types import SessionEvent


class LinkResult(Enum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    FAILED = "failed"


@dataclass(frozen=True)
class LoopbackSpec:
    """Arguments for one pw-loopback process, as pw-loopback property maps.

    A value type so routing.py can be tested without PipeWire.
    """

    capture_props: tuple[tuple[str, str], ...]
    playback_props: tuple[tuple[str, str], ...]


@runtime_checkable
class GraphSource(Protocol):
    def snapshot(self) -> PwGraph: ...


@runtime_checkable
class ManagedProcess(Protocol):
    """A process we read from."""

    @property
    def stdout(self) -> BinaryIO: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def stderr_text(self) -> str: ...


@runtime_checkable
class WritableProcess(Protocol):
    """A process we write to. pw-cat --playback and pw-loopback.

    Separate from ManagedProcess so a stdin-less fake cannot satisfy a
    consumer that needs stdin.
    """

    @property
    def stdin(self) -> BinaryIO: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def stderr_text(self) -> str: ...


@runtime_checkable
class ProcessLauncher(Protocol):
    def spawn(self, argv: Sequence[str]) -> ManagedProcess: ...

    def spawn_writer(self, argv: Sequence[str]) -> WritableProcess: ...


@runtime_checkable
class Linker(Protocol):
    def link(self, src_port: int, dst_port: int) -> LinkResult: ...


@runtime_checkable
class Unlinker(Protocol):
    def unlink(self, src_port: int, dst_port: int) -> LinkResult: ...


@runtime_checkable
class VolumeControl(Protocol):
    def set_volume(self, object_id: int, fraction: float) -> bool:
        """`object_id` is the PipeWire object.id, NOT object.serial.

        wpctl resolves against the id and answers "Object not found" for a
        serial. Durable references (the journal, the tap's dedup keys) still
        use serials, because ids get recycled.
        """
        ...


@runtime_checkable
class LoopbackFactory(Protocol):
    def create(self, spec: LoopbackSpec) -> WritableProcess: ...


@runtime_checkable
class AudioSink(Protocol):
    def write(self, pcm: bytes) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def wait(self, event: threading.Event, timeout: float) -> bool:
        """Block until `event` is set or `timeout` elapses.

        Returns whether `event` was set, like threading.Event.wait. Unlike
        `sleep` it wakes as soon as `event` is set, so a shutdown can
        interrupt a poll loop promptly.
        """
        ...


@runtime_checkable
class InterpreterSession(Protocol):
    """One live speech-to-speech translation session.

    Synchronous on purpose: the audio layer is thread-based, so the SDK's
    asyncio is confined inside live.py rather than leaking into consumers.
    """

    def send(self, pcm: bytes) -> None:
        """Queue 100 ms of 16 kHz s16 mono. Never blocks the caller."""
        ...

    def events(self) -> Iterator[SessionEvent]:
        """Yield until the session ends. Returns when it has."""
        ...

    def close(self) -> None:
        """Tear down. Makes `events()` return."""
        ...


@runtime_checkable
class SessionFactory(Protocol):
    def open(
        self, target_lang: str, *, echo: bool, handle: str | None = None
    ) -> InterpreterSession:
        """`handle` resumes a previous session; None starts a fresh one."""
        ...
