"""Durable transcript: append-only JSONL of events, Markdown at close.

Deliberately NOT source/target pairs. inputAudioTranscription and
outputAudioTranscription arrive as two independently-drifting streams, so a
pairing would be this program's invention rather than an observation. The
Markdown interleaves them by time instead, which is honest about what is
actually known.

sidetap emits the same schema with engine "cascade" (see that repository's
transcript.py), which is what makes the two comparable.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .live import MODEL
from .types import Direction, TranscriptEvent

log = logging.getLogger(__name__)

ENGINE = "live"
LABELS = {Direction.IN: "Them", Direction.OUT: "You"}


def hhmmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def event_to_dict(event: TranscriptEvent) -> dict:
    return {
        "t": event.t,
        "direction": event.direction.value,
        "kind": event.kind,
        "text": event.text,
    }


# A paragraph ends after this much speech, at the first sentence boundary.
#
# MEASURED against a real call: the source and target streams alternate almost
# exactly 1:1 (101 fragments each), roughly one per second per stream, and the
# longest gap inside a single stream was 1.95 s. So grouping by "consecutive
# events of the same kind" produces a heading per fragment and a transcript of
# two-word lines, and a pause-based break never fires at all.
PARAGRAPH_SPAN_S = 18.0
# Hard cap, for a speaker who never reaches a sentence boundary.
PARAGRAPH_MAX_S = 45.0
SENTENCE_END = ".!?…。！？"


def _paragraphs(events: list[TranscriptEvent]) -> list[tuple[float, Direction, str, str]]:
    """Group one stream's fragments into readable paragraphs.

    Grouped per (direction, kind) INDEPENDENTLY, not by runs of consecutive
    events. The two streams interleave, so a run-based grouping restarts on
    every fragment.

    A paragraph closes at the first sentence boundary after PARAGRAPH_SPAN_S,
    which keeps sentences whole, with a hard cap for speech that never
    supplies one.
    """
    out: list[tuple[float, Direction, str, str]] = []
    streams: dict[tuple[Direction, str], list[TranscriptEvent]] = {}
    for event in sorted(events, key=lambda e: e.t):
        streams.setdefault((event.direction, event.kind), []).append(event)

    for (direction, kind), items in streams.items():
        buffer: list[str] = []
        started = items[0].t
        for event in items:
            buffer.append(event.text)
            span = event.t - started
            text = "".join(buffer).strip()
            ends_sentence = text.endswith(tuple(SENTENCE_END))
            if (span >= PARAGRAPH_SPAN_S and ends_sentence) or span >= PARAGRAPH_MAX_S:
                out.append((started, direction, kind, text))
                buffer, started = [], event.t
        if buffer:
            out.append((started, direction, kind, "".join(buffer).strip()))
    return sorted(out, key=lambda p: (p[0], p[2] != "source"))


def render_markdown(session: str, events: list[TranscriptEvent]) -> str:
    """Chronological, both directions and both streams interleaved.

    Fragments are joined into paragraphs because the model emits no turn
    boundary - experiment 4 saw `finished=True` never fire across a whole run -
    so transcription arrives a few words at a time. The JSONL keeps every
    fragment exactly as it arrived; this is a presentation choice and belongs
    here, where it loses nothing.
    """
    lines = [f"# Interpretation transcript {session}", "", f"_engine: {ENGINE} ({MODEL})_", ""]
    for t, direction, kind, text in _paragraphs(events):
        if not text:
            continue
        marker = "" if kind == "source" else " →"
        lines.append("")
        lines.append(f"**{LABELS[direction]}{marker}** _{hhmmss(t)}_")
        lines.append(text)
    return "\n".join(lines) + "\n"


class EventTranscript:
    def __init__(self, outdir: Path, session: str | None = None):
        outdir.mkdir(parents=True, exist_ok=True)
        # Sub-second resolution: whole seconds meant two runs started within
        # the same second appended into one file.
        self.session = session or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.jsonl_path = outdir / f"{self.session}.jsonl"
        self.md_path = outdir / f"{self.session}.md"
        self._lock = threading.Lock()
        self._events: list[TranscriptEvent] = []
        self._closed = False
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        self._write_line(
            {
                "meta": {
                    "engine": ENGINE,
                    "model": MODEL,
                    "session": self.session,
                    "started": datetime.now(timezone.utc).isoformat(),
                }
            }
        )

    def _write_line(self, payload: dict) -> None:
        self._jsonl.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # Flushed per line, so a crash keeps everything up to that moment.
        self._jsonl.flush()

    def write(self, event: TranscriptEvent) -> None:
        with self._lock:
            if self._closed:
                # Session.shutdown() joins workers on a shared deadline and
                # then restores the graph whether or not they stopped. A
                # daemon thread writing here would raise ValueError and print
                # a traceback over the TUI.
                log.debug("transcript write after close, ignored: %r", event)
                return
            self._events.append(event)
            self._write_line(event_to_dict(event))

    def close(self) -> Path:
        with self._lock:
            if self._closed:
                return self.md_path
            self._closed = True
            self._jsonl.close()
            self.md_path.write_text(
                render_markdown(self.session, self._events), encoding="utf-8"
            )
        return self.md_path
