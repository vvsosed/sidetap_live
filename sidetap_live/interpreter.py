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
    DEAD_AIR_S,
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

        # The replacement session while OVERLAPPING. Fed the same audio as
        # `_session`; its output is discarded until `_switch` promotes it.
        self._pending = None
        self._pending_warm = False
        self._pending_since = 0.0
        self._outgoing_silent = False

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

        if self._state is not SessionState.SUSPENDED and self._take_dead() is not None:
            # Same reasoning as the SUSPENDED wake below: _reopen's pre-roll
            # replay already drains this chunk - it was added to the ring
            # above, before the dead check ran. Falling through to the send
            # at the end of feed() would put it on the wire twice.
            self._reopen()
            return

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

        self._check_dead_air()
        self._send(chunk.pcm)
        if self._pending is not None:
            self._pending.send(chunk.pcm)

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

    # ---------- rotation: make before break ----------

    def note_goaway(self, event: GoAway) -> None:
        """The connection will end. Open the replacement NOW.

        Not after a pause, not on a timer: a fresh session needs ~3s before
        it emits anything, so it has to start listening immediately or the
        switch reintroduces the hole. Closing the outgoing session inside
        time_left is mandatory - the server aborts with 1008 otherwise.
        """
        if self._state is not SessionState.RUNNING:
            return
        with self._lock:
            self._goaway_at = self._clock.monotonic() + event.time_left_s
        self._open_pending()

    def _open_pending(self) -> None:
        self._pending = self._sessions.open(
            self._config.target_lang, echo=self._config.echo, handle=None
        )
        self._pending_warm = False
        self._pending_since = self._clock.monotonic()
        self._outgoing_silent = False
        threading.Thread(
            target=self._receive, args=(self._pending,), daemon=True,
            name=f"recv-pending-{self.direction.value}",
        ).start()
        self._set_state(SessionState.OVERLAPPING)

    def _switch_if_ready(self) -> None:
        if self._pending is None:
            return
        expired = self._clock.monotonic() - self._pending_since >= OVERLAP_MAX_S
        if not self._pending_warm and not expired:
            return
        if not self._outgoing_silent and not expired:
            return
        self._switch(forced=expired and not self._outgoing_silent)

    def _switch(self, *, forced: bool) -> None:
        old, self._session = self._session, self._pending
        self._pending = None
        self._pending_warm = False
        with self._lock:
            self._goaway_at = None
        self._set_state(SessionState.RUNNING)
        if old is not None:
            old.close()
        self._metrics.add_rotation(self.direction, forced=forced, replayed_s=0.0)
        log.info("%s rotated (%s)", self.direction.value,
                 "forced, no output gap" if forced else "clean")

    # ---------- the receive thread ----------

    def _receive(self, session) -> None:
        """Drain one session's events. Records only - never transitions,
        except `note_goaway` which must open the replacement at once (see
        its docstring)."""
        for event in session.events():
            try:
                self.note_event_from(session, event)
            except Exception:
                log.exception("interpreter event error (%s)", self.direction.value)

    def note_event(self, event) -> None:
        """Events from the session currently on air. Public for tests."""
        self.note_event_from(self._session, event)

    def note_event_from(self, session, event) -> None:
        """Handle one event, attributed to its session.

        While OVERLAPPING, two sessions produce at once. The replacement's
        audio is DISCARDED - its first seconds translate audio the outgoing
        session has already spoken, so playing it would repeat a sentence.
        What it is used for is readiness: any energy-bearing chunk from it
        means it is warm and the switch may proceed.
        """
        if self._pending is not None and session is self._pending:
            if isinstance(event, AudioOut) and self._has_speech(event.pcm):
                self._pending_warm = True
            elif isinstance(event, ResumptionHandle):
                self.note_handle(event.handle)
            return
        if isinstance(event, AudioOut):
            self._outgoing_silent = not self._has_speech(event.pcm)
        self._dispatch(event)

    def note_handle(self, handle: str) -> None:
        """Remember the latest resumption handle for `_reopen` (Task 21)."""
        self._handle = handle

    @staticmethod
    def _has_speech(pcm: bytes) -> bool:
        """Energy, not byte presence.

        The model streams output continuously whether or not it is
        translating - measured at 0.04% of frames above threshold when idle
        against 75.5% when translating. Byte presence would mean the outgoing
        session never reads as silent and every rotation hits the bound.

        Uses playout.has_speech, NOT `find_silence_boundary(...) is None`.
        The latter reports where the first quiet frame is, and a 250 ms chunk
        of clear speech routinely contains one inside a word - so inverting it
        would call ordinary speech silent and switch sessions mid-word on
        every rotation.
        """
        from .playout import has_speech

        return has_speech(pcm)

    # ---------- Task 21: audio out, transcript, offset, dead session ----------

    def _dispatch(self, event) -> None:
        """Handle one event from the session currently on air.

        Events from a warming replacement never reach here - note_event_from
        filters them out, because its first seconds translate audio this
        session has already spoken and playing them would repeat a sentence.
        """
        match event:
            case AudioOut(pcm=pcm):
                self._playout.submit(pcm)
                self._metrics.set_backlog_s(self.direction, self._playout.backlog_s())
                # submit() may have trimmed at a silence boundary, so read the
                # drop total back here rather than letting Playout reach into
                # Metrics - the dependency runs one way only.
                self._metrics.set_dropped_s(self.direction, self._playout.dropped_s)
                self._metrics.add_cost(self._rates.output_usd(output_seconds(len(pcm))))
                self._note_spoke()
            case SourceText(text=text):
                self._metrics.set_text(self.direction, source=text)
                self._emit("source", text)
            case TargetText(text=text):
                self._metrics.set_text(self.direction, target=text)
                self._emit("target", text)
            case GoAway():
                # Must stay. note_goaway is reachable only from here.
                self.note_goaway(event)
            case ResumptionHandle(handle=handle):
                self.note_handle(handle)
            case Closed(reason=reason):
                with self._lock:
                    self._dead = reason
            case _:
                log.debug("ignoring %r", event)

    def _note_spoke(self) -> None:
        """First audio since speech started fixes this stretch's offset.

        Clearing _speech_at is what makes the metric measure the STRETCH
        rather than every chunk: the second and later chunks of the same
        utterance find it already None and leave the figure alone.
        """
        with self._lock:
            started, self._speech_at = self._speech_at, None
        if started is not None:
            self._metrics.set_offset_s(
                self.direction, self._clock.monotonic() - started
            )
        self._metrics.set_dead_air(self.direction, False)

    def _check_dead_air(self) -> None:
        """Speech went in and nothing has come out.

        Matters most on OUT: IN degrades gracefully now, because no audio out
        opens the duck and the user hears the unmediated call. On OUT there is
        no raw path to fall through to - the remote party hears nothing and
        has no way to know.
        """
        with self._lock:
            started = self._speech_at
        if started is not None and self._clock.monotonic() - started > DEAD_AIR_S:
            self._metrics.set_dead_air(self.direction, True)

    def _emit(self, kind: str, text: str) -> None:
        if self._on_event is None:
            return
        self._on_event(
            TranscriptEvent(
                t=self._clock.monotonic() - self._session_t0,
                direction=self.direction,
                kind=kind,
                text=text,
            )
        )

    def _reopen(self) -> None:
        """The session died with no GoAway. Come back on the last handle."""
        self._metrics.set_health(self.direction, session=Health.FAILED)
        self._close("died")
        self._open(replay=True, handle=self._handle)
