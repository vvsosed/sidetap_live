"""Experiment 2: does the output voice survive a session rotation?

Answers the question that can invalidate the spec's session-continuity
decision: rotate-at-a-pause is only the right seam strategy if voice
identity survives across a session boundary landing in silence. Gemini's
model card warns voices "might shift after long pauses" - if that is true
here, rotating exactly at a pause makes the seam maximally audible instead
of inaudible, and the seam strategy must become make-before-break.

Feeds the SAME clip (tests/fixtures/speech_en_16k.raw) twice:
  1. continuous - one session, the whole clip.
  2. rotated    - two sessions, split at SPLIT_S: clip[:split] through the
                  first, clip[split:] through a second, FRESH session (no
                  session_resumption handle - a clean rotation deliberately
                  starts over, since the spec assumes literal interpretation
                  carries almost no context across a sentence boundary).

Writes both outputs as raw 24 kHz s16 mono PCM to docs/experiments/audio/,
and prints every input_transcription / output_transcription with a
timestamp, so a content regression (dropped/repeated clause) can be told
apart from a pure timbre change.

Uses the config shape established and corrected in experiment 1 (see
docs/experiments/01-connect.md): `translation_config`,
`input_audio_transcription`, and `output_audio_transcription` are top-level
fields of `LiveConnectConfig`, not nested inside `generation_config`. No
`session_resumption` / `context_window_compression` fields are set here -
unlike exp03_session_limits.py, this experiment wants genuinely fresh
sessions, not resumed ones.

TARGET defaults to "en", not the "ru" used as an illustrative default in
exp01/exp03. This was discovered empirically, not assumed: the fixture's
actual spoken content (confirmed via input_transcription, despite its
tests/fixtures/speech_en_16k.raw filename) is Russian. A first run of this
script with target="ru" - source language == target language - produced
"audio" output that was 99.6%+ digital silence (a ~0.6s startup blip, then
zeros for the rest of each session; no output_transcription text ever
appeared). A 15s throwaway diagnostic with target="en" on the same clip
immediately produced substantive audio (70% nonzero bytes, peak within ~6%
of the input's own peak) and populated output_transcription text. So with
echo_target_language=False, this model appears to suppress spoken output
when it judges the input is already in the target language - a real
finding in its own right, but one that would have made THIS experiment
answer "no voice to compare" instead of the question it's meant to answer.
Using target="en" is a parameter value, not a structural change to
`config()` or a different model - the shape above is unchanged.

Run (in background - takes ~3.5 minutes of audio, twice, plus overhead):
  GEMINI_API_KEY=... uv run python scripts/exp02_voice_stability.py
"""

import asyncio
import os
import pathlib
import time

from google import genai
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"

BLOCK = 3200  # 100 ms of 16 kHz s16 mono
REALTIME = True  # feed at wall-clock speed, as the real app does

CLIP = pathlib.Path("tests/fixtures/speech_en_16k.raw")
OUT = pathlib.Path("docs/experiments/audio")

# Midpoint of the longest silent gap in the clip's energy profile (32.3s-
# 33.1s, 0.8s wide) - the only pause besides the one at 12.0s that
# comfortably exceeds the design's ROTATE_PAUSE_S of 0.7s. Splitting here
# simulates a clean pause-rotation, not a forced mid-word one. Do not change
# without re-analysing the clip.
SPLIT_S = 32.7

TAIL_WAIT_S = 4.0  # let the tail arrive after sending, before closing


def config(target: str = "en") -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target,
            echo_target_language=False,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


def elapsed_prefix(start: float) -> str:
    return f"[t={time.monotonic() - start:6.1f}s]"


def _process(msg, start: float, label: str, audio_buf: bytearray, transcript_log: list) -> None:
    data = getattr(msg, "data", None)
    if data:
        audio_buf.extend(data)

    sc = getattr(msg, "server_content", None)
    if sc is None:
        return

    for kind, field in (("input", "input_transcription"), ("output", "output_transcription")):
        tr = getattr(sc, field, None)
        if tr is not None and tr.text:
            t = time.monotonic() - start
            finished = bool(tr.finished)
            transcript_log.append((t, kind, tr.text, finished))
            print(
                f"{elapsed_prefix(start)} [{label}] {kind}_transcription: "
                f"{tr.text!r} finished={finished}",
                flush=True,
            )


