"""Durable transcript: append-only JSONL of events, Markdown at close.

Deliberately NOT source/target pairs. inputAudioTranscription and
outputAudioTranscription arrive as two independently-drifting streams, so a
pairing would be this program's invention rather than an observation. The
Markdown interleaves them by time instead, which is honest about what is
actually known.

Three Markdown files are rendered from the same events at close: the
interleaved one, `.original.md` (every source fragment - what was actually
said) and `.translated.md` (every target fragment - what each side heard).
All three are grouped into paragraphs ONCE and then filtered, so their blocks
share boundaries and timestamps and can be read side by side.

sidetap emits the same schema with engine "cascade" (see that repository's
transcript.py), which is what makes the two comparable.
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
# How far past a paragraph boundary the target may run to finish its sentence.
#
# The translation trails its source by about a quarter of a second, so a small
# tolerance keeps sentences whole at no cost. Beyond it, alignment wins: on a
# real call the target's sentence ends fell at 5.6, 6.3 and then 38.3 s, so an
# unbounded search cut a paragraph 13 s late and swallowed the next block's
# content - which reads as a mistranslation and is not one.
TARGET_SENTENCE_TOLERANCE_S = 3.0


# Fragments that end in a period without ending a sentence. Kept short and
# lowercase-matched: the cost of missing one is a paragraph cut slightly
# early, which is what happened for all of them before, so this only has to
# catch the common cases to be worth having.
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

    Cuts at the first sentence end at or after that time, so the translation
    keeps whole sentences while staying aligned with its source - but only
    within TARGET_SENTENCE_TOLERANCE_S. Past that, alignment wins and the cut
    lands mid-sentence, because a block that silently absorbs the next one's
    content reads as a mistranslation.
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

    The source decides where a paragraph ends - it is the record of what was
    actually said - and the target is cut at its first sentence end at or after
    that time.

    Cutting each stream independently is what produced mismatched blocks.
    Measured on a real call, the source reached its first sentence end at
    25.0 s and the target at 5.6 s, so a source paragraph covered 20 s while
    the target paragraph printed beside it covered 33 s and ran on into the
    next block's content - which reads as a mistranslation and is not one.

    Exact pairing stays impossible: the model emits no turn boundary and the
    streams drift. These are aligned TIME WINDOWS, not sentence pairs, and the
    translation still trails its source by the model's own lag - measured at
    roughly a quarter of a second.
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

    **The filter runs AFTER _paragraphs, never before**, and that is the whole
    of it. _paragraphs cuts both streams at the same boundary and the SOURCE
    decides where it falls; handed a target-only list it takes its `else`
    branch, makes the boundary the last fragment of the call, and renders an
    entire hour as one paragraph.

    Grouping once is also what makes the split files worth having together:
    the same blocks, at the same timestamps, in the same order, so
    `.original.md` and `.translated.md` line up when read side by side.
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
    boundary - experiment 4 saw `finished=True` never fire across a whole run -
    so transcription arrives a few words at a time. The JSONL keeps every
    fragment exactly as it arrived; this is a presentation choice and belongs
    here, where it loses nothing.
    """
    return _render(session, events, title="Interpretation transcript")


def render_original(session: str, events: list[TranscriptEvent]) -> str:
    """What was actually said - both directions, so both languages.

    The arrow marker is dropped: every block here is a source, so marking each
    one says nothing and costs the alignment with the translated file.
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
        # Sub-second resolution: whole seconds meant two runs started within
        # the same second appended into one file.
        self.session = session or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.jsonl_path = outdir / f"{self.session}.jsonl"
        self.md_path = outdir / f"{self.session}.md"
        # `.original.` / `.translated.` rather than a suffix swap, so all four
        # of a run's files sort together under one stem. session_name() is
        # "%Y%m%d-%H%M%S-%f" and carries no dots of its own.
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
                # Session.shutdown() joins workers on a shared deadline and
                # then restores the graph whether or not they stopped. A
                # daemon thread writing here would raise ValueError and print
                # a traceback over the TUI.
                log.debug("transcript write after close, ignored: %r", event)
                return
            self._events.append(event)
            self._write_line(event_to_dict(event))

    def _write_atomic(self, path: Path, text: str) -> None:
        """Atomic, for the same reason Journal.save() is.

        A bare write_text() can leave a truncated file if interrupted, and
        this runs inside Session.shutdown(), which is exactly where a crash or
        a second Ctrl-C lands. The .jsonl survives either way - it is flushed
        per line - but nothing regenerates a .md from it, so a torn write
        loses the readable half of the record outright.
        """
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
            # Interleaved first: it is the primary record, so if the disk
            # fills part-way through it is the one that survives.
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
