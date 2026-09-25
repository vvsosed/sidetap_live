"""Durable transcript: append-only JSONL of events, Markdown at close.

Not source/target pairs: the two transcriptions drift independently, so a
pairing would be invented. The Markdown interleaves them by time instead.

Three Markdown files are rendered at close: the interleaved one,
`.original.md` (what was said) and `.translated.md` (what each side heard).
All are grouped into paragraphs once and then filtered, so their blocks share
boundaries and timestamps and can be read side by side.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
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
# Fragments arrive about once a second per stream, alternating, with no long
# gaps, so neither grouping by kind nor breaking at pauses works.
PARAGRAPH_SPAN_S = 18.0
# Hard cap, for a speaker who never reaches a sentence boundary.
PARAGRAPH_MAX_S = 45.0
SENTENCE_END = ".!?…。！？"
# How far past a paragraph boundary the target may run to finish its sentence.
# The translation trails its source only slightly, so this keeps sentences
# whole; beyond it alignment wins, since a block that swallows the next one's
# content reads as a mistranslation.
TARGET_SENTENCE_TOLERANCE_S = 3.0


# Fragments that end in a period without ending a sentence. Matched
# lowercase. Only the common cases: missing one just cuts a paragraph early.
ABBREVIATIONS = (
    "т.д.", "т.п.", "т.е.", "др.", "г.", "гг.", "рис.", "см.",
    "e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "ms.", "dr.", "st.",
    "fig.", "no.", "cf.", "al.",
)


def _ends_sentence(text: str) -> bool:
    stripped = text.strip()
    if not stripped.endswith(tuple(SENTENCE_END)):
        return False
    # A trailing period is ambiguous in a way that ! ? … are not, so only
    # that case is worth checking against the abbreviation list.
    lowered = stripped.lower()
    return not any(lowered.endswith(abbr) for abbr in ABBREVIATIONS)


def _cut_target(fragments: list[TranscriptEvent], after: float) -> int:
    """How many target fragments belong to a paragraph ending at `after`.

    Cuts at the first sentence end at or after that time, within
    TARGET_SENTENCE_TOLERANCE_S; past that, mid-sentence.
    """
    fallback = None
    acc = ""
    for i, event in enumerate(fragments):
        acc += event.text
        if event.t < after:
            continue
        if fallback is None:
            fallback = i + 1
        if event.t > after + TARGET_SENTENCE_TOLERANCE_S:
            break
        if _ends_sentence(acc):
            return i + 1
    return fallback if fallback is not None else len(fragments)


def _paragraphs(events: list[TranscriptEvent]) -> list[tuple[float, Direction, str, str]]:
    """Group fragments into paragraphs, cutting BOTH streams at the same point.

    The source decides where a paragraph ends, since it records what was
    said; the target is cut at its first sentence end after that. Cutting
    the streams independently would print blocks covering different spans.

    These are aligned TIME WINDOWS, not sentence pairs: the model emits no
    turn boundary and the streams drift.
    """
    out: list[tuple[float, Direction, str, str]] = []
    for direction in sorted({e.direction for e in events}, key=lambda d: d.value):
        ordered = sorted((e for e in events if e.direction == direction), key=lambda e: e.t)
        src = [e for e in ordered if e.kind == "source"]
        tgt = [e for e in ordered if e.kind == "target"]

        while src or tgt:
            if src:
                take = len(src)
                acc = ""
                for i, event in enumerate(src):
                    acc += event.text
                    span = event.t - src[0].t
                    if (span >= PARAGRAPH_SPAN_S and _ends_sentence(acc)) or span >= PARAGRAPH_MAX_S:
                        take = i + 1
                        break
                head, src = src[:take], src[take:]
                boundary = head[-1].t
                text = "".join(e.text for e in head).strip()
                if text:
                    out.append((head[0].t, direction, "source", text))
            else:
                boundary = tgt[-1].t

            if tgt:
                take = _cut_target(tgt, boundary)
                head, tgt = tgt[:take], tgt[take:]
                text = "".join(e.text for e in head).strip()
                if text:
                    out.append((head[0].t, direction, "target", text))
    return sorted(out, key=lambda p: (p[0], p[2] != "source"))


BOTH_STREAMS = ("source", "target")


def _render(
    session: str,
    events: list[TranscriptEvent],
    *,
    title: str,
    kinds: tuple[str, ...] = BOTH_STREAMS,
    mark_target: bool = True,
) -> str:
    """One rendering, filtered to the streams asked for.

    **The filter runs AFTER _paragraphs, never before.** The source decides
    the boundaries, so a target-only list would render as one paragraph.
    Grouping once also keeps the split files' blocks aligned.
    """
    lines = [f"# {title} {session}", "", f"_engine: {ENGINE} ({MODEL})_", ""]
    for t, direction, kind, text in _paragraphs(events):
        if kind not in kinds or not text:
            continue
        marker = " →" if mark_target and kind == "target" else ""
        lines.append("")
        lines.append(f"**{LABELS[direction]}{marker}** _{hhmmss(t)}_")
        lines.append(text)
    return "\n".join(lines) + "\n"


def render_markdown(session: str, events: list[TranscriptEvent]) -> str:
    """Chronological, both directions and both streams interleaved.

    Fragments are joined into paragraphs because the model emits no turn
    boundary. The JSONL keeps every fragment as it arrived.
    """
    return _render(session, events, title="Interpretation transcript")


def render_original(session: str, events: list[TranscriptEvent]) -> str:
    """What was actually said - both directions, so both languages.

    No arrow marker, which would say nothing here and break the alignment
    with the translated file.
    """
    return _render(
        session, events, title="Original transcript",
        kinds=("source",), mark_target=False,
    )


def render_translated(session: str, events: list[TranscriptEvent]) -> str:
    """What each side heard - the IN translation in your language, the OUT
    translation in theirs."""
    return _render(
        session, events, title="Translated transcript",
        kinds=("target",), mark_target=False,
    )


class EventTranscript:
    def __init__(self, outdir: Path, session: str | None = None):
        outdir.mkdir(parents=True, exist_ok=True)
        # Sub-second resolution, so runs started in the same second do not
        # share a file.
        self.session = session or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.jsonl_path = outdir / f"{self.session}.jsonl"
        self.md_path = outdir / f"{self.session}.md"
        # `.original.` / `.translated.` rather than a suffix swap, so all four
        # files sort together under one stem.
        self.original_path = outdir / f"{self.session}.original.md"
        self.translated_path = outdir / f"{self.session}.translated.md"
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
                    "started": datetime.now(UTC).isoformat(),
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
                # A worker may outlive shutdown's join deadline; writing to
                # the closed file would print a traceback over the TUI.
                log.debug("transcript write after close, ignored: %r", event)
                return
            self._events.append(event)
            self._write_line(event_to_dict(event))

    def _write_atomic(self, path: Path, text: str) -> None:
        """Atomic, because this runs during shutdown, where an interruption
        is likely, and nothing regenerates a .md from the .jsonl."""
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def close(self) -> Path:
        with self._lock:
            if self._closed:
                return self.md_path
            self._closed = True
            self._jsonl.close()
            # Interleaved first: it is the primary record.
            self._write_atomic(
                self.md_path, render_markdown(self.session, self._events)
            )
            self._write_atomic(
                self.original_path, render_original(self.session, self._events)
            )
            self._write_atomic(
                self.translated_path, render_translated(self.session, self._events)
            )
        return self.md_path
