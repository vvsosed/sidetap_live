import json

from sidetap_live.transcript import ENGINE, EventTranscript, render_markdown
from sidetap_live.types import Direction, TranscriptEvent


def event(t, direction, kind, text):
    return TranscriptEvent(t=t, direction=direction, kind=kind, text=text)


def test_the_header_names_the_engine_so_transcripts_are_comparable(tmp_path):
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.close()
    first = json.loads(transcript.jsonl_path.read_text().splitlines()[0])
    assert first["meta"]["engine"] == ENGINE


def test_events_are_appended_and_flushed_per_event(tmp_path):
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.write(event(1.0, Direction.IN, "source", "privet"))
    # Readable before close: an unclean exit must leave everything on disk.
    rows = [json.loads(line) for line in transcript.jsonl_path.read_text().splitlines()]
    assert rows[1] == {
        "t": 1.0,
        "direction": "in",
        "kind": "source",
        "text": "privet",
    }


def test_markdown_joins_fragments_into_readable_paragraphs(tmp_path):
    """The model emits no turn boundary, so text arrives a few words at a
    time. One line per fragment makes an hour-long call unreadable."""
    transcript = EventTranscript(tmp_path, session="s1")
    for t, text in ((1.0, "the coastal"), (1.4, " regions were"), (1.9, " cosmopolitan")):
        transcript.write(event(t, Direction.IN, "source", text))
    transcript.write(event(2.4, Direction.IN, "target", "прибрежные районы"))
    text = transcript.close().read_text()

    assert "the coastal regions were cosmopolitan" in text
    # The JSONL keeps them separate; only the rendering joins them.
    rows = [line for line in transcript.jsonl_path.read_text().splitlines()[1:]]
    assert len(rows) == 4


def test_markdown_interleaves_both_directions_chronologically(tmp_path):
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.write(event(2.0, Direction.OUT, "source", "how are you"))
    transcript.write(event(1.0, Direction.IN, "source", "privet"))
    transcript.write(event(1.4, Direction.IN, "target", "hello"))
    text = transcript.close().read_text()
    assert text.index("privet") < text.index("hello") < text.index("how are you")


def test_writing_after_close_is_ignored_not_raised(tmp_path):
    """Playout threads outlive shutdown's join deadline by design - the graph
    matters more than a tidy exit - and a ValueError from a daemon thread
    would print a traceback over the TUI."""
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.close()
    transcript.write(event(1.0, Direction.IN, "source", "late"))
