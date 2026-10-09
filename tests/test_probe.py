import json

from sidetap_live.playout import CHUNK_BYTES
from sidetap_live.probe import AudioProbe
from sidetap_live.types import Direction


def frame(peak: int) -> bytes:
    return int(peak).to_bytes(2, "little", signed=True) * (CHUNK_BYTES // 2)


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_it_records_the_peak_of_every_frame(tmp_path):
    """The raw per-frame peaks are the measurement.

    Aggregates cannot answer the question that matters - how LONG the quiet
    runs are at a given threshold - so the sequence has to survive, not a
    histogram of it.
    """
    path = tmp_path / "probe.jsonl"
    probe = AudioProbe(path)
    probe.note(Direction.IN, frame(1078) + frame(2500) + frame(300),
               since_target_text=0.4)
    probe.close()

    entries = read(path)
    assert len(entries) == 1
    assert entries[0]["peaks"] == [1078, 2500, 300]
    assert entries[0]["d"] == "in"
    assert entries[0]["tt"] == 0.4


def test_a_trailing_partial_frame_is_not_dropped_or_mismeasured(tmp_path):
    """The model's chunk size is its own choice and need not be a multiple of
    20 ms - that is one of the things this exists to measure."""
    path = tmp_path / "probe.jsonl"
    probe = AudioProbe(path)
    probe.note(Direction.OUT, frame(900) + frame(900)[: CHUNK_BYTES // 2],
               since_target_text=None)
    probe.close()

    entry = read(path)[0]
    assert entry["peaks"] == [900, 900]
    assert entry["partial"] is True
    assert entry["tt"] is None


def test_it_records_the_chunk_size_the_model_actually_sent(tmp_path):
    """Unmeasured until now, and it decides whether chunk-level disposal can
    work at all: a chunk carrying any speech cannot be dropped whole."""
    path = tmp_path / "probe.jsonl"
    probe = AudioProbe(path)
    probe.note(Direction.IN, frame(100) * 5, since_target_text=9.0)
    probe.close()

    assert read(path)[0]["bytes"] == CHUNK_BYTES * 5


def test_nothing_is_written_until_close_or_a_full_buffer(tmp_path):
    """Writing per chunk would put a synchronous file write on the receive
    thread, ~20 a second per direction."""
    path = tmp_path / "probe.jsonl"
    probe = AudioProbe(path, flush_every=3)
    for _ in range(2):
        probe.note(Direction.IN, frame(100), since_target_text=None)
    assert not path.exists()

    probe.note(Direction.IN, frame(100), since_target_text=None)
    assert len(read(path)) == 3

    probe.note(Direction.IN, frame(100), since_target_text=None)
    probe.close()
    assert len(read(path)) == 4


def test_the_payload_carries_no_audio_and_no_text(tmp_path):
    """It gets read and quoted during diagnosis, and a real call is private.

    Frame energies and timings only - never the samples, never a transcript
    fragment.
    """
    path = tmp_path / "probe.jsonl"
    probe = AudioProbe(path)
    probe.note(Direction.IN, frame(1078), since_target_text=0.1)
    probe.close()

    assert set(read(path)[0]) == {"t", "w", "d", "bytes", "peaks", "partial", "tt"}


def test_it_records_wall_clock_as_well_as_output_time(tmp_path):
    """Without wall clock the probe cannot answer the question it exists for.

    `t` is cumulative OUTPUT-audio time, so a file of it says how much audio
    arrived but not how fast. Whether a burst arrives faster than realtime -
    which is what makes queue depth grow - needs the two side by side.
    """
    path = tmp_path / "probe.jsonl"
    ticks = iter([100.0, 100.5, 103.0])
    probe = AudioProbe(path, clock=lambda: next(ticks), flush_every=1)
    probe.note(Direction.IN, frame(0) * 25, since_target_text=None)   # 0.5 s
    probe.note(Direction.IN, frame(0) * 25, since_target_text=None)   # 0.5 s
    probe.close()

    entries = read(path)
    assert [e["w"] for e in entries] == [0.0, 0.5]
    assert [e["t"] for e in entries] == [0.0, 0.5]


def test_wall_clock_shows_audio_arriving_faster_than_realtime(tmp_path):
    """The shape that makes a backlog: 1 s of audio delivered in 0.4 s."""
    path = tmp_path / "probe.jsonl"
    ticks = iter([0.0, 0.4, 0.4])
    probe = AudioProbe(path, clock=lambda: next(ticks), flush_every=1)
    probe.note(Direction.IN, frame(0) * 50, since_target_text=None)   # 1.0 s
    probe.note(Direction.IN, frame(0) * 50, since_target_text=None)
    probe.close()

    second = read(path)[1]
    assert second["t"] == 1.0 and second["w"] == 0.4, second
