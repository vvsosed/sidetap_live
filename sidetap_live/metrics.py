"""Live pipeline state, written by worker threads and read by the UI.

The dependency runs one way: pipeline threads write here and the TUI polls
snapshot(), so --no-tui and the headless tests share one code path.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

from .types import Direction, SessionState

# Characters of live transcription kept per stream for the dashboard. The
# model emits no turn boundary, only fragments of a few words, so the pane
# shows a rolling tail; the transcript keeps the full record.
LIVE_TEXT_CHARS = 240


class Health(Enum):
    OK = "ok"
    RETRYING = "retrying"
    FAILED = "failed"


@dataclass
class DirectionState:
    # Live text from the two transcription streams. They drift relative to
    # each other by design; the TUI shows both rather than pairing them.
    source: str = ""
    target: str = ""

    # The headline number: translated audio queued but not yet played.
    backlog_s: float = 0.0
    # Speech onset to the first output chunk of that stretch.
    offset_s: float = 0.0

    # Seconds of translated audio the lag cap threw away.
    dropped_s: float = 0.0
    capture_dropped: int = 0
    dead_air: bool = False
    no_audio: bool = False

    session_state: SessionState = SessionState.SUSPENDED
    rotations: int = 0
    forced_rotations: int = 0
    replayed_s: float = 0.0

    session: Health = Health.OK


@dataclass
class Snapshot:
    directions: dict[Direction, DirectionState]
    cost_usd: float = 0.0
    bypassed: bool = False
    # Separate from bypass, though both suppress OUT playout: mute pressed
    # while bypassed changes what you come back to, so the TUI must know which
    # the user asked for.
    muted_out: bool = False
    # Session-wide: percentage of wall clock where BOTH tracks carry speech.
    # None when it cannot be measured.
    overlap_pct: float | None = 0.0


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._states = {d: DirectionState() for d in Direction}
        self._cost_usd = 0.0
        self._bypassed = False
        self._muted_out = False
        self._overlap_pct = 0.0

    def append_text(self, direction: Direction, *, source: str | None = None,
                    target: str | None = None) -> None:
        """Add a transcription fragment, keeping a rolling tail.

        Append, not set: each event is only a few words. Fragments carry
        their own leading space, so they join with no separator.
        """
        with self._lock:
            state = self._states[direction]
            if source is not None:
                state.source = (state.source + source)[-LIVE_TEXT_CHARS:]
            if target is not None:
                state.target = (state.target + target)[-LIVE_TEXT_CHARS:]

    def set_backlog_s(self, direction: Direction, seconds: float) -> None:
        with self._lock:
            self._states[direction].backlog_s = seconds

    def set_dropped_s(self, direction: Direction, seconds: float) -> None:
        """Seconds of translated audio the lag cap threw away, cumulative.

        Distinct from `capture_dropped`: this overflows when the model
        outpaces realtime, that one when nothing drains the capture queue.
        """
        with self._lock:
            self._states[direction].dropped_s = seconds

    def set_offset_s(self, direction: Direction, seconds: float) -> None:
        with self._lock:
            self._states[direction].offset_s = seconds

    def set_session_state(self, direction: Direction, state: SessionState) -> None:
        with self._lock:
            self._states[direction].session_state = state

    def add_rotation(self, direction: Direction, *, forced: bool,
                     replayed_s: float) -> None:
        """One session handed over to its replacement.

        `forced` means the switch did not wait for a gap in the outgoing
        output. `replayed_s` is pre-roll pushed into the new session.
        """
        with self._lock:
            state = self._states[direction]
            state.rotations += 1
            if forced:
                state.forced_rotations += 1
            state.replayed_s += replayed_s

    def set_dead_air(self, direction: Direction, value: bool) -> None:
        with self._lock:
            self._states[direction].dead_air = value

    def set_no_audio(self, direction: Direction, value: bool) -> None:
        """No audio at all is reaching this direction's capture queue.

        Separate from dead_air (speech in, nothing out). An unlinked capture
        node delivers zero bytes, not silence, so every stage downstream
        looks healthy.
        """
        with self._lock:
            self._states[direction].no_audio = value

    def set_capture_dropped(self, direction: Direction, count: int) -> None:
        """Blocks the CAPTURE queue discarded, as an absolute count.

        Distinct from `dropped_s` (playout side). Reported so that lost
        capture does not read as nobody talking.
        """
        with self._lock:
            self._states[direction].capture_dropped = count

    def set_health(
        self,
        direction: Direction,
        *,
        session: Health | None = None,
    ) -> None:
        with self._lock:
            state = self._states[direction]
            if session is not None:
                state.session = session

    def add_cost(self, usd: float) -> None:
        with self._lock:
            self._cost_usd += usd

    def set_muted_out(self, value: bool) -> None:
        with self._lock:
            self._muted_out = value

    def set_bypassed(self, value: bool) -> None:
        with self._lock:
            self._bypassed = value

    def set_overlap_pct(self, value: float | None) -> None:
        with self._lock:
            self._overlap_pct = value

    def snapshot(self) -> Snapshot:
        """A deep copy. The UI renders from this while threads keep writing."""
        with self._lock:
            return Snapshot(
                directions=deepcopy(self._states),
                cost_usd=self._cost_usd,
                bypassed=self._bypassed,
                muted_out=self._muted_out,
                overlap_pct=self._overlap_pct,
            )
