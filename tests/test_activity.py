import pytest

from sidetap_live.activity import SpeechActivity
from tests.conftest import FakeClock

SPEECH = b"\x40\x10" * 800     # 100 ms of 16 kHz s16
SILENCE = b"\x00\x00" * 800


def always(value: bool):
    return lambda pcm: value


def test_no_detector_never_reports_a_pause():
    """Without a detector, idle-suspend never fires - rather than firing
    constantly on a detector saying nothing."""
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


def test_not_one_byte_reaches_the_session_differently_for_having_been_observed():
    """The name check above is necessary and nowhere near sufficient.

    Gating reinstated under any other name - observe() returning a trimmed
    buffer, a filter() added beside it, silence dropped in the pump - passes
    a hasattr assertion untouched. What the invariant actually says is that
    the bytes captured and the bytes sent are the same bytes, in the same
    order, whatever the detector thinks. So assert that.
    """
    from sidetap_live.cost import Rates
    from sidetap_live.interpreter import DirectionInterpreter, InterpreterConfig
    from sidetap_live.metrics import Metrics
    from sidetap_live.playout import Playout
    from sidetap_live.preroll import PreRoll
    from sidetap_live.types import BLOCK_BYTES, AudioChunk
    from tests.conftest import FakeAudioSink, FakeSessionFactory

    loud = b"\x00\x40" * (BLOCK_BYTES // 2)
    quiet = b"\x00\x00" * (BLOCK_BYTES // 2)
    blocks = [loud, quiet, quiet, loud, quiet, loud, loud, quiet]

    clock = FakeClock()
    sessions = FakeSessionFactory()
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.OUT, target_lang="ru", echo=True),
        sessions=sessions,
        playout=Playout(Direction.OUT, FakeAudioSink()),
        # A detector that calls only the loud blocks speech: if anything
        # anywhere gates on it, the quiet blocks are what goes missing.
        activity=SpeechActivity(lambda pcm: pcm == loud, clock),
        metrics=Metrics(),
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )

    for pcm in blocks:
        interpreter.feed(AudioChunk(track="mic", pcm=pcm, t_start=0.0))

    sent = b"".join(bytes(s.sent) for s in sessions.sessions)
    assert sent == b"".join(blocks), (
        "the stream the model received is not the stream that was captured"
    )


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


def test_overlap_is_unavailable_rather_than_zero_without_a_detector():
    """0.0% and "cannot be measured" are not the same claim.

    observe() returns False with no detector, so `speaking` never becomes
    True and OverlapWatch reported a confident 0.0% - indistinguishable from
    two people who genuinely never talked over each other. Overlap is the
    project's chosen axis and the thing manual smoke check 10 exists to
    produce, so a fabricated zero is the worst possible reading.
    """
    clock = FakeClock()
    tracks = {d: SpeechActivity(None, clock) for d in Direction}
    watch = OverlapWatch(tracks, clock)

    clock.advance(10.0)
    assert watch.sample() is None
    assert watch.pct is None


def test_overlap_is_a_number_once_every_track_can_be_measured():
    clock = FakeClock()
    tracks = {d: SpeechActivity(always(True), clock) for d in Direction}
    watch = OverlapWatch(tracks, clock)
    for track in tracks.values():
        track.observe(b"\x00\x00")

    clock.advance(10.0)
    assert watch.sample() == pytest.approx(100.0)
