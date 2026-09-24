import pytest

from sidetap_live.activity import SpeechActivity
from sidetap_live.cost import Rates
from sidetap_live.interpreter import DirectionInterpreter, InterpreterConfig
from sidetap_live.metrics import Metrics
from sidetap_live.playout import Playout
from sidetap_live.preroll import PreRoll
from sidetap_live.types import (
    BLOCK_BYTES,
    IDLE_SUSPEND_S,
    REOPEN_BACKOFF_S,
    AudioChunk,
    Direction,
    SessionState,
)
from tests.conftest import FakeAudioSink, FakeClock, FakeSessionFactory

SPEECH = b"\x00\x40" * (BLOCK_BYTES // 2)
SILENCE = b"\x00\x00" * (BLOCK_BYTES // 2)


def build(*, echo=False, idle_suspend=True, speaking=True):
    clock = FakeClock()
    sessions = FakeSessionFactory()
    metrics = Metrics()
    playout = Playout(Direction.IN, FakeAudioSink())
    detector = (lambda pcm: pcm == SPEECH) if speaking else None
    interpreter = DirectionInterpreter(
        InterpreterConfig(
            direction=Direction.IN,
            target_lang="en",
            echo=echo,
            idle_suspend=idle_suspend,
        ),
        sessions=sessions,
        playout=playout,
        activity=SpeechActivity(detector, clock),
        metrics=metrics,
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )
    return interpreter, sessions, metrics, clock


def block(pcm):
    return AudioChunk(track="remote", pcm=pcm, t_start=0.0)


def test_it_starts_suspended_and_sends_nothing():
    interpreter, sessions, _, _ = build()
    assert interpreter.state is SessionState.SUSPENDED
    interpreter.feed(block(SILENCE))
    assert sessions.opens == []


def test_speech_opens_a_session_with_the_configured_target_and_echo():
    interpreter, sessions, _, _ = build(echo=True)
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING
    assert sessions.opens == [("en", True, None)]


def test_waking_replays_the_preroll_so_the_onset_is_not_lost():
    """The speech that woke the session happened before there was a session."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SPEECH))
    # Two silent blocks of pre-roll, plus the block that woke it.
    assert len(sessions.sessions[0].sent) == 3 * BLOCK_BYTES


def test_running_sends_every_block_including_silence():
    """No gate. A model reasoning over continuous audio gets continuous audio."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    sent_after_wake = len(sessions.sessions[0].sent)
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SILENCE))
    assert len(sessions.sessions[0].sent) == sent_after_wake + 2 * BLOCK_BYTES


def test_idle_suspends_and_closes_the_session():
    interpreter, sessions, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S + 1)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.SUSPENDED
    assert sessions.sessions[0].closed is True
    assert metrics.snapshot().directions[Direction.IN].session_state is SessionState.SUSPENDED


def test_no_idle_suspend_keeps_the_session_open():
    interpreter, sessions, _, clock = build(idle_suspend=False)
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S * 10)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING
    assert sessions.sessions[0].closed is False


def test_without_a_detector_it_opens_at_once_and_never_suspends():
    """Degraded mode: no webrtcvad means no pause detection, so the session is
    opened on the first block and held for the whole call."""
    interpreter, sessions, _, clock = build(speaking=False)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING
    clock.advance(IDLE_SUSPEND_S * 10)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING


def test_input_audio_is_billed():
    interpreter, _, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    # 100 ms x 25 tokens/s x $3.50/M
    assert metrics.snapshot().cost_usd == pytest.approx(0.1 * 25 * 3.50 / 1e6, rel=1e-6)


from sidetap_live.types import OVERLAP_MAX_S, AudioOut, GoAway

LOUD = b"\x00\x40" * 600       # 24 kHz s16, peak 0x4000 - reads as speech
QUIET = b"\x00\x00" * 600      # reads as silence


def goaway(interpreter, seconds=50.0):
    interpreter.note_event(GoAway(time_left_s=seconds))


def test_goaway_opens_a_replacement_at_once():
    """No waiting for a pause: the replacement needs ~3s to warm up, so it
    must start warming the moment the window opens."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.OVERLAPPING
    assert len(sessions.sessions) == 2
    assert sessions.opens[1] == ("en", False, None)


def test_both_sessions_are_fed_while_overlapping():
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    before = len(sessions.sessions[0].sent)
    interpreter.feed(block(SPEECH))
    interpreter.feed(block(SPEECH))

    assert len(sessions.sessions[0].sent) == before + 2 * BLOCK_BYTES
    assert len(sessions.sessions[1].sent) == 2 * BLOCK_BYTES


def test_the_replacements_output_is_discarded_until_it_takes_over():
    """Its first seconds translate audio the live session already spoke."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))

    interpreter.note_event_from(sessions.sessions[1], AudioOut(pcm=LOUD))
    assert interpreter._playout.backlog_s() == 0.0


def test_switch_needs_the_replacement_warm_and_the_outgoing_silent():
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions

    # Outgoing still speaking: no switch even though the replacement is warm.
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    interpreter.note_event_from(old, AudioOut(pcm=LOUD))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.OVERLAPPING
    assert old.closed is False

    # Outgoing falls silent: switch.
    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING
    assert old.closed is True


