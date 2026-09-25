"""Own the duck path, and be able to give the graph back.

Engaging moves the application's links to its output device into a loopback
whose volume playout controls, and the loopback plays into that same device.
Restoration is a correctness requirement: without it a crash leaves the user
with no call audio.

Only links read from the graph are moved, never ones inferred from the default
sink, because restore() recreates exactly what was journalled.

Every change is journalled to disk BEFORE it is made, so a kill -9 between the
two is recoverable. Entries name nodes by object.serial and ports by name,
never by port id, which PipeWire recycles.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .graph import PLAYBACK_STREAM, SINK, PwGraph, PwNode, PwPort
from .ports import GraphSource, Linker, LinkResult, LoopbackFactory, LoopbackSpec, Unlinker
from .tap import GRAPH_ERROR_WARN_AFTER

log = logging.getLogger(__name__)

# Matches AppTap's cadence: both watch for late-created streams.
POLL_INTERVAL_S = 2.0

# NOT shared with sidetap, unlike the virtmic constants below. The duck lives
# only as long as this process; a shared name would let either program's
# `doctor --repair` tear down the other's live duck.
DUCK_NODE = "sidetap_live_duck"
VIRTMIC_SINK = "sidetap_tts_sink"
VIRTMIC_SOURCE = "sidetap_virtmic"
VIRTMIC_DESCRIPTION = "sidetap Virtual Mic"
# Different from VIRTMIC_DESCRIPTION so the sink we write into cannot be
# mistaken for the microphone the messenger should select; picking the wrong
# one leaves the remote party hearing nothing, with no error.
VIRTMIC_CAPTURE_DESCRIPTION = "sidetap TTS input"

VIRTMIC_CONFIG = f"""\
# Installed by `sidetap doctor`.
#
# Presents two nodes: a sink sidetap writes synthesised speech into, and a
# source your messenger sees as an ordinary microphone.
#
# This is a permanent config file rather than something sidetap creates at
# runtime, and that is deliberate. A runtime-created device has a different
# identity every session, so Zoom and Viber lose the saved selection and fall
# back to your real microphone - silently, with the remote party hearing your
# untranslated voice.
#
# Apply with: systemctl --user restart pipewire pipewire-pulse

context.modules = [
  {{ name = libpipewire-module-loopback
    args = {{
      node.description = "{VIRTMIC_DESCRIPTION}"
      capture.props = {{
        node.name       = "{VIRTMIC_SINK}"
        node.description = "{VIRTMIC_CAPTURE_DESCRIPTION}"
        media.class     = Audio/Sink
        audio.position  = [ MONO ]
        audio.rate      = 48000
      }}
      playback.props = {{
        node.name        = "{VIRTMIC_SOURCE}"
        node.description = "{VIRTMIC_DESCRIPTION}"
        media.class      = Audio/Source
        audio.position   = [ MONO ]
        node.passive     = false
      }}
    }}
  }}
]
"""

VIRTMIC_CONFIG_PATH = Path.home() / ".config/pipewire/pipewire.conf.d/90-sidetap-mic.conf"

# NOT shared with sidetap: the journal describes links this process made, and
# each program repairs only its own. The virtual mic is shared because a
# messenger's saved device selection is tied to the node name.
JOURNAL_PATH = Path.home() / ".local/state/sidetap_live/routing-journal.json"


@dataclass(frozen=True)
class LinkRef:
    """One link, named durably.

    Serial plus port name, never port id: PipeWire recycles ids.
    """

    src_serial: int
    src_port: str
    dst_serial: int
    dst_port: str

    def to_dict(self) -> dict:
        return {
            "src_serial": self.src_serial,
            "src_port": self.src_port,
            "dst_serial": self.dst_serial,
            "dst_port": self.dst_port,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LinkRef:
        return cls(
            src_serial=int(data["src_serial"]),
            src_port=str(data["src_port"]),
            dst_serial=int(data["dst_serial"]),
            dst_port=str(data["dst_port"]),
        )


@dataclass(frozen=True)
class Journal:
    broken: tuple[LinkRef, ...] = ()
    made: tuple[LinkRef, ...] = ()

    def save(self, path: Path) -> None:
        """Write atomically: a temp file in the same directory, then os.replace().

        The journal is rewritten whole on every change, so a torn write would
        erase the record of every link already applied, not just the new one.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "broken": [r.to_dict() for r in self.broken],
            "made": [r.to_dict() for r in self.made],
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            # fsync the file before the rename and the directory after it.
            # os.replace() alone survives only a kill -9; after a power loss
            # the journal could revert while the graph stays rewired, which
            # doctor --repair cannot recover from.
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            # Do not leave a stray .tmp behind.
            tmp.unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> Journal:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # No session has ever journalled here.
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            # A journal that exists but cannot be read may be the only
            # record of links still live in the graph. Start anyway, but
            # loudly.
            log.error(
                "routing journal at %s is unreadable (%s) - proceeding as if "
                "there is nothing to restore, but the audio graph may still "
                "be modified from a previous session. Run `sidetap-live doctor "
                "--repair` and check manually if call audio sounds wrong.",
                path,
                exc,
            )
            return cls()
        return cls(
            broken=tuple(LinkRef.from_dict(d) for d in payload.get("broken", ())),
            made=tuple(LinkRef.from_dict(d) for d in payload.get("made", ())),
        )

    def is_empty(self) -> bool:
        return not self.broken and not self.made


