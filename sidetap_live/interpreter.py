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
from .ports import Clock, SessionFactory, StopFlag
from .preroll import PreRoll
from .types import (
    DEAD_AIR_S,
    FATAL_OPEN_FAILURES,
    IDLE_SUSPEND_S,
    OVERLAP_MAX_S,
    REOPEN_BACKOFF_S,
    SESSION_PROBATION_S,
    SINK_STALL_S,
    STARVED_RATIO,
    STARVED_WINDOW_S,
    SUPPRESSED_SUSPEND_S,
    TARGET_RATE,
    TTS_BYTES_PER_S,
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


class SessionDied(RuntimeError):
    """A session the server accepted and then dropped.

    Carries the server's own reason, which is the actionable part: a 1011
    names a spending cap, a quota or a rejected config, sometimes with the
    URL that clears it.
    """


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
        probe=None,
    ):
        self._config = config
        self._sessions = sessions
        self._playout = playout
        # Diagnostic only, None unless --probe-audio. Energy alone could not
        # separate keep-alive padding from speech on a real call, so probe.py
        # records the raw frames beside the model's own speaking signal.
        self._probe = probe
        self._last_target_text_at: float | None = None
        # When this direction's output stopped being wanted - bypass or mute.
        # None means wanted. See set_output_wanted.
        self._unwanted_since: float | None = None
        self._wake_on_release = False
        self._release_pending = False
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
        # When the session on air opened, or None once it has outlived
        # SESSION_PROBATION_S. See _in_probation.
        self._opened_at: float | None = None
        # Output delivery over the current window. Written by the receive
        # thread under the lock, judged on the pump. See _check_output_rate.
        self._output_bytes = 0
        self._output_since: float | None = None
        self._starved = False
        # The playout thread's current sink write, judged on the pump. See
        # _check_sink.
        self._sink_stalled = False
        self._sink_blocked_s = 0.0
        self._last_playout_note: float | None = None
        # Monotonic time of the last failed open, or None. Gates retries.
        self._open_failed_at: float | None = None
        self._open_failures = 0
        self._reported_fatal = False
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

    def set_output_wanted(self, wanted: bool) -> None:
        """Tell this direction whether anyone is listening to it.

        Called when bypass or mute changes. Playout suppression alone throws
        the audio away after paying for it - the session stays open and the
        model keeps translating - so a ~30 minute mute on the 2026-10-09 call
        bought 2,129 s of OUT audio discarded on arrival, about $1.12.
        IDLE_SUSPEND_S cannot catch that: it keys on input silence and the
        user is still talking.

        Not immediate either way: suspending waits SUPPRESSED_SUSPEND_S so a
        short bypass does not churn a session that emits nothing for its
        first ~3 s, and resuming opens at once rather than on speech onset -
        see _should_wake.

        Safe from another thread: it only sets flags, and the pump acts on
        them when the next block arrives. Nothing here touches the pre-roll,
        the session, or the state.
        """
        if wanted:
            self._unwanted_since = None
            # Only a flag. PreRoll has no lock and drain() iterates the deque
            # the pump's add() appends to, so clearing it from the hotkey's
            # worker thread can raise "deque mutated during iteration".
            # _discard_suppressed_window does it on the pump.
            self._release_pending = True
            return
        if self._unwanted_since is None:
            self._unwanted_since = self._clock.monotonic()

    def _discard_suppressed_window(self) -> None:
        """Forget everything recorded while nobody was listening.

        The pre-roll, because _open(replay=True) would otherwise send the
        model up to PREROLL_S of what was said WHILE MUTED, and the remote
        party would hear a translation of it - mute means do not transmit
        this. _speech_at, because only arriving output clears it and none
        arrives while suspended, so the first feed after release would read
        as DEAD_AIR_S of speech with silence out and sound the earcon.

        Runs on the pump, before the current block is added, so the first
        block after release still reaches the new session.
        """
        # Owed only if there is something to wake. A bypass shorter than
        # SUPPRESSED_SUSPEND_S left the session RUNNING, which never consults
        # _should_wake - so the debt survived the bypass and later cancelled
        # a legitimate idle suspend, costing a connect per toggle.
        self._wake_on_release = self._state is SessionState.SUSPENDED
        # The release is itself activity. Without this the idle clock still
        # reads silent after a mute longer than IDLE_SUSPEND_S, so the block
        # after the eager open suspends it again and the warm-up is paid on
        # the first word anyway - the 30 min mute case exactly.
        self._activity.restart_silence()
        self._preroll.drain()
        with self._lock:
            self._speech_at = None

    def _suppressed_long_enough(self) -> bool:
        # Snapshotted, not read twice: set_output_wanted nulls this from the
        # hotkey's worker thread, so a release landing between the guard and
        # the subtraction computed `float - None` on the pump.
        since = self._unwanted_since
        if since is None:
            return False
        # --no-idle-suspend is an explicit choice to pay for responsiveness,
        # and this is the same trade.
        if not self._config.idle_suspend:
            return False
        return self._clock.monotonic() - since >= SUPPRESSED_SUSPEND_S

    def _suspend_for_suppression(self) -> None:
        """Close the session AND any warming replacement.

        _close only touches the session on air, and a replacement opened for a
        rotation bills just the same. Not _drop_pending: that reports a dead
        replacement and sets the reopen backoff, which would then delay the
        open on release.
        """
        pending, self._pending = self._pending, None
        self._pending_warm = False
        if pending is not None:
            pending.close()
        self._close("output suppressed")
        self._set_state(SessionState.SUSPENDED)

    # ---------- the pump thread ----------

    def feed(self, chunk: AudioChunk) -> None:
        """Handle one captured block. Public so tests drive it without threads."""
        if self._release_pending:
            self._release_pending = False
            self._discard_suppressed_window()
        # Every block, SUSPENDED included: a stalled sink blocks the playout
        # thread whatever the session is doing, so nothing would play on wake.
        self._check_sink()
        speaking = self._activity.observe(chunk.pcm)
        self._preroll.add(chunk.pcm)
        if speaking:
            with self._lock:
                if self._speech_at is None:
                    self._speech_at = self._clock.monotonic()

        if self._state is not SessionState.SUSPENDED:
            reason = self._take_dead()
            if reason is not None:
                # _reopen's pre-roll replay already sends this chunk; falling
                # through would send it twice.
                self._reopen(reason)
                return

        # Before the state dispatch, so it preempts an owed rotation and an
        # overlap in progress: closing the session makes both moot, and
        # neither is worth paying for when nobody is listening.
        if self._state is not SessionState.SUSPENDED and self._suppressed_long_enough():
            self._suspend_for_suppression()
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

        self._note_session_proven()
        self._check_output_rate()
        self._check_dead_air()
        self._send(chunk.pcm)
        if self._pending is not None:
            # Through _send_to so the cost estimate counts it: the
            # replacement is billed too.
            self._send_to(self._pending, chunk.pcm)

    def pump(self, chunks, stop: StopFlag) -> None:
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
        if self._unwanted_since is not None:
            # Nobody is listening; waking would reopen what suppression just
            # closed and bill for it.
            return False
        if self._wake_on_release:
            # Opening on release rather than on the next word is what keeps
            # unmuting from costing the fresh session's ~3 s of silence on
            # top of the user's own reaction time. Cleared by _open once a
            # session is actually up, never here: clearing on the attempt
            # lost the debt to a refused open, and the user was back to
            # waiting until they spoke. Same rule as _goaway_at's.
            return True
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
        try:
            self._session = self._sessions.open(
                self._config.target_lang, echo=self._config.echo, handle=handle
            )
        except Exception as exc:
            # Fall back to SUSPENDED, not OPENING: nothing re-wakes or sends
            # from an OPENING direction, so it would be silently dead. The
            # next speech onset retries, subject to the backoff.
            log.error("%s could not open a session: %s", self.direction.value, exc)
            self._session = None
            self._metrics.set_health(self.direction, session=Health.FAILED)
            self._set_state(SessionState.SUSPENDED)
            self._note_open_failure(exc)
            return 0.0
        # The backoff clears - an attempt got through - but NOT _open_failures
        # or _reported_fatal: the socket coming up proves nothing, and
        # resetting the give-up accounting here let a server dropping every
        # session be retried twice a second forever. _note_session_proven
        # owns that reset.
        self._open_failed_at = None
        self._opened_at = self._clock.monotonic()
        self._wake_on_release = False
        self._reset_output_rate()
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

    def _note_open_failure(self, exc: BaseException) -> None:
        """One attempt at having a session failed. Paces and gives up.

        A blip clears, but a rejected language code, a revoked key or an
        exhausted spending cap fails the same way every time, so report the
        direction dead after FATAL_OPEN_FAILURES. Reported once; the retries
        continue. The server's text is published too, because under the TUI
        the log is a file nobody is watching.
        """
        self._open_failed_at = self._clock.monotonic()
        # Never "": that is the sentinel for healthy, and the TUI keys both
        # the line and the alarm border on it, so an exception carrying no
        # message would render as a working session.
        self._metrics.set_session_error(
            self.direction, str(exc) or f"{type(exc).__name__}, with no message"
        )
        self._open_failures += 1
        if (
            self._open_failures >= FATAL_OPEN_FAILURES
            and not self._reported_fatal
            and self._on_fatal is not None
        ):
            self._reported_fatal = True
            self._on_fatal(self.direction, exc)

    def _in_probation(self) -> bool:
        """The session on air has not yet shown that it works.

        A death in this window is an open that failed late, not a session
        that broke: it must be paced and counted, or a server that accepts
        every connection and hangs up becomes an unbounded reconnect loop.
        """
        if self._opened_at is None:
            return False
        return self._clock.monotonic() - self._opened_at < SESSION_PROBATION_S

    def _note_session_proven(self) -> None:
        """A session that has outlived SESSION_PROBATION_S really opened.

        This is the reset _open's success path used to do. Here instead,
        because the socket coming up proves nothing - on 2026-10-12 every
        open succeeded and the server closed each one inside a second.
        """
        if self._opened_at is None or self._in_probation():
            return
        self._opened_at = None
        self._open_failures = 0
        self._reported_fatal = False
        self._metrics.set_session_error(self.direction, "")

    def _reset_output_rate(self) -> None:
        """No session, no measurement. Called wherever one starts or ends."""
        with self._lock:
            self._output_bytes = 0
        self._output_since = None
        self._starved = False
        self._metrics.set_output_ratio(self.direction, None)
        self._metrics.set_starved(self.direction, False)

    def _check_sink(self) -> None:
        """Is the playout thread stuck inside a sink write?

        pw-cat paces playout by blocking one chunk at a time, ~20 ms. A sink
        that stopped draining would otherwise surface only as the lag cap
        30 s later, and a bypass flushes the queue first. The thread that
        could notice is the one stuck, so the pump reads the write's start.
        Also publishes the duck's counters and the probe's playout record.
        """
        blocked = self._playout.write_blocked_s()
        stalled = blocked >= SINK_STALL_S
        if stalled:
            self._sink_blocked_s = blocked
        if stalled and not self._sink_stalled:
            log.warning(
                "%s playout sink has not accepted audio for %.1fs with %.1fs "
                "queued - pw-cat is alive but nothing is draining it, so the "
                "model's audio is reaching nobody",
                self.direction.value,
                blocked,
                self._playout.backlog_s(),
            )
        elif self._sink_stalled and not stalled:
            log.info(
                "%s playout sink is accepting audio again, after %.1fs",
                self.direction.value,
                self._sink_blocked_s,
            )
        self._sink_stalled = stalled
        self._metrics.set_sink_stalled(self.direction, stalled)

        duck = self._playout.duck
        if duck is not None:
            self._metrics.set_duck(
                self.direction,
                transitions=duck.transitions,
                failures=duck.failures,
                failing=duck.failing,
            )

        if self._probe is None:
            return
        now = self._clock.monotonic()
        if self._last_playout_note is not None and now - self._last_playout_note < 1.0:
            return
        self._last_playout_note = now
        self._probe.note_playout(
            self.direction,
            backlog_s=self._playout.backlog_s(),
            written=self._playout.written,
            duck_closed=duck is not None and not duck.is_open,
            blocked_s=blocked,
            duck_transitions=duck.transitions if duck is not None else 0,
            duck_failures=duck.failures if duck is not None else 0,
        )

    def _check_output_rate(self) -> None:
        """Is the model's continuous stream still arriving at realtime?

        It emits padding whether or not it is translating, so a session
        delivering a fraction of realtime is half-dead rather than quiet -
        measured at 0.282x on 2026-10-13 against 0.97-1.05x across 133
        consecutive windows of a healthy call. Nothing else sees this: the
        direction stays RUNNING with health OK until a Closed arrives, and
        _check_dead_air cannot fire because the bursts that do arrive keep
        clearing _speech_at.

        On the pump, not on arrival: a stream that stops dead delivers no
        chunk to judge.
        """
        if self._in_probation():
            # A fresh session emits nothing for ~3 s. Judging it would alarm
            # on every open, rotation and wake.
            return
        now = self._clock.monotonic()
        if self._output_since is None:
            # Discard what arrived during probation along with opening the
            # window, or the first verdict covers ~20 s of audio over a 10 s
            # span and reads as 2.05x - measured on two of the four probes.
            with self._lock:
                self._output_bytes = 0
            self._output_since = now
            return
        span = now - self._output_since
        if span < STARVED_WINDOW_S:
            return
        with self._lock:
            delivered, self._output_bytes = self._output_bytes, 0
        self._output_since = now
        ratio = delivered / TTS_BYTES_PER_S / span
        self._metrics.set_output_ratio(self.direction, ratio)
        starved = ratio < STARVED_RATIO
        if starved and not self._starved:
            log.warning(
                "%s output arriving at %.2fx realtime; the model's stream is "
                "continuous, so this session is probably half-dead rather "
                "than quiet",
                self.direction.value,
                ratio,
            )
        self._starved = starved
        self._metrics.set_starved(self.direction, starved)

    def _suspend(self) -> None:
        self._close("idle")
        self._set_state(SessionState.SUSPENDED)

    def _close(self, reason: str) -> None:
        self._reset_output_rate()
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
        """A GoAway landed, no replacement is listening, the backoff is up."""
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
        # The replacement goes on air with the age it already has, not the
        # outgoing session's: _switch bypasses _open, so without this a fresh
        # session would inherit "proven" and skip the pacing if it died.
        self._opened_at = self._pending_since
        self._reset_output_rate()
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
                if self._probe is not None:
                    since = None
                    if self._last_target_text_at is not None:
                        since = self._clock.monotonic() - self._last_target_text_at
                    self._probe.note(self.direction, pcm, since_target_text=since)
                with self._lock:
                    self._output_bytes += len(pcm)
                self._playout.submit(pcm)
                self._metrics.set_backlog_s(self.direction, self._playout.backlog_s())
                # submit() may have trimmed, so read the drop total back here
                # rather than letting Playout depend on Metrics.
                self._metrics.set_dropped_s(self.direction, self._playout.dropped_s)
                self._metrics.set_squelched_s(
                    self.direction, self._playout.squelched_s
                )
                # Billed on what the model SENT, not what survived the drain:
                # the padding was generated and charged for either way.
                self._metrics.add_cost(self._rates.output_usd(output_seconds(len(pcm))))
                self._note_spoke()
            case SourceText(text=text):
                self._metrics.append_text(self.direction, source=text)
                self._emit("source", text)
            case TargetText(text=text):
                self._last_target_text_at = self._clock.monotonic()
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

        A session that never made it out of probation is a failed open with
        a late verdict, so it is counted and paced like one, and falls back
        to SUSPENDED as _open's failure path does: SUSPENDED is both true and
        recoverable, _should_wake retries it once REOPEN_BACKOFF_S is up, and
        FATAL_OPEN_FAILURES can eventually say so instead of reconnecting for
        the rest of the call.
        """
        self._metrics.set_health(self.direction, session=Health.FAILED)
        if self._pending is not None:
            self._switch(forced=True)
            return
        # With the reason, not bare "died": it is the server's own account of
        # why, and the only thing in the log a user can act on.
        self._close(f"died: {reason}")
        if self._in_probation():
            self._note_open_failure(
                SessionDied(reason or "the server closed it without saying why")
            )
            self._set_state(SessionState.SUSPENDED)
            return
        # Not gated on _backing_off(): _drop_pending and _open_pending arm
        # that timer for a REPLACEMENT, and a proven session dying a moment
        # later must not be made to wait out somebody else's backoff - it
        # would lose both the two seconds and the resumption handle.
        self._open(replay=True, handle=self._handle)
