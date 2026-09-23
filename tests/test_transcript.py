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


def test_interleaved_streams_still_form_paragraphs(tmp_path):
    """The case a real call actually produces, and the one that was missed.

    Source and target alternate almost exactly 1:1 - measured at 101 fragments
    each in a 60 s call, about one per second per stream. Grouping by runs of
    consecutive same-kind events therefore restarts on EVERY fragment, giving
    a heading per fragment and a transcript of two-word lines.

    The original tests used bursty same-stream traffic and passed throughout.
    """
    transcript = EventTranscript(tmp_path, session="s1")
    pairs = [
        ("different languages", "И разные"),
        (" draw boundaries", " языки проводят"),
        (" at different points.", " границы в разных точках."),
    ]
    t = 0.0
    for src, tgt in pairs:
        transcript.write(event(t, Direction.IN, "source", src))
        transcript.write(event(t + 0.4, Direction.IN, "target", tgt))
        t += 1.0
    text = transcript.close().read_text()

    assert "different languages draw boundaries at different points." in text
    assert "И разные языки проводят границы в разных точках." in text
    # One heading per stream, not one per fragment.
    assert text.count("**Them**") == 1
    assert text.count("**Them →**") == 1


def test_a_long_monologue_is_broken_into_paragraphs(tmp_path):
    """Without a span rule an hour of speech is one unreadable block.

    A pause-based rule cannot do this: the longest gap measured inside a
    single stream was 1.95 s.
    """
    from sidetap_live.transcript import PARAGRAPH_SPAN_S

    transcript = EventTranscript(tmp_path, session="s1")
    for i in range(120):
        transcript.write(event(float(i), Direction.IN, "source", f" sentence {i}."))
    text = transcript.close().read_text()

    assert text.count("**Them**") > 1
    assert text.count("**Them**") <= int(120 / PARAGRAPH_SPAN_S) + 2


def test_source_and_target_blocks_cover_the_same_window(tmp_path):
    """Blocks are aligned time windows, not independently grouped streams.

    Measured on a real call, the source reached its first sentence end at
    25.0 s and the target at 5.6 s. Grouping each stream on its own sentence
    boundaries gave a 20 s source block printed above a 33 s target block,
    so the translation ran on into the next block's content - which reads as
    a mistranslation and is not one.
    """
    transcript = EventTranscript(tmp_path, session="s1")
    # Source ends sentences often; target almost never - the real pattern.
    for i in range(40):
        transcript.write(event(float(i), Direction.IN, "source", f" sentence {i}."))
        transcript.write(event(i + 0.3, Direction.IN, "target", f" fragment {i}"))
    transcript.close()

    from sidetap_live.transcript import _paragraphs
    from sidetap_live.types import TranscriptEvent

    events = [
        TranscriptEvent(t=float(i), direction=Direction.IN, kind=k, text=f" {k} {i}")
        for i in range(40)
        for k in ("source", "target")
    ]
    blocks = _paragraphs(events)
    starts = {"source": [], "target": []}
    for t, _direction, kind, _text in blocks:
        starts[kind].append(t)

    assert len(starts["source"]) == len(starts["target"]), "streams cut differently"
    for src_t, tgt_t in zip(starts["source"], starts["target"]):
        assert abs(src_t - tgt_t) <= 3.0, f"blocks drifted apart: {src_t} vs {tgt_t}"
