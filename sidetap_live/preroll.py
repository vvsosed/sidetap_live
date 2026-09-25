"""A bounded ring of the most recent capture.

Replayed into a freshly opened session, so the speech that woke a SUSPENDED
direction, or that a dead session never translated, is not lost. Rotation
needs none: the replacement is fed live audio while the outgoing one talks.
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
