"""One direction of the interpreter, end to end.

Instantiated twice. IN reads the remote track and speaks into your
headphones; OUT reads the mic and speaks into the virtual mic's sink. Nothing
here knows which is which beyond its config.

THE INVARIANT: every state transition happens on the pump thread. The receive
thread only records - GoAway opens the replacement, Closed sets a flag, a
resumption handle is stored - and the pump acts on them when the next block
arrives.
Transitioning from the receive thread would close a session out from under the
loop iterating it, and would need a second lock around the whole machine.
"""

from __future__ import annotations

import logging
import queue as queue_module
import threading
from dataclasses import dataclass
from typing import Callable

from .activity import SpeechActivity
from .cost import Rates, input_seconds, output_seconds
from .metrics import Health, Metrics
from .playout import Playout
from .ports import Clock, SessionFactory
from .preroll import PreRoll
from .types import (
    IDLE_SUSPEND_S,
    OVERLAP_MAX_S,
    TARGET_RATE,
    AudioChunk,
    AudioOut,
    Closed,
    Direction,
    GoAway,
    ResumptionHandle,
    SessionState,
    SourceText,
    TargetText,
    TranscriptEvent,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InterpreterConfig:
    direction: Direction
    target_lang: str
    # IN is False: a remote party already speaking your language produces no
    # output, no audio flows, the duck opens and you hear them raw. OUT must
    # be True - your real mic is never linked to the messenger, so no output
    # means the remote party hears nothing at all.
    echo: bool
    idle_suspend: bool = True


class DirectionInterpreter:
    def __init__(
        self,
        config: InterpreterConfig,
        *,
        sessions: SessionFactory,
        playout: Playout,
        activity: SpeechActivity,
        metrics: Metrics,
        clock: Clock,
        rates: Rates | None = None,
        preroll: PreRoll | None = None,
        on_event: Callable[[TranscriptEvent], None] | None = None,
        session_t0: float = 0.0,
    ):
        self._config = config
        self._sessions = sessions
        self._playout = playout
        self._activity = activity
        self._metrics = metrics
        self._clock = clock
        self._rates = rates or Rates()
        self._preroll = preroll or PreRoll()
        self._on_event = on_event
        self._session_t0 = session_t0

        self._state = SessionState.SUSPENDED
        self._session = None
        self._handle: str | None = None

        # Written by the receive thread, read by the pump thread.
        self._lock = threading.Lock()
        self._goaway_at: float | None = None
        self._dead: str | None = None
        self._speech_at: float | None = None

    @property
    def direction(self) -> Direction:
        return self._config.direction

    @property
    def state(self) -> SessionState:
        return self._state

    # ---------- the pump thread ----------

    def feed(self, chunk: AudioChunk) -> None:
        """Handle one captured block. Public so tests drive it without threads."""
        speaking = self._activity.observe(chunk.pcm)
        self._preroll.add(chunk.pcm)
        if speaking:
            with self._lock:
                if self._speech_at is None:
                    self._speech_at = self._clock.monotonic()

        if self._take_dead() is not None:
            self._reopen()

        if self._state is SessionState.SUSPENDED:
            if not self._should_wake(speaking):
                return
            # _open's pre-roll replay already drains this chunk - it was
            # added to the ring above, before the state was checked. Falling
            # through to the send below would put it on the wire twice.
            self._open(replay=True)
            return
        elif self._state is SessionState.OVERLAPPING:
            self._switch_if_ready()
        elif self._should_suspend():
            self._suspend()
            return

        self._send(chunk.pcm)

    def pump(self, chunks, stop: threading.Event) -> None:
        """Drain a capture queue into the session until told to stop."""
        while not stop.is_set():
            try:
                chunk = chunks.get(timeout=0.25)
            except queue_module.Empty:
                continue
            try:
                self.feed(chunk)
            except Exception:
                # One malformed block must not take the direction down for the
                # rest of the call.
                log.exception("interpreter pump error (%s)", self.direction.value)
        self._close("shutdown")

    def _should_wake(self, speaking: bool) -> bool:
        # With no detector there is no onset to wait for, so open at once and
        # hold the session for the whole call - the documented degraded mode.
        return speaking or not self._activity.available

    def _should_suspend(self) -> bool:
        if not self._config.idle_suspend or not self._activity.available:
            return False
        return self._activity.silence_s() >= IDLE_SUSPEND_S

    def _open(self, *, replay: bool, handle: str | None = None) -> float:
        """Open a session. Returns seconds of pre-roll replayed into it."""
        self._set_state(SessionState.OPENING)
        self._session = self._sessions.open(
            self._config.target_lang, echo=self._config.echo, handle=handle
        )
        with self._lock:
            self._goaway_at = None
            self._dead = None
        threading.Thread(
            target=self._receive,
            args=(self._session,),
            daemon=True,
            name=f"recv-{self.direction.value}",
        ).start()
        self._set_state(SessionState.RUNNING)
        self._metrics.set_health(self.direction, session=Health.OK)

        if not replay:
            return 0.0
        blocks = self._preroll.drain()
        for block in blocks:
            self._send(block)
        return sum(len(b) for b in blocks) / (TARGET_RATE * 2)

    def _suspend(self) -> None:
        self._close("idle")
        self._set_state(SessionState.SUSPENDED)

    def _close(self, reason: str) -> None:
        session = self._session
        self._session = None
        if session is not None:
            log.info("%s session closed (%s)", self.direction.value, reason)
            session.close()

    def _send(self, pcm: bytes) -> None:
        if self._session is None:
            return
        self._session.send(pcm)
        seconds = input_seconds(len(pcm))
        self._metrics.add_cost(self._rates.input_usd(seconds))

    def _set_state(self, state: SessionState) -> None:
        self._state = state
        self._metrics.set_session_state(self.direction, state)

    def _take_dead(self) -> str | None:
        with self._lock:
            reason, self._dead = self._dead, None
            return reason

    # ---------- stubs replaced by Tasks 20 and 21 ----------

    def _switch_if_ready(self) -> None:
        raise NotImplementedError("Task 20")

    def _reopen(self) -> None:
        raise NotImplementedError("Task 21")

    def _receive(self, session) -> None:
        for _ in session.events():  # replaced in Task 21
            pass
