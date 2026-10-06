"""Record what the model's output audio actually looks like, on a real call.

This exists because a lab measurement gave the wrong answer. Experiment 2
measured the model's idle output peaking at 1078, from a session fed
prerecorded audio already in the target language, and `SPEECH_PEAK = 2000` was
set above it. On a live Teams call the keep-alive stream carries energy over
2000 roughly every 100 ms, so every quiet run is one or two frames and a drain
that needs a 200 ms run to act removes nothing - measured on 2026-10-06, where
the OUT queue was 95% padding by volume and still pinned at the 30 s lag cap.

So the question is no longer "how much padding is there" but "is padding
separable from speech at all, and by what". That needs the raw per-frame
energies from a real call, plus the model's own signal for when it is
speaking, which is why this writes the sequence rather than a histogram of it.

Energies and timings only: a real call is private, and this file gets read
and quoted during diagnosis.
"""

from __future__ import annotations

import json
import logging
from array import array
from pathlib import Path

from .types import TTS_BYTES_PER_S, Direction

# 20 ms at 24 kHz s16 mono, matching playout's own frame size so the runs
# measured here are the runs the drain would see.
_FRAME_MS = 20
_BYTES_PER_S = TTS_BYTES_PER_S
_FRAME_BYTES = TTS_BYTES_PER_S * _FRAME_MS // 1000

log = logging.getLogger(__name__)

# Entries buffered before touching the disk. The caller is the receive thread,
# ~20 chunks a second per direction, and a synchronous write per chunk is the
# kind of thing this is meant to observe, not cause.
FLUSH_EVERY = 200


class AudioProbe:
    """Append one record per output chunk. Off unless explicitly constructed."""

    def __init__(self, path: Path, *, flush_every: int = FLUSH_EVERY):
        self._path = Path(path)
        self._flush_every = flush_every
        self._buffer: list[str] = []
        self._elapsed = 0.0

    def note(
        self,
        direction: Direction,
        pcm: bytes,
        *,
        since_target_text: float | None,
    ) -> None:
        """One chunk as the model delivered it.

        `since_target_text` is seconds since this direction last produced
        transcribed output, or None if it never has. That is the candidate
        signal that does not depend on amplitude at all: a stretch with no
        target text is padding whatever its energy.
        """
        peaks = []
        frame_bytes = _FRAME_BYTES
        offset = 0
        partial = False
        while offset < len(pcm):
            block = pcm[offset : offset + frame_bytes]
            if len(block) < frame_bytes:
                partial = True
            samples = array("h")
            samples.frombytes(block[: len(block) // 2 * 2])
            if not samples:
                break
            peaks.append(max(max(samples), -min(samples)))
            offset += frame_bytes

        self._buffer.append(
            json.dumps(
                {
                    "t": round(self._elapsed, 3),
                    "d": direction.value,
                    "bytes": len(pcm),
                    "peaks": peaks,
                    "partial": partial,
                    "tt": since_target_text,
                },
                separators=(",", ":"),
            )
        )
        # Output time, not wall clock: the caller has no clock and this is
        # what the sequence has to be read against.
        self._elapsed += len(pcm) / _BYTES_PER_S
        if len(self._buffer) >= self._flush_every:
            self._write()

    def close(self) -> None:
        self._write()

    def _write(self) -> None:
        if not self._buffer:
            return
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(self._buffer) + "\n")
        except OSError:
            # A diagnostic must never take the call down.
            log.exception("could not write the audio probe to %s", self._path)
        self._buffer.clear()
