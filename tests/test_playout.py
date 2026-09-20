import pytest

from sidetap_live.playout import CHUNK_BYTES, DuckControl, find_silence_boundary
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
    from sidetap_live.playout import has_speech

    chunk = bytearray()
    for frame in range(12):
        amp = 200 if frame == 7 else 12000
        for _ in range(CHUNK_BYTES // 2):
            chunk += amp.to_bytes(2, "little", signed=True)

    assert find_silence_boundary(chunk) is not None   # it does find the dip
    assert has_speech(chunk) is True                  # but it is still speech


def test_has_speech_says_no_to_the_models_idle_stream():
    from sidetap_live.playout import has_speech

    idle = bytearray()
    for _ in range(CHUNK_BYTES * 6):
        idle += (1078).to_bytes(2, "little", signed=True)
    assert has_speech(idle) is False


def test_has_speech_judges_a_short_buffer_rather_than_ignoring_it():
    from sidetap_live.playout import has_speech

    assert has_speech(b"") is False
    assert has_speech(b"\x00\x40" * 10) is True      # 20 samples, loud
    assert has_speech(b"\x00\x00" * 10) is False
