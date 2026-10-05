import pytest

from sidetap_live.playout import (
    CHUNK_BYTES,
    DuckControl,
    find_silence_boundary,
    has_speech,
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
    playout, _, _ = build()
    playout.submit(b"\x00" * TTS_BYTES_PER_S)
    assert playout.backlog_s() == pytest.approx(1.0)


def test_the_cap_drops_at_a_silence_boundary_only():
    playout, _, _ = build(lag_cap_s=0.5)
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
    playout, _, _ = build(lag_cap_s=0.1)
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
    playout, _, _ = build(lag_cap_s=0.5)
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


# The model's keep-alive stream at its measured idle level, peak 1078
# (docs/experiments/02-voice-stability.md) - below SPEECH_PEAK, so inaudible.
IDLE = (1078).to_bytes(2, "little", signed=True) * (CHUNK_BYTES // 2)


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

    `find_silence_boundary` reports the first quiet frame, and clear speech
    routinely contains one inside a word - so draining to it cuts the word in
    half. Only a quiet run at the HEAD is safe to remove, which is what
    leading_silence_bytes answers.
    """
    word = SPEECH * 7 + QUIET + SPEECH * 7
    playout, _, _ = build()
    playout.submit(word * 10)                    # 3 s, every word dipping

    assert find_silence_boundary(word) is not None   # the trap is reachable
    assert playout.squelched_s == 0.0, "drained at a dip inside a word"


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
    playout, _, _ = build(lag_cap_s=0.5)
    playout.submit(SPEECH * 25 + QUIET + SPEECH * 25)

    assert playout.dropped_s > 0.0
    assert playout.backlog_s() <= 0.5
