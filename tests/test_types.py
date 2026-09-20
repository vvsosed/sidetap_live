import pytest

from sidetap_live.types import (
    BLOCK_BYTES,
    MIC,
    REMOTE,
    AudioChunk,
    Direction,
)


def test_block_bytes_is_100ms_of_16k_mono_s16():
    # 16000 samples/s * 2 bytes * 0.1 s
    assert BLOCK_BYTES == 3200


def test_direction_in_reads_the_remote_track():
    assert Direction.IN.track == REMOTE
    assert Direction.OUT.track == MIC


def test_direction_opposite_flips():
    assert Direction.IN.opposite is Direction.OUT
    assert Direction.OUT.opposite is Direction.IN


def test_direction_values_match_the_wire_format():
    # These strings reach the durable JSONL transcript and Textual widget ids.
    # Identity-based tests above would not catch the two literals being swapped.
    assert Direction.IN.value == "in"
    assert Direction.OUT.value == "out"


def test_audio_chunk_is_frozen():
    chunk = AudioChunk(track=MIC, pcm=b"\x00" * BLOCK_BYTES, t_start=1.5)
    with pytest.raises(AttributeError):
        chunk.t_start = 2.0
