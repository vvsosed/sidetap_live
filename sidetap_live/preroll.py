"""A bounded ring of the most recent capture.

Feeds exactly two paths, both of them the non-ideal ones:

  - waking a SUSPENDED session, where the speech onset that woke it happened
    before there was a session to send it to;
  - crossing a seam where GoAway's window ran out before a pause arrived, so
    the rotation landed mid-speech.

The happy path - rotating inside a pause - drains nothing, because nothing
was being said. That is the whole point of rotating there.
"""

from __future__ import annotations

from collections import deque

from .types import BLOCK_MS, PREROLL_S, TARGET_RATE


class PreRoll:
    def __init__(self, seconds: float = PREROLL_S):
        self._blocks: deque[bytes] = deque(maxlen=max(1, int(seconds * 1000 / BLOCK_MS)))

    def add(self, pcm: bytes) -> None:
        self._blocks.append(pcm)

    def drain(self) -> list[bytes]:
        blocks = list(self._blocks)
        self._blocks.clear()
        return blocks

    def seconds(self) -> float:
        return sum(len(b) for b in self._blocks) / (TARGET_RATE * 2)