def test_a_cold_replacement_does_not_take_over_however_quiet_it_is():
    """Switching to a session that is not yet producing reintroduces the
    3-second hole this design exists to remove."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, _ = sessions.sessions

    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.OVERLAPPING
    assert old.closed is False


def test_the_overlap_is_bounded_and_a_bounded_switch_counts_as_forced():
    interpreter, sessions, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    # Outgoing never falls silent.
    interpreter.note_event_from(old, AudioOut(pcm=LOUD))
    clock.advance(OVERLAP_MAX_S + 1)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.RUNNING
    assert old.closed is True
    state = metrics.snapshot().directions[Direction.IN]
    assert (state.rotations, state.forced_rotations) == (1, 1)


def test_a_clean_switch_is_not_counted_as_forced():
    interpreter, sessions, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))

    state = metrics.snapshot().directions[Direction.IN]
    assert (state.rotations, state.forced_rotations) == (1, 0)
    assert state.replayed_s == 0.0


def test_rotation_replays_no_preroll():
    """The replacement has been listening for seconds; there is nothing it
    missed. Only an idle-suspend wake replays."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions
    sent_before_switch = len(new.sent)
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))

    # Exactly one more block - the one that drove the switch. No replay burst.
    assert len(new.sent) == sent_before_switch + BLOCK_BYTES


from sidetap_live.metrics import Health
from sidetap_live.types import (
    DEAD_AIR_S,
    TTS_BYTES_PER_S,
    AudioOut,
    Closed,
    ResumptionHandle,
    SourceText,
    TargetText,
)


def build_with_events(**kwargs):
    events = []
    interpreter, sessions, metrics, clock = build(**kwargs)
    interpreter._on_event = events.append
    return interpreter, sessions, metrics, clock, events


def test_audio_out_reaches_playout_and_is_billed():
    interpreter, _, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=b"\x00" * TTS_BYTES_PER_S))

    assert interpreter._playout.backlog_s() == pytest.approx(1.0)
    state = metrics.snapshot().directions[Direction.IN]
    assert state.backlog_s == pytest.approx(1.0)
    # 1 s of output at 25 tokens/s x $21/M, on top of the input already billed.
    assert metrics.snapshot().cost_usd > 25 * 21.0 / 1e6 * 0.9


def test_both_transcriptions_become_separate_events():
    interpreter, _, metrics, _, events = build_with_events()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(SourceText(text="privet"))
    interpreter.note_event(TargetText(text="hello"))

    assert [(e.kind, e.text) for e in events] == [
        ("source", "privet"),
        ("target", "hello"),
    ]
    state = metrics.snapshot().directions[Direction.IN]
    assert (state.source, state.target) == ("privet", "hello")


def test_offset_is_speech_onset_to_first_audio_out():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(1.4)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].offset_s == pytest.approx(1.4)


def test_offset_measures_the_stretch_not_every_chunk():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(1.4)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    clock.advance(5.0)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].offset_s == pytest.approx(1.4)


def test_dead_air_fires_when_speech_produces_nothing():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(DEAD_AIR_S + 1)
    interpreter.feed(block(SPEECH))
    assert metrics.snapshot().directions[Direction.IN].dead_air is True


def test_audio_out_clears_the_dead_air_alarm():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(DEAD_AIR_S + 1)
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].dead_air is False


def test_a_dead_session_is_reopened_on_the_last_handle():
    interpreter, sessions, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(ResumptionHandle(handle="h-9"))
    interpreter.note_event(Closed(reason="boom"))
    assert metrics.snapshot().directions[Direction.IN].session is Health.OK

    interpreter.feed(block(SPEECH))
    assert len(sessions.sessions) == 2
    assert sessions.opens[1] == ("en", False, "h-9")


def test_a_dead_session_while_suspended_is_not_reopened():
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S + 1)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.SUSPENDED

    interpreter.note_event(Closed(reason="closed by us"))
    interpreter.feed(block(SILENCE))
    assert len(sessions.sessions) == 1


def test_goaway_arriving_as_an_event_enters_overlapping():
    # NOTE: the plan's Task 21 text names this test "...enters_draining" and
    # asserts SessionState.DRAINING, a state that does not exist - it is the
    # pre-reconciliation name for what Task 20's make-before-break rewrite
    # renamed to OVERLAPPING (see the plan's own Task 20 section, "In feed(),
    # replace the DRAINING branch", and SessionState's docstring). Every other
    # rotation test in this file already asserts OVERLAPPING for this same
    # transition. Corrected here rather than left permanently red; see the
    # Task 21 report for the full disagreement.
    interpreter, _, _, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(GoAway(time_left_s=30.0))
    assert interpreter.state is SessionState.OVERLAPPING


