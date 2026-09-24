"""Live pipeline state, written by worker threads and read by the UI.

The dependency runs one way only: pipeline threads write here, the TUI polls
snapshot(). Nothing in the pipeline imports the UI, which is what lets
--no-tui and the headless test suite be the same code path.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

from .types import Direction, SessionState

# Characters of live transcription kept per stream for the dashboard.
#
# The model emits no turn boundary - experiment 4 saw `finished=True` never
# fire across a whole run - so text arrives as fragments of a few words, about
# twice a second. Replacing on each one leaves the pane showing two words;
# accumulating without a bound leaves it showing an hour. A rolling tail is
# what a live display actually wants, and the transcript keeps the full record.
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

    # The headline number. Translated audio queued but not yet played.
    # Whether this stays bounded under continuous speech is the result this
    # project exists to measure.
    backlog_s: float = 0.0
    # Speech onset to the first output chunk of that stretch.
    offset_s: float = 0.0

    # Seconds of translated audio the lag cap threw away, not a count of
    # utterances: there are no utterances here to count.
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
    # Mute is a separate control from bypass even though both suppress the
    # OUT playout. The TUI needs to know which the user asked for: pressed
    # while bypassed, mute changes what you come back to, not what bypass is
    # doing now. Reading playout.suppressed instead would conflate them.
    muted_out: bool = False
    # Session-wide, not per-direction: it is a property of the two tracks
    # together. Fraction of wall clock where BOTH carry speech at once -
    # the most direct measure of whether the humans stopped taking turns.
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

        APPEND, not set. The model sends no turn boundary, so each event is a
        few words; setting would leave the dashboard showing the last two. The
        tail is bounded because this is a live pane, not a log - the full text
        is in the transcript.

        Fragments arrive carrying their own leading space (" жили",
        " всегда там"), so they join with no separator.
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

        Distinct from `capture_dropped`, which counts blocks the CAPTURE queue
        discarded. The two overflow for unrelated reasons - this one when the
        model generates faster than realtime for long enough, that one when
        nothing is draining the queue - and a user who cannot tell them apart
        cannot act on either.
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

        `forced` means no pause arrived before GoAway's time_left ran out, so
        the seam landed mid-speech and `replayed_s` of pre-roll was pushed
        into the new session. The clean/forced split is what says whether the
        pause assumption in the spec survived contact.
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

        Separate from dead_air, which means an utterance finished and nothing
        came out the other end. This one is upstream of everything: the track
        itself has gone silent, and because an unlinked PipeWire capture
        delivers zero bytes rather than silence, every stage downstream looks
        healthy while doing nothing.
        """
        with self._lock:
            self._states[direction].no_audio = value

    def set_capture_dropped(self, direction: Direction, count: int) -> None:
        """Blocks the CAPTURE queue discarded, as an absolute count.

        Distinct from `dropped_s`, which counts seconds of translated audio
        the lag cap discarded on the playout side. These two queues overflow
        for unrelated reasons - this one fills when a network outage stops
        the recogniser draining it - and meetscribe's documented bug was
        exactly this one going unreported, so a lost stretch read as nobody
        talking.
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
