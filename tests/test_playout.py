import pytest

from sidetap_live.playout import (
    CHUNK_BYTES,
    DuckControl,
    find_silence_boundary,
    has_speech,
    leading_silence_bytes,
)
from tests.conftest import FakeVolumeControl

LOUD = (b"\x00\x40" * (CHUNK_BYTES // 2))     # peak 0x4000
QUIET = (b"\x00\x00" * (CHUNK_BYTES // 2))


def test_duck_only_calls_on_a_transition():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, object_id=7)
    duck.close()
    duck.close()
    duck.close()
    assert volume.calls == [(7, 0.0)]
    duck.open()
    duck.open()
    assert volume.calls == [(7, 0.0), (7, 1.0)]


def test_duck_level_is_configurable_for_booth_mode():
    volume = FakeVolumeControl()
    DuckControl(volume, object_id=7, level=0.2).close()
    assert volume.calls == [(7, 0.2)]


def test_a_failed_call_leaves_the_flag_alone_so_the_next_one_retries():
    volume = FakeVolumeControl(ok=False)
    duck = DuckControl(volume, object_id=7)
    duck.close()
    assert duck.is_open is True
    duck.close()
    assert volume.calls == [(7, 0.0), (7, 0.0)]


def test_a_callable_object_id_is_resolved_on_every_transition():
    """Router.engage() returns before pw-loopback has registered the duck, so
    the id is still None when Session.setup() builds this. Reading it once
    meant the duck was never created and the original played under every
    translation for the whole call, with nothing logged."""
    volume = FakeVolumeControl()
    ids = iter([None, 42])
    duck = DuckControl(volume, object_id=lambda: next(ids))
    duck.close()
    assert volume.calls == []
    assert duck.is_open is True
    duck.close()
    assert volume.calls == [(42, 0.0)]


def test_silence_boundary_finds_the_first_quiet_frame():
    pcm = bytearray(LOUD + LOUD + QUIET + LOUD)
    assert find_silence_boundary(pcm) == 2 * CHUNK_BYTES


def test_silence_boundary_is_none_when_it_is_loud_throughout():
    """No boundary means run long rather than cut a word in half."""
    assert find_silence_boundary(bytearray(LOUD * 4)) is None


def test_silence_boundary_ignores_a_trailing_partial_frame():
    pcm = bytearray(LOUD + QUIET[: CHUNK_BYTES // 2])
    assert find_silence_boundary(pcm) is None


def test_the_models_idle_stream_does_not_read_as_speech():
    """Measured regression guard, docs/experiments/02-voice-stability.md.

    The model emits a continuous output stream even with nothing to
    translate, peaking at 1078. If that reads as speech the duck never
    reopens and the remote party is inaudible for the whole call.
    """
    from sidetap_live.playout import SPEECH_PEAK

    assert SPEECH_PEAK > 1078
    idle = bytearray()
    for _ in range(CHUNK_BYTES // 2):
        idle += (1078).to_bytes(2, "little", signed=True)
    assert find_silence_boundary(idle) == 0


def test_has_speech_is_not_find_silence_boundary_inverted():
    """A real 250ms chunk of speech contains quiet frames inside words.

    find_silence_boundary reports the FIRST quiet frame, so inverting it would
    call this chunk silent. The interpreter uses has_speech to decide the
    outgoing session has stopped talking; getting it backwards switches
    sessions mid-word on every rotation.
    """

    chunk = bytearray()
    for frame in range(12):
        amp = 200 if frame == 7 else 12000
        for _ in range(CHUNK_BYTES // 2):
            chunk += amp.to_bytes(2, "little", signed=True)

    assert find_silence_boundary(chunk) is not None   # it does find the dip
    assert has_speech(chunk) is True                  # but it is still speech


def test_has_speech_says_no_to_the_models_idle_stream():

    idle = bytearray()
    for _ in range(CHUNK_BYTES * 6):
        idle += (1078).to_bytes(2, "little", signed=True)
    assert has_speech(idle) is False


def test_has_speech_judges_a_short_buffer_rather_than_ignoring_it():

    assert has_speech(b"") is False
    assert has_speech(b"\x00\x40" * 10) is True      # 20 samples, loud
    assert has_speech(b"\x00\x00" * 10) is False


from sidetap_live.playout import (
    DUCK_HOLD_TICKS,
    STARVE_LIMIT_TICKS,
    Playout,
)
from sidetap_live.types import TARGET_LATENCY_S, TTS_BYTES_PER_S, Direction
from tests.conftest import FakeAudioSink

SPEECH = b"\x00\x40" * (CHUNK_BYTES // 2)


def build(**kwargs):
    sink = FakeAudioSink()
    volume = FakeVolumeControl()
    duck = DuckControl(volume, object_id=7)
    return Playout(Direction.IN, sink, duck=duck, **kwargs), sink, volume


def test_a_full_chunk_is_written_and_closes_the_duck():
    playout, sink, volume = build()
    playout.submit(SPEECH)
    assert playout.tick() is True
    assert sink.chunks == [SPEECH]
    assert volume.calls == [(7, 0.0)]


def test_an_empty_queue_writes_silence_and_keeps_the_duck_shut_until_the_hold():
    playout, sink, volume = build()
    playout.submit(SPEECH)
    playout.tick()
    for _ in range(DUCK_HOLD_TICKS - 1):
        assert playout.tick() is False
    assert volume.calls == [(7, 0.0)]          # still closed, within the hold
    playout.tick()
    assert volume.calls == [(7, 0.0), (7, 1.0)]


def test_a_gap_shorter_than_the_hold_does_not_reopen_the_duck():
    """Chunks arriving unevenly must not chop the original into fragments."""
    playout, sink, volume = build()
    playout.submit(SPEECH)
    playout.tick()
    for _ in range(DUCK_HOLD_TICKS - 2):
        playout.tick()
    playout.submit(SPEECH)
    assert playout.tick() is True
    assert volume.calls == [(7, 0.0)]


def test_a_partial_tail_waits_then_is_flushed_padded():
    playout, sink, _ = build()
    playout.submit(SPEECH[: CHUNK_BYTES // 2])
    for _ in range(STARVE_LIMIT_TICKS):
        assert playout.tick() is False
    assert playout.tick() is True
    assert len(sink.chunks[-1]) == CHUNK_BYTES
    assert sink.chunks[-1].endswith(b"\x00" * (CHUNK_BYTES // 2))


def test_backlog_reports_seconds_pending():
    # SPEECH, not silence: silence is what the drain removes, so a silent
    # buffer would make this depend on TARGET_LATENCY_S happening to equal
    # the length submitted.
    playout, _, _ = build()
    playout.submit(SPEECH * 50)
    assert playout.backlog_s() == pytest.approx(1.0)


def test_the_cap_drops_at_a_silence_boundary_only():
    # target_latency_s high enough that the drain never fires: this is about
    # _trim_locked, and the drain would otherwise remove the pause first.
    playout, _, _ = build(lag_cap_s=0.5, target_latency_s=1e9)
    loud = SPEECH * 25                                   # 0.5 s, all loud
    playout.submit(loud + QUIET + loud)
    # It cut at the pause, never inside either loud run: exactly the second
    # run survives. The pause itself is dropped too, because the buffer was
    # still over the cap once the first run was gone and an opening pause is
    # inaudible to drop - see test_the_cap_still_trims_when_the_buffer_opens
    # _on_a_pause, which is the case that behaviour exists for.
    assert playout.backlog_s() == pytest.approx(len(loud) / TTS_BYTES_PER_S)
    assert playout.dropped_s == pytest.approx(
        (len(loud) + len(QUIET)) / TTS_BYTES_PER_S
    )


def test_the_cap_refuses_to_cut_a_word_in_half():
    playout, _, _ = build(lag_cap_s=0.1, target_latency_s=1e9)
    playout.submit(SPEECH * 50)
    assert playout.dropped_s == 0.0
    assert playout.backlog_s() > 0.1


def test_the_models_idle_stream_does_not_hold_the_duck_closed():
    """The failure this whole design turns on.

    The model streams output continuously whether or not it is translating.
    Keyed on bytes arriving, the duck closes on the first chunk and never
    reopens - the remote party is inaudible for the entire call. Keyed on
    energy, idle output passes through without touching it.
    """
    playout, sink, volume = build()
    idle = bytes(bytearray().join(
        (1078).to_bytes(2, "little", signed=True) for _ in range(CHUNK_BYTES // 2)
    ))
    playout.submit(SPEECH)
    playout.tick()
    assert volume.calls == [(7, 0.0)]          # real speech closed it

    for _ in range(DUCK_HOLD_TICKS * 3):       # then a long idle stream
        playout.submit(idle)
        assert playout.tick() is False         # written, but not speech
    assert volume.calls[-1] == (7, 1.0)        # duck reopened despite bytes
    assert len(sink.chunks) > DUCK_HOLD_TICKS  # and every chunk still reached pw-cat


def test_suppressed_throws_the_queue_away_and_opens_the_duck():
    playout, sink, volume = build()
    playout.submit(SPEECH)
    playout.tick()
    playout.set_suppressed(True)
    assert playout.backlog_s() == 0.0
    assert playout.tick() is False
    assert volume.calls[-1] == (7, 1.0)


def test_the_cap_still_trims_when_the_buffer_opens_on_a_pause():
    """`if not cut` treated "the head is already quiet" (offset 0) exactly
    like "there is no pause anywhere" (None), and returned without dropping a
    byte.

    The model streams a near-silent 24 kHz output whenever it has nothing to
    translate, so a quiet head is the common case, not a corner: the one
    safety valve against a runaway backlog silently never fired.
    """
    playout, _, _ = build(lag_cap_s=0.5, target_latency_s=1e9)
    playout.submit(QUIET + SPEECH * 50 + QUIET + SPEECH * 5)

    assert playout.dropped_s > 0.0, "the cap never fired on a quiet head"
    assert playout.backlog_s() <= 0.5


def test_one_bad_chunk_does_not_end_playout_for_the_call():
    """pump() wraps feed() so "one malformed block must not take the
    direction down for the rest of the call". run() had no equivalent, so a
    single exception out of tick() ended playout permanently - the duck
    opened via the finally, but that direction never spoke again, and on OUT
    there is no raw path for the remote party to fall back to.
    """
    import threading

    class SometimesFailingSink(FakeAudioSink):
        def __init__(self):
            super().__init__()
            self.writes = 0

        def write(self, pcm):
            self.writes += 1
            if self.writes == 2:
                raise RuntimeError("transient pw-cat hiccup")
            super().write(pcm)

    sink = SometimesFailingSink()
    playout = Playout(Direction.IN, sink)
    stop = threading.Event()

    thread = threading.Thread(target=playout.run, args=(stop,), daemon=True)
    thread.start()
    for _ in range(200):
        if sink.writes > 5:
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert sink.writes > 5, (
        f"playout died on the failing chunk after {sink.writes} writes"
    )


# What the model emits between utterances on a LIVE call: exact digital
# silence (experiment 6, 2026-10-08 - 21.2% of the IN stream, 48.0% of OUT).
# Experiment 2's 1078-peak figure came from a session with nothing to
# translate and does not describe a real call; 1078 sits in the middle of
# OUT's own speech distribution, so treating it as padding cuts speech.
IDLE = (0).to_bytes(2, "little", signed=True) * (CHUNK_BYTES // 2)


def model_stream():
    """The output stream as the model actually delivers it: runs, not frames.

    2 s of translated speech then ~3.5 s of keep-alive padding. The split is
    derived from the 2026-10-05 call: of 3,588 s delivered on IN, the source
    transcript accounts for only ~1,313 s of speech (22,024 chars at
    conversational pace, times the 0.894 ratio measured in experiment 4), so
    roughly 37% of the bytes are speech and 63% are padding.

    That split is the whole reason this is fixable: speech alone arrives at
    0.69x realtime, so it FITS in the call. Only the padding pushes inflow
    over 1.0, so discarding padding is always sufficient and speech never has
    to be cut.
    """
    while True:
        for _ in range(100):
            yield SPEECH
        for _ in range(173):
            yield IDLE


def interleaved_stream():
    """Padding interleaved with speech at a 40 ms grain, same 37/63 split.

    The coarse runs in model_stream are the easy case. Nothing measures how
    finely the model actually interleaves its keep-alive stream with speech,
    so the drain has to cope with the fine grain too.
    """
    while True:
        for _ in range(37):
            yield SPEECH
            yield IDLE
            yield IDLE


def run_stream(run_s: float):
    """Contiguous speech runs of `run_s`, then padding at the 37/63 split."""
    speech_frames = int(run_s * 50)
    padding_frames = int(speech_frames * 63 / 37)
    while True:
        for _ in range(speech_frames):
            yield SPEECH
        for _ in range(padding_frames):
            yield IDLE


def feed_at(playout, inflow: float, ticks: int) -> list[float]:
    """Drive `ticks` of wall clock with the model delivering `inflow`x realtime.

    One tick is one 20 ms chunk played, which is what pw-cat's blocking write
    paces in production. Returns the backlog sampled every 100 ticks, because
    the property that matters is the trend, not any single depth.
    """
    stream = model_stream()
    carry = 0.0
    depth = []
    for tick in range(ticks):
        carry += inflow
        while carry >= 1.0:
            playout.submit(next(stream))
            carry -= 1.0
        playout.tick()
        if tick % 100 == 0:
            depth.append(playout.backlog_s())
    return depth


def test_padding_is_drained_before_the_lag_cap_is_ever_reached():
    """The failure from the 2026-10-05 call, in miniature.

    The model holds its audio channel open continuously, so inflow is padding
    PLUS speech and runs over realtime - measured at 1.89x on IN over a
    31.7 min call. Playout drains at exactly realtime, so the surplus
    accumulated as queue depth, and queue depth IS the delay you hear: it hit
    the 30 s cap 49 s into the call and stayed pinned there, with the cap
    discarding 1,688 s of real audio - 89% of the stream - to hold it.

    The cap is a safety valve against runaway speech. Reaching it because
    nobody drained the padding means it is cutting audio to solve a problem
    that was never about audio.
    """
    playout, _, _ = build(lag_cap_s=5.0)
    depth = feed_at(playout, 1.89, 3000)             # 60 s of wall clock

    assert playout.dropped_s == 0.0, (
        f"the lag cap cut real audio; {playout.backlog_s():.1f}s still queued"
    )
    assert playout.squelched_s > 0.0, "nothing was drained at all"
    # Bounded by the longest speech run, which must play out, NOT by how long
    # the call has been going. A ratchet is the bug; an oscillation is not.
    assert max(depth) < 4.0, f"latency ran away: {[round(d, 1) for d in depth]}"
    assert max(depth[-10:]) <= max(depth[:10]) + 0.5, (
        f"latency ratcheted up over the run: {[round(d, 1) for d in depth]}"
    )


def test_draining_never_cuts_speech():
    """Over the threshold is not a licence to drop. Only padding may go."""
    playout, _, _ = build()
    playout.submit(SPEECH * 150)                 # 3 s of solid speech

    assert playout.squelched_s == 0.0
    assert playout.backlog_s() == pytest.approx(3.0)


def test_a_quiet_frame_inside_a_word_is_not_a_drain_point():
    """Guards the one way to implement this that looks right and is not.

    Clear speech routinely contains a quiet 20 ms frame inside a word, so a
    drain that keys on "is this frame quiet" clips consonants. Restricting it
    to the head is NOT enough: tick() advances the head, so after 7 ticks the
    head sits exactly on the dip and a head-only drain cuts it. The guard has
    to be a minimum RUN length - a dip is one or two frames, a pause is many.
    """
    word = SPEECH * 7 + QUIET + SPEECH * 7
    playout, _, _ = build()
    playout.submit(word * 10)                    # 3 s, every word dipping

    assert find_silence_boundary(word) is not None   # the trap is reachable
    assert playout.squelched_s == 0.0, "drained at a dip inside a word"

    # The reachable case: let tick() walk the head onto the dip.
    for _ in range(7):
        playout.tick()
    playout.submit(SPEECH)
    assert playout.squelched_s == 0.0, (
        "drained a dip inside a word once the head had advanced onto it"
    )


def test_a_pause_between_sentences_is_shortened_not_erased():
    """Removing a pause outright splices two sentences into one.

    It also keeps the duck shut across what used to be the gap - _idle_ticks
    never reaches DUCK_HOLD_TICKS - so the remote party's original stays muted
    through a silence that is no longer there. The drain has to leave a floor.
    """
    playout, _, _ = build()
    playout.submit(QUIET * 60 + SPEECH * 120)    # 1.5 s pause, then 3 s

    assert playout.squelched_s > 0.0, "the pause was not drained at all"
    assert leading_silence_bytes(playout._pending) > 0, "the pause was erased"


@pytest.mark.parametrize("chunk_frames,label", [(1, "20ms"), (5, "100ms"),
                                                (10, "200ms")])
def test_the_drain_keeps_up_whatever_size_chunks_the_model_sends(
    chunk_frames, label
):
    """Throughput must not depend on how Gemini happens to packetise.

    live.py emits one AudioOut per server message, so the submit granularity
    is the model's choice and no experiment measures it. A drain that removes
    one quiet run per submit() is rate-limited by submits/second: at 100 ms
    chunks it fell 30 s behind and the cap cut 89 s of real speech, while the
    same stream submitted one 20 ms frame at a time stayed at 1.0 s. Only the
    second was tested.
    """
    playout, _, _ = build()
    stream = model_stream()
    carry, buf, peak = 0.0, [], 0.0
    for _ in range(3000):                        # 60 s of wall clock
        carry += 1.89
        while carry >= 1.0:
            buf.append(next(stream))
            carry -= 1.0
            if len(buf) >= chunk_frames:
                playout.submit(b"".join(buf))
                buf = []
        playout.tick()
        peak = max(peak, playout.backlog_s())

    assert playout.dropped_s == 0.0, (
        f"{label} chunks: the cap cut {playout.dropped_s:.1f}s of real audio"
    )
    assert peak < 4.0, f"{label} chunks: latency reached {peak:.1f}s"


def test_padding_finer_than_the_drain_can_resolve_is_left_to_the_cap():
    """The honest limit, stated as a test so nobody "fixes" it by accident.

    Padding interleaved with speech at a 40 ms grain is, to any frame-energy
    test, identical to the quiet frames that occur inside words - the same
    20 ms frames below the same SPEECH_PEAK. Draining it would mean clipping
    consonants, which is a worse failure than latency: see
    test_a_quiet_frame_inside_a_word_is_not_a_drain_point.

    So the drain declines, and LAG_CAP_S handles it as the runaway backlog it
    cannot distinguish from one. Nothing measures whether the model actually
    interleaves this finely; exp02 found its idle stretches to be long runs of
    digital silence, which the drain does resolve.
    """
    playout, _, _ = build()
    stream = interleaved_stream()
    carry = 0.0
    for _ in range(900):                         # 18 s of wall clock
        carry += 1.89
        while carry >= 1.0:
            playout.submit(next(stream))
            carry -= 1.0
        playout.tick()

    assert playout.squelched_s == 0.0, (
        "the drain cut runs short enough to be dips inside words"
    )


@pytest.mark.parametrize("run_s", [2, 10, 30])
def test_a_long_speaking_stretch_costs_latency_but_never_speech(run_s):
    """One person presenting is a long contiguous speech run.

    The model generates faster than realtime, so during an uninterrupted
    stretch it runs ahead and the queue holds real speech - that latency is
    the model's lead and draining cannot touch it. What must NOT happen is the
    cap cutting speech to hide it: at 60 s runs the unfixed drain let the cap
    discard 53.7 s. Bounding latency below the lead needs time-stretching, not
    dropping, and is deliberately out of scope here.
    """
    playout, _, _ = build()
    stream = run_stream(run_s)
    carry = 0.0
    for _ in range(2500):                        # 50 s of wall clock
        carry += 1.89
        while carry >= 1.0:
            playout.submit(next(stream))
            carry -= 1.0
        playout.tick()

    assert playout.dropped_s == 0.0, (
        f"{run_s}s runs: the cap cut {playout.dropped_s:.1f}s of real speech"
    )
    assert playout.squelched_s > 0.0, "no padding was drained"


def test_discarded_padding_is_not_reported_as_dropped_audio():
    """`dropped_s` is a quality loss the dashboard reports. Padding is not.

    Conflating them makes the TUI alarm about losing 89% of the stream during
    completely healthy operation, which trains you to ignore the one number
    that means the translation is being cut.
    """
    playout, _, _ = build()
    playout.submit(IDLE * 150)                   # 3 s of keep-alive padding

    assert playout.squelched_s > 0.0
    assert playout.dropped_s == 0.0
    assert playout.backlog_s() <= TARGET_LATENCY_S


def test_the_lag_cap_still_fires_on_a_backlog_of_real_speech():
    """Draining padding must not disarm the valve it exists to keep closed."""
    playout, _, _ = build(lag_cap_s=0.5, target_latency_s=1e9)
    playout.submit(SPEECH * 25 + QUIET + SPEECH * 25)

    assert playout.dropped_s > 0.0
    assert playout.backlog_s() <= 0.5


def test_nothing_accumulates_while_suppressed():
    """What bypass is FOR: the conversation during it happened unmediated.

    tick() consumes nothing while suppressed - it writes silence and returns -
    but submit() had no suppression guard, so the model kept filling the queue
    to LAG_CAP_S with audio nobody would ever want. Measured on the 2026-10-08
    call: 311 of its 325 cap drops fell inside one 81 s window, with the cap
    grinding continuously through it.

    A small cap here only to keep the test quick. The deep queue is also what
    made _trim_locked rescan the whole buffer on every submit, on the playout
    thread, for the entire bypass.
    """
    playout, _, _ = build(lag_cap_s=2.0)
    playout.set_suppressed(True)
    for _ in range(300):                         # 6 s of bypass
        playout.submit(SPEECH)
        playout.tick()

    assert playout.backlog_s() == 0.0, (
        f"{playout.backlog_s():.1f}s queued up during bypass"
    )
    assert playout.dropped_s == 0.0, "the lag cap ran while suppressed"
    assert playout.suppressed_s > 0.0, "the discard was not accounted for"


def test_leaving_bypass_does_not_hand_back_the_bypassed_conversation():
    """Leaving suppression used to inherit the whole bypassed window.

    Measured at the real 30 s cap: 80 s of bypass left 27.92 s queued, and
    set_suppressed(False) never flushed - so the remote party was about to
    hear the last half minute of a conversation already had without the
    interpreter.
    """
    playout, _, _ = build(lag_cap_s=2.0)
    playout.submit(SPEECH * 100)
    playout.set_suppressed(True)
    for _ in range(300):
        playout.submit(SPEECH)
        playout.tick()

    playout.set_suppressed(False)
    assert playout.backlog_s() == 0.0, (
        f"came back to {playout.backlog_s():.1f}s of the bypassed conversation"
    )


def test_the_queue_resumes_normally_after_bypass():
    """The flush must not leave playout wedged - OUT has no raw fallback."""
    playout, sink, _ = build()
    playout.set_suppressed(True)
    playout.submit(SPEECH)
    playout.set_suppressed(False)

    playout.submit(SPEECH)
    assert playout.tick() is True
    assert sink.chunks[-1] == SPEECH


# OUT's translated speech, at its measured median peak (experiment 6,
# 2026-10-08: p50 591, p90 2136). Well BELOW SPEECH_PEAK, which is why a
# drain keyed on that threshold ate it.
QUIET_SPEECH = (591).to_bytes(2, "little", signed=True) * (CHUNK_BYTES // 2)

# The model's idle output as actually measured on a live call: exact digital
# silence. 21.2% of the IN stream and 48.0% of OUT, in runs whose median
# length is 17 frames on IN and 847 on OUT. Not the 1078-peak hiss experiment
# 2 saw from a session with nothing to translate.
DIGITAL_SILENCE = QUIET


def test_quiet_speech_is_not_mistaken_for_padding():
    """OUT's speech sits below SPEECH_PEAK, so that threshold cannot gate it.

    Experiment 6 measured OUT's translated speech at a median peak of 591
    against IN's 5480. A drain keyed on SPEECH_PEAK = 2000 therefore treats
    most genuine OUT speech as padding - and OUT has no raw path to fall back
    to, so what it cuts the remote party simply never hears.
    """
    playout, _, _ = build()
    playout.submit(QUIET_SPEECH * 150)           # 3 s of OUT-level speech

    assert playout.squelched_s == 0.0, (
        f"drained {playout.squelched_s:.2f}s of speech quieter than SPEECH_PEAK"
    )
    assert playout.backlog_s() == pytest.approx(3.0)


def test_the_drain_removes_the_models_digital_silence():
    """What the model actually emits between utterances, and the whole point.

    Experiment 6: 21.2% of the IN stream and 48.0% of OUT are exact zeros,
    in runs long enough to act on. Against a measured surplus of 2.6% on IN
    that is an order of magnitude of headroom - and it carries no risk at
    all, because there is nothing in it to lose.
    """
    playout, _, _ = build()
    playout.submit(DIGITAL_SILENCE * 150)        # 3 s of exact silence

    assert playout.squelched_s > 0.0
    assert playout.dropped_s == 0.0
    assert playout.backlog_s() <= TARGET_LATENCY_S