def test_a_death_mid_overlap_promotes_the_replacement_instead_of_orphaning_it():
    """Regression: the replacement was left fed, billed and never closed.

    feed() sends to _pending for as long as it is set, so a replacement that
    is neither promoted nor closed keeps consuming audio and money for the
    rest of the call, with its receive thread still alive. The window this
    happens in is 50s every ~9 minutes.
    """
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(GoAway(time_left_s=50.0))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.OVERLAPPING
    replacement = interpreter._pending

    interpreter.note_event(Closed(reason="outgoing died"))
    interpreter.feed(block(SPEECH))

    # Promoted, not orphaned: no third session, nothing left dangling.
    assert len(sessions.sessions) == 2
    assert interpreter.state is SessionState.RUNNING
    assert interpreter._pending is None
    assert replacement.closed is False          # it is the live one now

    sent = len(replacement.sent)
    for _ in range(3):
        interpreter.feed(block(SPEECH))
    # Fed exactly once per block, as the on-air session - not twice, and not
    # as a leaked second consumer.
    assert len(replacement.sent) == sent + 3 * BLOCK_BYTES


class _DeadFactory:
    """A factory whose sessions never open."""

    def __init__(self):
        self.attempts = 0

    def open(self, target_lang, *, echo, handle=None):
        self.attempts += 1
        raise RuntimeError("PERMISSION_DENIED: API key revoked")


def build_dead():
    from sidetap_live.cost import Rates
    from sidetap_live.preroll import PreRoll

    clock = FakeClock()
    sessions = _DeadFactory()
    metrics = Metrics()
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.IN, target_lang="en", echo=False),
        sessions=sessions,
        playout=Playout(Direction.IN, FakeAudioSink()),
        activity=SpeechActivity(lambda pcm: True, clock),
        metrics=metrics,
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )
    return interpreter, sessions, metrics, clock


def test_a_failed_open_falls_back_to_suspended_not_stuck_in_opening():
    """OPENING is a lie after a failure, and a trap.

    Nothing re-wakes an OPENING direction and nothing sends from one, so the
    direction goes silently dead for the rest of the call with only the
    dead-air alarm ever noticing.
    """
    interpreter, _, metrics, _ = build_dead()
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.SUSPENDED
    assert metrics.snapshot().directions[Direction.IN].session is Health.FAILED


def test_a_failed_open_backs_off_instead_of_retrying_every_block():
    """Ten attempts a second at an API that just refused us is how a revoked
    key becomes a rate-limit ban."""
    from sidetap_live.types import REOPEN_BACKOFF_S

    interpreter, sessions, _, clock = build_dead()
    for _ in range(20):
        interpreter.feed(block(SPEECH))
    assert sessions.attempts == 1

    clock.advance(REOPEN_BACKOFF_S + 0.1)
    interpreter.feed(block(SPEECH))
    assert sessions.attempts == 2


def test_recovery_clears_the_backoff_and_the_health_flag():
    interpreter, sessions, metrics, clock = build_dead()
    interpreter.feed(block(SPEECH))
    assert metrics.snapshot().directions[Direction.IN].session is Health.FAILED

    working = FakeSessionFactory()
    interpreter._sessions = working
    clock.advance(__import__("sidetap_live.types",fromlist=["x"]).REOPEN_BACKOFF_S + 0.1)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.RUNNING
    assert metrics.snapshot().directions[Direction.IN].session is Health.OK


class _FlakyFactory(FakeSessionFactory):
    """Opens fine except on the nth call, which raises."""

    def __init__(self, fail_on: int):
        super().__init__()
        self._fail_on = fail_on
        self.attempts = 0

    def open(self, target_lang, *, echo, handle=None):
        self.attempts += 1
        if self.attempts == self._fail_on:
            raise RuntimeError("503 backend unavailable")
        return super().open(target_lang, echo=echo, handle=handle)


def build_flaky(fail_on):
    clock = FakeClock()
    sessions = _FlakyFactory(fail_on)
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.OUT, target_lang="ru", echo=True),
        sessions=sessions,
        playout=Playout(Direction.OUT, FakeAudioSink()),
        activity=SpeechActivity(lambda pcm: pcm == SPEECH, clock),
        metrics=Metrics(),
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )
    return interpreter, sessions, clock


def test_a_replacement_that_cannot_be_opened_does_not_escape_or_wedge_the_state():
    """_open() guards its connect; _open_pending() did not.

    It runs on the receive thread, whose loop logs and swallows, so a 503 at
    the nine-minute mark silently left _pending None with the state still
    RUNNING - and note_goaway() early-returns on anything but RUNNING, so
    nothing ever tried again. The session then overran GoAway's time_left and
    the server killed it with 1008, mid-conversation.
    """
    interpreter, sessions, _ = build_flaky(fail_on=2)
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING

    goaway(interpreter)  # must not raise out onto the receive thread

    assert interpreter.state is SessionState.RUNNING
    assert interpreter._pending is None


def test_a_replacement_that_failed_to_open_is_retried_before_the_window_closes():
    """A transient failure must cost a retry, not the connection."""
    interpreter, sessions, clock = build_flaky(fail_on=2)
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    assert interpreter.state is SessionState.RUNNING, "precondition: the open failed"

    clock.advance(REOPEN_BACKOFF_S + 0.1)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.OVERLAPPING
    assert interpreter._pending is not None
