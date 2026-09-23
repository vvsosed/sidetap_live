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