async def receiver(session, start: float, stop: asyncio.Event, label: str,
                    audio_buf: bytearray, transcript_log: list) -> None:
    """Consume session.receive(), collecting audio bytes and transcripts.

    `session.receive()` is an async generator that ends when one
    interaction/turn completes (see google.genai.live.AsyncSession.receive
    and scripts/exp03_session_limits.py, which found and documented this) -
    it does NOT stay open for the life of the connection. So this polls
    receive() in an outer loop, re-invoking it whenever the inner generator
    ends, and uses asyncio.wait_for on each step so it can check `stop`
    instead of blocking forever on a message that may never come.
    """
    try:
        while not stop.is_set():
            gen = session.receive()
            try:
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(gen.__anext__(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    except StopAsyncIteration:
                        break
                    _process(msg, start, label, audio_buf, transcript_log)
            finally:
                aclose = getattr(gen, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
    except Exception as e:  # noqa: BLE001 - report, don't hide, connection errors
        print(f"{elapsed_prefix(start)} [{label}] receiver ended with exception: {e!r}", flush=True)


async def run_segment(client: genai.Client, pcm: bytes, label: str, start: float):
    """Feed `pcm` through one fresh session; return (audio_bytes, transcript_log)."""
    audio_buf = bytearray()
    transcript_log: list = []
    connect_started = time.monotonic()
    async with client.aio.live.connect(model=MODEL, config=config()) as session:
        connected_ms = (time.monotonic() - connect_started) * 1000
        print(f"{elapsed_prefix(start)} [{label}] connected in {connected_ms:.0f} ms", flush=True)

        stop = asyncio.Event()
        recv_task = asyncio.create_task(
            receiver(session, start, stop, label, audio_buf, transcript_log)
        )

        n = 0
        for i in range(0, len(pcm), BLOCK):
            block = pcm[i : i + BLOCK]
            if len(block) < BLOCK:
                block = block + b"\x00" * (BLOCK - len(block))
            await session.send_realtime_input(
                audio=types.Blob(data=block, mime_type="audio/pcm;rate=16000")
            )
            n += 1
            if REALTIME:
                await asyncio.sleep(0.1)
        print(
            f"{elapsed_prefix(start)} [{label}] sender done: {n} blocks "
            f"({n * BLOCK / 32000:.1f}s of 16kHz input)",
            flush=True,
        )

        print(f"{elapsed_prefix(start)} [{label}] waiting {TAIL_WAIT_S}s for tail...", flush=True)
        await asyncio.sleep(TAIL_WAIT_S)
        stop.set()
        await recv_task

    print(
        f"{elapsed_prefix(start)} [{label}] session closed: {len(audio_buf)} bytes "
        f"({len(audio_buf) / 48000:.2f}s of 24kHz output) collected",
        flush=True,
    )
    return bytes(audio_buf), transcript_log


def print_transcripts(title: str, transcripts: list) -> None:
    print(f"\n--- {title} transcripts ---", flush=True)
    if not transcripts:
        print("  (none)", flush=True)
    for t, kind, text, finished in transcripts:
        print(f"  [t={t:6.1f}s] {kind:6s} finished={finished!s:5s} {text!r}", flush=True)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    pcm = CLIP.read_bytes()

    split = int(SPLIT_S * 16000) * 2
    assert split % BLOCK == 0, f"SPLIT_S={SPLIT_S} does not land on a 100ms block boundary"
    print(
        f"clip: {len(pcm)} bytes ({len(pcm) / 32000:.1f}s of 16kHz input), "
        f"split at byte {split} ({SPLIT_S}s)",
        flush=True,
    )

    print("\n=== Run 1: continuous (one session, whole clip) ===", flush=True)
    start_c = time.monotonic()
    continuous_audio, continuous_transcripts = await run_segment(client, pcm, "continuous", start_c)
    (OUT / "continuous.raw").write_bytes(continuous_audio)

    print("\n=== Run 2: rotated (two fresh sessions, split at SPLIT_S) ===", flush=True)
    start_r = time.monotonic()
    audio_a, transcripts_a = await run_segment(client, pcm[:split], "rotated-A", start_r)
    audio_b, transcripts_b = await run_segment(client, pcm[split:], "rotated-B", start_r)
    rotated_audio = audio_a + audio_b
    (OUT / "rotated.raw").write_bytes(rotated_audio)
    seam_offset = len(audio_a)

    print("\n=== Summary ===", flush=True)
    print(
        f"continuous.raw: {len(continuous_audio)} bytes ({len(continuous_audio) / 48000:.2f}s)",
        flush=True,
    )
    print(
        f"rotated.raw:    {len(rotated_audio)} bytes ({len(rotated_audio) / 48000:.2f}s)"
        f"  [A={len(audio_a)} bytes / {len(audio_a) / 48000:.2f}s,"
        f" B={len(audio_b)} bytes / {len(audio_b) / 48000:.2f}s]",
        flush=True,
    )
    print(
        f"seam byte offset in rotated.raw: {seam_offset} ({seam_offset / 48000:.2f}s)",
        flush=True,
    )

    print_transcripts("continuous", continuous_transcripts)
    print_transcripts("rotated-A", transcripts_a)
    print_transcripts("rotated-B", transcripts_b)

    print("\nListen to both:", flush=True)
    print(
        f"  pw-cat --playback --raw --rate 24000 --channels 1 --format s16 {OUT}/continuous.raw",
        flush=True,
    )
    print(
        f"  pw-cat --playback --raw --rate 24000 --channels 1 --format s16 {OUT}/rotated.raw",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
