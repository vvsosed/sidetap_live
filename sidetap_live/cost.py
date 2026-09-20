"""A running spend estimate, computed from audio we actually moved.

sidetap infers spend from character counts against prices that render
dynamically on Google's pages. Here the quantity is exact - this process sends
and receives every byte it is billed for - so only the RATE can drift. Still an
estimate to catch a runaway session rather than an invoice, but a much
narrower one.

Rates as of docs/experiments, for gemini-3.5-live-translate-preview:
audio in $3.50/M tokens, audio out $21.00/M tokens, 25 tokens per second.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import TARGET_RATE, TTS_BYTES_PER_S


def input_seconds(pcm_bytes: int) -> float:
    """Capture side: 16 kHz s16 mono."""
    return pcm_bytes / (TARGET_RATE * 2)


def output_seconds(pcm_bytes: int) -> float:
    """Playout side: 24 kHz s16 mono."""
    return pcm_bytes / TTS_BYTES_PER_S


@dataclass(frozen=True)
class Rates:
    tokens_per_second: float = 25.0
    input_per_million: float = 3.50
    output_per_million: float = 21.00

    def input_usd(self, seconds: float) -> float:
        return seconds * self.tokens_per_second * self.input_per_million / 1_000_000

    def output_usd(self, seconds: float) -> float:
        return seconds * self.tokens_per_second * self.output_per_million / 1_000_000
