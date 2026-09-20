"""Experiment 1: open a live-translate session, measure cold start.

Answers spec experiment 6 - how long does opening a session take? That bounds
both the idle-suspend wake cost and the size of a forced seam's hole.

Corrected against google-genai 2.24.0 (see docs/experiments/01-connect.md):
`translation_config` and `input_audio_transcription` /
`output_audio_transcription` are top-level fields of `LiveConnectConfig`,
NOT nested inside `generation_config`. The plan's original hypothesis
(REST-doc-derived) nested translation_config under GenerationConfig; the
installed SDK does not accept that shape for a live session.

Run:  GEMINI_API_KEY=... uv run python scripts/exp01_connect.py
"""

import asyncio
import os
import time

from google import genai
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"
ROUNDS = 10


def config(target: str = "ru") -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target,
            echo_target_language=False,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    times = []
    for i in range(ROUNDS):
        started = time.monotonic()
        async with client.aio.live.connect(model=MODEL, config=config()) as session:
            elapsed = (time.monotonic() - started) * 1000
            times.append(elapsed)
            print(f"round {i}: connected in {elapsed:.0f} ms, session={session!r}")
    times.sort()
    print(f"\nn={len(times)}  min={times[0]:.0f}  median={times[len(times)//2]:.0f}  max={times[-1]:.0f} ms")


if __name__ == "__main__":
    asyncio.run(main())
