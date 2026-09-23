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
