import pytest

from sidetap_live.activity import SpeechActivity
from tests.conftest import FakeClock

SPEECH = b"\x40\x10" * 800     # 100 ms of 16 kHz s16
SILENCE = b"\x00\x00" * 800


def always(value: bool):
    return lambda pcm: value


def test_no_detector_never_reports_a_pause():
    """Without a detector, rotation falls back to a timer and idle-suspend
    never fires - rather than firing constantly on a detector saying nothing."""
    activity = SpeechActivity(None, FakeClock())
    assert activity.available is False
    assert activity.observe(SILENCE) is False
    assert activity.silence_s() == 0.0


def test_speech_resets_the_silence_clock():
    clock = FakeClock()
    activity = SpeechActivity(always(True), clock)
    clock.advance(5.0)
    activity.observe(SPEECH)
    assert activity.speaking is True
    assert activity.silence_s() == 0.0


def test_silence_accumulates_from_the_last_speech():
    clock = FakeClock()
    activity = SpeechActivity(always(True), clock)
    activity.observe(SPEECH)
    activity._detect = always(False)
    clock.advance(2.5)
    activity.observe(SILENCE)
    assert activity.speaking is False
    assert activity.silence_s() == pytest.approx(2.5)


def test_silence_accumulates_from_construction_when_nobody_has_spoken():
    """Starting the app before the call must still reach idle-suspend."""
    clock = FakeClock()
    activity = SpeechActivity(always(False), clock)
    clock.advance(60.0)
    assert activity.silence_s() == pytest.approx(60.0)


def test_it_is_not_a_gate():
    """Regression guard. sidetap's vad.py had .allows(pcm) and decided what to
    send; handing a continuous-audio model a discontinuous stream is the one
    thing this module must never do. If someone adds this method back, the
    rename did not do its job."""
    activity = SpeechActivity(always(True), FakeClock())
    assert not hasattr(activity, "allows")