def resolve(graph: PwGraph, ref: LinkRef) -> tuple[int, int] | None:
    """LinkRef -> live port ids, or None if either end is gone."""
    by_serial = {n.serial: n for n in graph.nodes}
    src_node = by_serial.get(ref.src_serial)
    dst_node = by_serial.get(ref.dst_serial)
    if src_node is None or dst_node is None:
        return None

    src = next(
        (p for p in graph.ports_of(src_node.id, "out") if p.name == ref.src_port), None
    )
    dst = next(
        (p for p in graph.ports_of(dst_node.id, "in") if p.name == ref.dst_port), None
    )
    # No fallback to a port of the other direction: that would hand pw-link a
    # backwards port and journal it as the requested link. None makes
    # restore() and repair() report the ref, which the user can act on.
    if src is None or dst is None:
        return None
    return (src.id, dst.id)


def sink_links(graph: PwGraph, stream: PwNode) -> tuple[tuple[PwPort, PwPort, PwNode], ...]:
    """(stream port, sink port, sink) for each link from `stream` to an output device.

    An output device is an Audio/Sink someone listens to, so not the duck or
    the virtual mic's sink: the IN translation follows the call's device and
    must never land in either. Links to anything else, such as the tap into
    our capture node, are not playback and are never moved.
    """
    outputs = {p.id: p for p in graph.ports_of(stream.id, "out")}
    inputs = {p.id: p for p in graph.ports if p.direction == "in"}
    nodes = {n.id: n for n in graph.nodes}
    found = []
    for link in graph.links_from(stream.id):
        out_port = outputs.get(link.output_port_id)
        in_port = inputs.get(link.input_port_id)
        sink = nodes.get(link.input_node_id)
        if out_port is None or in_port is None or sink is None:
            continue
        if sink.media_class == SINK and sink.name not in (DUCK_NODE, VIRTMIC_SINK):
            found.append((out_port, in_port, sink))
    return tuple(sorted(found, key=lambda f: (f[0].name, f[1].name)))


def call_sink(graph: PwGraph, app_pattern: str) -> PwNode | None:
    """The output device a matching stream already plays on, if any."""
    for stream in graph.by_class(PLAYBACK_STREAM):
        if stream.matches(app_pattern):
            for _, _, sink in sink_links(graph, stream):
                return sink
    return None


def _device(node: PwNode | None) -> str:
    # The description is what a messenger's speaker menu shows.
    if node is None:
        return "the default output"
    return repr(node.description or node.name)


def duck_loopback_spec(target_sink: str) -> LoopbackSpec:
    """A sink we own, playing into the call's device at a volume we control."""
    return LoopbackSpec(
        capture_props=(
            ("node.name", DUCK_NODE),
            ("node.description", "sidetap_live duck"),
            ("media.class", "Audio/Sink"),
            ("audio.position", "[ FL FR ]"),
        ),
        playback_props=(
            ("node.name", f"{DUCK_NODE}_out"),
            ("target.object", target_sink),
            ("node.passive", "false"),
        ),
    )


