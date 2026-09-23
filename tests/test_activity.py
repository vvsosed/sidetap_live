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


from sidetap_live.activity import OverlapWatch
from sidetap_live.types import Direction


def two_tracks(clock, in_speaking: bool, out_speaking: bool):
    tracks = {
        Direction.IN: SpeechActivity(always(in_speaking), clock),
        Direction.OUT: SpeechActivity(always(out_speaking), clock),
    }
    for activity in tracks.values():
        activity.observe(SPEECH if activity._detect(SPEECH) else SILENCE)
    return tracks


def test_no_overlap_when_they_take_turns():
    clock = FakeClock()
    tracks = two_tracks(clock, True, False)
    watch = OverlapWatch(tracks, clock)
    clock.advance(10.0)
    assert watch.sample() == pytest.approx(0.0)


def test_full_overlap_when_both_talk():
    clock = FakeClock()
    tracks = two_tracks(clock, True, True)
    watch = OverlapWatch(tracks, clock)
    clock.advance(10.0)
    assert watch.sample() == pytest.approx(100.0)


def test_overlap_is_a_running_fraction_of_wall_clock():
    clock = FakeClock()
    tracks = two_tracks(clock, True, True)
    watch = OverlapWatch(tracks, clock)
    clock.advance(5.0)
    watch.sample()
    tracks[Direction.OUT]._detect = always(False)
    tracks[Direction.OUT].observe(SILENCE)
    clock.advance(15.0)
    assert watch.sample() == pytest.approx(25.0)


def test_it_reports_zero_before_any_time_has_passed():
    clock = FakeClock()
    assert OverlapWatch(two_tracks(clock, True, True), clock).sample() == 0.0
