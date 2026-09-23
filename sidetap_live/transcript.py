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


def render_markdown(session: str, events: list[TranscriptEvent]) -> str:
    """Chronological, both directions and both streams interleaved.

    Consecutive fragments of the same direction and kind are joined into one
    paragraph rather than one line each. That is not cosmetic: the model
    emits no turn boundary at all - experiment 4 observed `finished=True`
    never firing across a whole run - so transcription arrives as fragments
    of a few words, roughly twice a second per stream. Rendered one per line,
    an hour of conversation is some 14,000 two-word lines and unreadable.

    The JSONL keeps every fragment exactly as it arrived; this is a
    presentation choice and belongs here rather than on the write path,
    where it would lose data.
    """
    lines = [f"# Interpretation transcript {session}", "", f"_engine: {ENGINE} ({MODEL})_", ""]
    last: tuple[str, str] | None = None
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            lines.append("".join(buffer).strip())
            buffer.clear()

    for event in sorted(events, key=lambda e: e.t):
        heading = (LABELS[event.direction], event.kind)
        if heading != last:
            flush()
            lines.append("")
            label, kind = heading
            marker = "" if kind == "source" else " →"
            lines.append(f"**{label}{marker}** _{hhmmss(event.t)}_")
            last = heading
        # Joined with no separator: fragments arrive carrying their own
        # leading space (" жили", " всегда там").
        buffer.append(event.text)
    flush()
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
