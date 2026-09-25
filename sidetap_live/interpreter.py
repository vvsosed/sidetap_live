"""One direction of the interpreter, end to end.

Instantiated twice. IN reads the remote track and speaks into your
headphones; OUT reads the mic and speaks into the virtual mic's sink. Nothing
here knows which is which beyond its config.

THE INVARIANT: every state transition happens on the pump thread. The receive
thread only records (Closed sets a flag, a handle is stored) and the pump acts
on the next block. Transitioning from the receive thread would close a session
under the loop iterating it. Exceptions, touching only `_pending` and the
state: note_goaway() and _drop_pending().
"""

from __future__ import annotations

import logging
import queue as queue_module
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .activity import SpeechActivity
from .cost import Rates, input_seconds, output_seconds
from .metrics import Health, Metrics
from .playout import Playout
from .ports import Clock, SessionFactory
from .preroll import PreRoll
from .types import (
    DEAD_AIR_S,
    FATAL_OPEN_FAILURES,
    IDLE_SUSPEND_S,
    OVERLAP_MAX_S,
    REOPEN_BACKOFF_S,
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
    # output, so the duck opens and you hear them raw. OUT must be True: your
    # real mic is never linked to the messenger, so no output means silence.
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
        on_fatal: Callable[[Direction, BaseException], None] | None = None,
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
        self._on_fatal = on_fatal
        self._session_t0 = session_t0

        self._state = SessionState.SUSPENDED
        self._session = None
        # Monotonic time of the last failed open, or None. Gates retries.
        self._open_failed_at: float | None = None
        self._open_failures = 0
        self._reported_fatal = False
        # Whether the session on air has sent any event yet. Written by the
        # receive thread; a session that dies before it does was refused.
        self._heard = False
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

        if self._open_failures and self._heard:
            # The session on air has answered, so the run of failures is over.
            self._open_failures = 0
            self._reported_fatal = False

        dead = None
        if self._state is not SessionState.SUSPENDED:
            dead = self._take_dead()
        if dead is not None:
            # _reopen's pre-roll replay already sends this chunk; falling
            # through would send it twice.
            self._reopen(dead)
            return

        if self._state is SessionState.SUSPENDED:
            if not self._should_wake(speaking):
                return
            # _open's pre-roll replay already sends this chunk.
            self._open(replay=True)
            return
        elif self._state is SessionState.OVERLAPPING:
            self._switch_if_ready()
        elif self._rotation_due():
            # A GoAway arrived but the replacement could not be opened. Retry
            # rather than run past time_left into a 1008. Checked before
            # _should_suspend so an owed rotation always wins.
            self._open_pending()
        elif self._should_suspend():
            self._suspend()
            return

        self._check_dead_air()
        self._send(chunk.pcm)
        if self._pending is not None:
            # Through _send_to so the cost estimate counts it: the
            # replacement is billed too.
            self._send_to(self._pending, chunk.pcm)

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
                # One bad block must not take the direction down.
                log.exception("interpreter pump error (%s)", self.direction.value)
        self._close("shutdown")

    def _backing_off(self) -> bool:
        """True while a recent failed open should not be retried yet."""
        if self._open_failed_at is None:
            return False
        return self._clock.monotonic() - self._open_failed_at < REOPEN_BACKOFF_S

    def _should_wake(self, speaking: bool) -> bool:
        if self._backing_off():
            return False
        # With no detector there is no onset to wait for, so open at once and
        # hold the session for the whole call.
        return speaking or not self._activity.available

    def _should_suspend(self) -> bool:
        if not self._config.idle_suspend or not self._activity.available:
            return False
        return self._activity.silence_s() >= IDLE_SUSPEND_S

    def _open(self, *, replay: bool, handle: str | None = None) -> float:
        """Open a session. Returns seconds of pre-roll replayed into it."""
        self._set_state(SessionState.OPENING)
        self._heard = False
        try:
            self._session = self._sessions.open(
                self._config.target_lang, echo=self._config.echo, handle=handle
            )
        except Exception as exc:
            log.error("%s could not open a session: %s", self.direction.value, exc)
            self._open_failed(exc)
            return 0.0
        # The failure count is cleared only once this session answers (see
        # feed()): the real factory connects in the background, so returning
        # here proves nothing.
        self._open_failed_at = None
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

    def _open_failed(self, exc: BaseException) -> None:
        """Fall back to SUSPENDED, back off, and report a direction that
        keeps failing."""
        # SUSPENDED, not OPENING: nothing re-wakes or sends from an OPENING
        # direction, so it would be silently dead. The next speech onset
        # retries, subject to the backoff.
        self._session = None
        self._open_failed_at = self._clock.monotonic()
        self._metrics.set_health(self.direction, session=Health.FAILED)
        self._set_state(SessionState.SUSPENDED)
        # A blip clears, but a rejected language code or revoked key fails
        # the same way every time, so report the direction dead after
        # FATAL_OPEN_FAILURES. Reported once; the owner decides whether to
        # stop the direction, and until it does the retries continue.
        self._open_failures += 1
        if (
            self._open_failures >= FATAL_OPEN_FAILURES
            and not self._reported_fatal
            and self._on_fatal is not None
        ):
            self._reported_fatal = True
            self._on_fatal(self.direction, exc)

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
        self._send_to(self._session, pcm)

    def _send_to(self, session, pcm: bytes) -> None:
        session.send(pcm)
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

        A fresh session needs ~3 s before it emits anything, so it must start
        listening immediately. The outgoing session must close inside
        time_left or the server aborts with 1008.
        """
        if self._state is not SessionState.RUNNING:
            return
        with self._lock:
            self._goaway_at = self._clock.monotonic() + event.time_left_s
        self._open_pending()

    def _rotation_due(self) -> bool:
        """A GoAway landed and there is still no replacement listening."""
        with self._lock:
            pending_deadline = self._goaway_at
        return pending_deadline is not None and not self._backing_off()

    def _open_pending(self) -> None:
        try:
            self._pending = self._sessions.open(
                self._config.target_lang, echo=self._config.echo, handle=None
            )
        except Exception as exc:
            # Guarded because this can run on the receive thread, whose loop
            # swallows exceptions and would cancel the rotation silently.
            # _goaway_at stays set so feed() still owes the rotation and
            # retries, paced by REOPEN_BACKOFF_S.
            log.error(
                "%s could not open a replacement session: %s",
                self.direction.value,
                exc,
            )
            self._pending = None
            self._open_failed_at = self._clock.monotonic()
            return
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
        """Drain one session's events. Records only, except `note_goaway`,
        which must open the replacement at once."""
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

        While OVERLAPPING, the replacement's audio is DISCARDED: its first
        seconds translate audio the outgoing session already spoke. Any
        energy-bearing chunk from it means it is warm and may take over.
        """
        if self._pending is not None and session is self._pending:
            if isinstance(event, AudioOut) and self._has_speech(event.pcm):
                self._pending_warm = True
            elif isinstance(event, ResumptionHandle):
                self.note_handle(event.handle)
            elif isinstance(event, Closed):
                self._drop_pending(event.reason)
            return
        if session is not None and session is not self._session:
            # A retired session still drains what it had queued. Playing it
            # after the handover would repeat a sentence. Its Closed is
            # dropped too: retiring it must not look like the live one dying.
            return
        if not isinstance(event, Closed):
            self._heard = True
        if isinstance(event, AudioOut):
            self._outgoing_silent = not self._has_speech(event.pcm)
        self._dispatch(event)

    def _drop_pending(self, reason: str) -> None:
        """The replacement died before it could take over.

        Otherwise OVERLAP_MAX_S would promote a dead session whose events()
        has ended, and the direction would stay silent for the rest of the
        call. _goaway_at stays set so the pump opens a fresh replacement,
        paced by REOPEN_BACKOFF_S. Safe on the receive thread: it touches
        only _pending and the state.
        """
        log.warning(
            "%s replacement session died before taking over (%s); "
            "staying on the outgoing session and retrying",
            self.direction.value,
            reason,
        )
        self._pending = None
        self._pending_warm = False
        self._open_failed_at = self._clock.monotonic()
        if self._state is SessionState.OVERLAPPING:
            self._set_state(SessionState.RUNNING)

    def note_handle(self, handle: str) -> None:
        """Remember the latest resumption handle for `_reopen`."""
        self._handle = handle

    @staticmethod
    def _has_speech(pcm: bytes) -> bool:
        """Energy, not byte presence: the model streams output continuously,
        so byte presence would never read as silent.

        Not `find_silence_boundary(...) is None`, which finds the first quiet
        frame; speech often has one inside a word, so that would switch
        sessions mid-word.
        """
        from .playout import has_speech

        return has_speech(pcm)

    # ---------- audio out, transcript, offset, dead session ----------

    def _dispatch(self, event) -> None:
        """Handle one event from the session currently on air.

        A warming replacement's events never reach here; note_event_from
        filters them.
        """
        match event:
            case AudioOut(pcm=pcm):
                self._playout.submit(pcm)
                self._metrics.set_backlog_s(self.direction, self._playout.backlog_s())
                # submit() may have trimmed, so read the drop total back here
                # rather than letting Playout depend on Metrics.
                self._metrics.set_dropped_s(self.direction, self._playout.dropped_s)
                self._metrics.add_cost(self._rates.output_usd(output_seconds(len(pcm))))
                self._note_spoke()
            case SourceText(text=text):
                self._metrics.append_text(self.direction, source=text)
                self._emit("source", text)
            case TargetText(text=text):
                self._metrics.append_text(self.direction, target=text)
                self._emit("target", text)
            case GoAway():
                # The only path to note_goaway.
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

        Clearing _speech_at makes later chunks of the same stretch leave the
        figure alone.
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

        Matters most on OUT: on IN no output opens the duck and the user
        hears the call raw, but on OUT the remote party hears nothing.
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

    def _reopen(self, reason: str) -> None:
        """The session died. Prefer a warming replacement over a cold start.

        Promoting a replacement that is already listening skips the ~3 s
        warm-up and avoids orphaning it: feed() keeps sending to `_pending`,
        so an orphan would be fed and billed for the rest of the call. The
        handover counts as forced, since it did not wait for an output gap.

        A session that died before sending any event was refused, not
        dropped: a rejected language code closes it ~1 s in, before the first
        output (~3 s) or resumption handle (~5 s). That counts as a failed
        open, with the backoff, rather than an immediate reconnect that would
        fail the same way about once a second for the rest of the call.
        """
        self._metrics.set_health(self.direction, session=Health.FAILED)
        if self._pending is not None:
            self._switch(forced=True)
            return
        self._close("died")
        if not self._heard:
            log.error(
                "%s session closed before answering: %s", self.direction.value, reason
            )
            self._open_failed(RuntimeError(reason))
            return
        self._open(replay=True, handle=self._handle)