class Router:
    def __init__(
        self,
        graph: GraphSource,
        linker: Linker,
        unlinker: Unlinker,
        loopbacks: LoopbackFactory,
        journal_path: Path = JOURNAL_PATH,
    ):
        self._graph = graph
        self._linker = linker
        self._unlinker = unlinker
        self._loopbacks = loopbacks
        self._journal_path = journal_path
        self._loopback = None
        # Serials already routed through the duck. Serial, not id, because ids
        # are recycled.
        self._routed: set[int] = set()
        # Serials whose move has begun. For these, no sink link means we
        # removed it, not that WirePlumber has yet to link the stream, so a
        # failed link into the duck is still retried.
        self._claimed: set[int] = set()
        # Stream serial -> the sink serials last reported for it, so a stream
        # on the wrong device is reported once, not every poll.
        self._elsewhere: dict[int, frozenset[int]] = {}
        self._app_pattern: str | None = None
        # The device the duck plays into, and the IN translation with it.
        # Set by engage().
        self.target_sink: PwNode | None = None
        # Held across whole method bodies of _route_locked()/restore()/
        # repair(), so the watcher's poll cannot re-break a link restore()
        # just fixed. These run rarely, so contention does not matter.
        self._lock = threading.Lock()
        # BOTH: the serial is the durable identifier the journal records; the
        # id is what wpctl resolves for the duck volume. Conflating them makes
        # the duck silently never close.
        self.duck_serial: int | None = None
        self.duck_id: int | None = None

    @property
    def has_routed(self) -> bool:
        """Has any application stream actually been rewired through the duck?

        This arms the IN direction's no-audio watch. Before then the app is
        simply not playing yet, and alarming on that would train the user to
        ignore the warning.
        """
        return bool(self._routed)

    def engage(self, app_pattern: str) -> None:
        with self._lock:
            if self._loopback is not None:
                # Otherwise the old loopback leaks and a second node named
                # DUCK_NODE makes node_by_name() lookups ambiguous.
                raise RuntimeError(
                    "Router.engage() called while already engaged - call "
                    "restore() before engaging again"
                )
            self._app_pattern = app_pattern
            # One snapshot, taken BEFORE the loopback is spawned: it supplies
            # the duck's target.object. It is reused rather than re-read,
            # because the freshly forked loopback may not have registered
            # yet; engage() then finds no duck and the next poll_once()
            # completes the routing. Never guess at duck ports and journal a
            # link that was not made.
            snapshot = self._graph.snapshot()
            # The device the call already plays on, so the duck and the
            # translation reach the ears the original was reaching. The
            # default sink only when nothing is playing yet; a stream that
            # then appears elsewhere is reported, not moved.
            self.target_sink = call_sink(snapshot, app_pattern) or snapshot.node_by_name(
                snapshot.default_sink or ""
            )
            target_sink_name = self.target_sink.name if self.target_sink else ""

            self._loopback = self._loopbacks.create(duck_loopback_spec(target_sink_name))

            self._route_locked(snapshot, app_pattern, initial=True)

    def poll_once(self) -> int:
        """Route any matching stream that is not routed yet. Returns how many.

        Applications create and re-create streams late. WirePlumber would
        connect a stream that appears after engage() straight to the speakers:
        unducked, unjournalled, and invisible to restore().
        """
        if self._app_pattern is None:
            return 0
        with self._lock:
            snapshot = self._graph.snapshot()
            return self._route_locked(snapshot, self._app_pattern, initial=False)

    def _route_locked(self, snapshot: PwGraph, app_pattern: str, *, initial: bool) -> int:
        """Caller must hold self._lock."""
        duck = snapshot.node_by_name(DUCK_NODE)
        # Refreshed on every call: engage()'s snapshot can predate the duck,
        # and a duck_id left None means the duck can never close.
        if duck is not None:
            self.duck_serial = duck.serial
            self.duck_id = duck.id
        elif initial:
            self.duck_serial = None
            self.duck_id = None
        if duck is None:
            return 0
        duck_inputs = snapshot.ports_of(duck.id, "in")
        # Defer until the duck has ports, not just a node: PipeWire announces
        # a node before its ports. Otherwise the unlink below would cut the
        # call from its device and join it to nothing, and since every unlink
        # succeeded the stream would be marked routed and never retried.
        if not duck_inputs:
            return 0

        target = self.target_sink
        broken: list[LinkRef] = []
        made: list[LinkRef] = []
        candidates: set[int] = set()

        for stream in snapshot.by_class(PLAYBACK_STREAM):
            if not stream.matches(app_pattern):
                continue
            if stream.serial in self._routed:
                continue
            outputs = snapshot.ports_of(stream.id, "out")
            if not outputs:
                # Ports not registered yet: nothing can move, so it is not done.
                continue
            # The links that exist, not ones inferred from the default sink:
            # restore() recreates exactly what is journalled.
            current = sink_links(snapshot, stream)
            if not current and stream.serial not in self._claimed:
                # Not linked to a device yet. Moving it now would leave
                # nothing to restore and it would join the duck AND whatever
                # WirePlumber links it to next; retry on the next poll.
                continue
            elsewhere = {
                sink.serial: sink
                for _, _, sink in current
                if target is None or sink.serial != target.serial
            }
            if elsewhere:
                # The duck plays on another device, so moving this stream
                # would take the call off the device the user is listening
                # on. Left untouched and audible instead.
                self._report_elsewhere(stream, tuple(elsewhere.values()))
                continue
            for out_port, in_port, sink in current:
                broken.append(
                    LinkRef(stream.serial, out_port.name, sink.serial, in_port.name)
                )
            for index, out_port in enumerate(outputs):
                # Into the duck paired by index, as WirePlumber links
                # (FL->FL, FR->FR).
                #
                # LIMIT: ports_of() sorts by name, which matches channel
                # order only for mono and stereo. A 5.1 sink would cross
                # channels; fixing that needs audio.channel on PwPort.
                in_port = duck_inputs[min(index, len(duck_inputs) - 1)]
                made.append(LinkRef(stream.serial, out_port.name, duck.serial, in_port.name))
            candidates.add(stream.serial)
            log.info("routing %s (serial=%s) through the duck", stream.label, stream.serial)

        if not candidates:
            return 0

        # Durable BEFORE the graph is touched.
        journal = Journal.load(self._journal_path)
        # Only what is not already recorded: a failing stream is retried on
        # every poll, and appending blindly would grow the journal without
        # bound, each duplicate rewritten under the lock and replayed on
        # restore.
        known_broken = set(journal.broken)
        known_made = set(journal.made)
        new_broken = tuple(ref for ref in broken if ref not in known_broken)
        new_made = tuple(ref for ref in made if ref not in known_made)
        if new_broken or new_made:
            Journal(
                broken=journal.broken + new_broken,
                made=journal.made + new_made,
            ).save(self._journal_path)
        self._claimed |= candidates

        # A FAILED apply is not done: leaving the serial out of self._routed
        # makes the next poll retry it. Otherwise a failed link into the duck
        # after a successful unlink would leave the call silent for good.
        failed: set[int] = set()
        for ref in broken:
            if self._apply(snapshot, ref, link=False) is LinkResult.FAILED:
                failed.add(ref.src_serial)
        for ref in made:
            if self._apply(snapshot, ref, link=True) is LinkResult.FAILED:
                failed.add(ref.src_serial)

        succeeded = candidates - failed
        self._routed |= succeeded
        if failed:
            log.warning(
                "could not fully route %r through %s (serials=%s) - will retry "
                "on the next poll; the original may be briefly audible over "
                "the translation until then",
                app_pattern,
                DUCK_NODE,
                sorted(failed),
            )

        if initial and succeeded:
            log.info(
                "routed %r through %s (%d links broken, %d made)",
                app_pattern,
                DUCK_NODE,
                len(broken),
                len(made),
            )
        return len(succeeded)

    def _report_elsewhere(self, stream: PwNode, sinks: tuple[PwNode, ...]) -> None:
        """ERROR once per stream and device: only the user can fix it.

        The stream is re-checked every poll regardless, so moving the app to
        the duck's device gets it routed with no restart.
        """
        key = frozenset(sink.serial for sink in sinks)
        if self._elsewhere.get(stream.serial) == key:
            return
        self._elsewhere[stream.serial] = key
        target = _device(self.target_sink)
        log.error(
            "%s plays to %s but the interpreter was set up on %s, so it is "
            "left untouched and will not be ducked. Set its speaker to %s, or "
            "restart sidetap-live during the call.",
            stream.label,
            ", ".join(_device(sink) for sink in sinks),
            target,
            target,
        )

    def run(self, stop: threading.Event, interval: float = POLL_INTERVAL_S) -> None:
        """Re-scan until stopped. A transient graph read must not kill this."""
        consecutive_errors = 0
        while not stop.is_set():
            try:
                self.poll_once()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors == GRAPH_ERROR_WARN_AFTER:
                    # As in AppTap.run(): repeated failure means new streams
                    # are no longer ducked, so warn once.
                    log.warning(
                        "routing watcher failing repeatedly (%s) - new streams "
                        "matching %r are no longer being routed through %s",
                        exc,
                        self._app_pattern,
                        DUCK_NODE,
                    )
                else:
                    log.debug("routing watcher: %s", exc)
            stop.wait(interval)

    def restore(self) -> None:
        with self._lock:
            self._restore_locked()

    def _restore_locked(self) -> None:
        """Caller must hold self._lock."""
        try:
            self._replay_locked()
        finally:
            # Unconditional: engage() creates the duck even if no stream is
            # ever routed and the journal stays empty. Skipping this would
            # orphan a pw-loopback that no later run can find.
            if self._loopback is not None:
                self._loopback.terminate()
                self._loopback = None

    def _replay_locked(self) -> None:
        journal = Journal.load(self._journal_path)
        if journal.is_empty():
            return
        snapshot = self._graph.snapshot()
        failed = False
        for ref in journal.made:
            if self._apply(snapshot, ref, link=False) is LinkResult.FAILED:
                failed = True
        for ref in journal.broken:
            if self._apply(snapshot, ref, link=True) is LinkResult.FAILED:
                failed = True

        if failed:
            # Keep the journal: it is the only record that can repair the
            # graph later.
            log.error(
                "could not fully restore the audio graph - at least one link "
                "failed to apply. Keeping the routing journal so the next "
                "repair can retry; run `sidetap-live doctor --repair`."
            )
        else:
            Journal().save(self._journal_path)

    def repair(self) -> bool:
        """Replay a journal left behind by a session that died.

        Returns True if there was one. Idempotent: entries whose nodes are
        gone are skipped, and the journal is cleared only once every apply in
        the replay succeeded - see restore().
        """
        with self._lock:
            # pw-loopback runs in its own session, so it survives a kill -9 of
            # this process, and the journal records links, not PIDs. A duck
            # present before engage() can only be such a leftover; say so.
            snapshot = self._graph.snapshot()
            duck = snapshot.node_by_name(DUCK_NODE)
            if duck is not None:
                log.warning(
                    "found an existing %s node (id=%s) from a previous "
                    "session - its pw-loopback process may still be running "
                    "with nothing able to stop it automatically. If call "
                    "audio still sounds wrong after this repair, stop it by "
                    "hand: pkill -f 'pw-loopback.*%s'",
                    DUCK_NODE,
                    duck.id,
                    DUCK_NODE,
                )

            journal = Journal.load(self._journal_path)
            if journal.is_empty():
                return False
            log.warning(
                "found a routing journal from a previous session; repairing the graph"
            )
            self._restore_locked()
            return True

    def _apply(self, snapshot: PwGraph, ref: LinkRef, *, link: bool) -> LinkResult:
        ports = resolve(snapshot, ref)
        if ports is None:
            # The application exited, so there is nothing to restore. Report
            # ALREADY_LINKED ("nothing needed doing"), not FAILED, so a stale
            # entry does not keep the journal alive.
            log.debug("skipping stale link %s", ref)
            return LinkResult.ALREADY_LINKED
        src, dst = ports
        if link:
            return self._linker.link(src, dst)
        else:
            return self._unlinker.unlink(src, dst)
