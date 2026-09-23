"""Experiment 5: is accented English detected as English?

`gemini-3.5-live-translate-preview` auto-detects the source language - there
is no field anywhere in `LiveConnectConfig` / `TranslationConfig` to declare
"the input is English." Only the target is configurable. The model card
warns that "language detection can struggle with non-native accents, similar
languages, or rapid language switches." In this project's real use, the OUT
direction sends the user's own accented English with `target_language_code`
set to the remote party's language (here, Russian). If detection misfires
there, the remote party hears a mistranslation or silence, with nothing on
screen explaining why.

Three questions, answered from the transcript/audio evidence this script
collects:

1. Is the source detected as English? `input_transcription` is the
   evidence - coherent English text means detection worked; Russian text,
   transliterated nonsense, or another language means it failed.
2. How badly are proper nouns / technical terms mangled? This model exposes
   no equivalent of sidetap's `--phrase` hints, so names are entirely at the
   model's mercy. Answered by inspecting the printed transcript for named
   entities and how they came out.
3. Does the translation actually happen? Confirmed two ways: (a)
   `output_transcription` text arrives and is Russian, (b) actual
   speech-bearing output audio arrives (not just an occasional connection-
   priming blip - see docs/experiments/02-voice-stability.md's "Continuous
   output stream" finding: this model holds its output byte channel open
   almost continuously regardless of whether it has anything to say, so byte
   presence alone is not evidence of translation; a peak-based per-frame
   speech test, threshold 1000, is used instead, per exp02's measured
   >1000 nothing to say" peak of 1078 vs 75%+ of actively-translating frames
   clearing 1000).

Clip: tests/fixtures/accented_en_16k.raw - 64.0s, raw s16 16kHz mono, peak
22074 (67% FS), 93% speech density, 2 pauses >=0.4s. English spoken with a
Slavic (Russian/Ukrainian) accent, captured from a YouTube talk via
sink-monitor capture (NOT the user's own voice - see the write-up's caveat
section). Do not confuse with tests/fixtures/speech_en_16k.raw, which despite
its name holds Russian speech (see docs/experiments/02-voice-stability.md)
and is used by experiments 2 and 4.

target_language_code="ru" is used deliberately, NOT as an illustrative
default: it is the real OUT-direction configuration for this user (English
speaker, Russian-speaking counterpart), so this experiment measures the
actual production risk rather than a synthetic one.

Uses the config shape established in docs/experiments/01-connect.md:
`translation_config`, `input_audio_transcription`, `output_audio_transcription`
are top-level fields of `LiveConnectConfig`, not nested in `generation_config`
(the nested form is deprecated and silently produces a conversational agent
instead of an interpreter). Reuses exp04_pacing.py's send/receive structure:
`session.send_realtime_input(audio=types.Blob(...))`, and an outer loop
around `session.receive()` since that generator ends at each turn boundary
rather than staying open for the connection's life.

Run (in background - takes ~64s of audio plus tail):
  set -a && . ~/.config/sidetap-live.env && set +a
  nohup uv run python scripts/exp05_detection.py > /tmp/exp05.log 2>&1 &
"""

from __future__ import annotations

import array
import asyncio
import os
import pathlib
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"

IN_RATE = 16000
OUT_RATE = 24000
BLOCK = 3200  # 100 ms of 16 kHz s16 mono, matches the real send cadence
FRAME_MS = 20
OUT_FRAME_BYTES = OUT_RATE * 2 * FRAME_MS // 1000  # 960
PEAK_THRESHOLD = 1000  # exp02: worst "nothing to say" peak was 1078 over ~200s;
                        # translating frames clear this 75%+ of the time.

CLIP = pathlib.Path("tests/fixtures/accented_en_16k.raw")
TARGET_LANGUAGE = "ru"  # the real OUT-direction config for this user - not illustrative

AUDIO_OUT = pathlib.Path("docs/experiments/audio/exp05_output_ru.raw")  # gitignored

TAIL_WAIT_S = 8.0  # let the tail arrive well past the last input block, before closing


def config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=TARGET_LANGUAGE,
            echo_target_language=False,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


def elapsed_prefix(start: float) -> str:
    return f"[t={time.monotonic() - start:6.1f}s]"


def frame_peak(frame: bytes) -> int:
    samples = array.array("h")
    samples.frombytes(frame)
    return max((abs(s) for s in samples), default=0)


def pad_to_block(pcm: bytes, block: int) -> bytes:
    rem = len(pcm) % block
    if rem == 0:
        return pcm
    return pcm + b"\x00" * (block - rem)


@dataclass
class State:
    transcript_log: list = field(default_factory=list)  # (t, kind, text, finished)
    audio_buf: bytearray = field(default_factory=bytearray)
    output_leftover: bytearray = field(default_factory=bytearray)
    total_output_bytes: int = 0
    speech_frames: int = 0
    total_frames: int = 0
    first_speech_t: float | None = None
    last_speech_t: float | None = None


def process_output_chunk(data: bytes, t: float, state: State) -> None:
    state.audio_buf.extend(data)
    state.total_output_bytes += len(data)
    buf = bytes(state.output_leftover) + data
    n = len(buf) // OUT_FRAME_BYTES
    for k in range(n):
        frame = buf[k * OUT_FRAME_BYTES : (k + 1) * OUT_FRAME_BYTES]
        state.total_frames += 1
        if frame_peak(frame) > PEAK_THRESHOLD:
            state.speech_frames += 1
            if state.first_speech_t is None:
                state.first_speech_t = t
            state.last_speech_t = t
    state.output_leftover = bytearray(buf[n * OUT_FRAME_BYTES :])


def process_message(msg, start: float, state: State) -> None:
    data = getattr(msg, "data", None)
    if data:
        t = time.monotonic() - start
        process_output_chunk(data, t, state)

    sc = getattr(msg, "server_content", None)
    if sc is None:
        return

    for kind, field_name in (("input", "input_transcription"), ("output", "output_transcription")):
        tr = getattr(sc, field_name, None)
        if tr is not None and tr.text:
            t = time.monotonic() - start
            finished = bool(tr.finished)
            state.transcript_log.append((t, kind, tr.text, finished))
            print(
                f"{elapsed_prefix(start)} {kind}_transcription: {tr.text!r} finished={finished}",
                flush=True,
            )


async def receiver(session, start: float, stop: asyncio.Event, state: State) -> None:
    """See exp02/exp03/exp04: session.receive() ends per-turn, not per-connection."""
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
                    process_message(msg, start, state)
            finally:
                aclose = getattr(gen, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
    except Exception as e:  # noqa: BLE001 - report, don't hide, connection errors
        print(f"{elapsed_prefix(start)} receiver ended with exception: {e!r}", flush=True)


async def sender(session, start: float, sent_pcm: bytes) -> float:
    """Feed sent_pcm at wall-clock speed. Returns the elapsed time (since
    `start`) at which sending finished."""
    n = 0
    for i in range(0, len(sent_pcm), BLOCK):
        block = sent_pcm[i : i + BLOCK]
        await session.send_realtime_input(
            audio=types.Blob(data=block, mime_type="audio/pcm;rate=16000")
        )
        n += 1
        await asyncio.sleep(0.1)
    finished_t = time.monotonic() - start
    print(
        f"{elapsed_prefix(start)} sender done: {n} blocks "
        f"({n * BLOCK / (IN_RATE * 2):.1f}s of {IN_RATE}Hz input)",
        flush=True,
    )
    return finished_t


def segment_turns(events: list[tuple[float, str, bool]]) -> list[dict]:
    """events: [(t, text, finished), ...] for ONE kind, in arrival order.

    A turn is a maximal run of events ending in a finished=True event (or,
    for a trailing partial turn with no finished event, ending at the last
    event seen - marked unfinished). Reconstructs the turn's full text by
    concatenating its events' text in arrival order (this model's
    input/output transcription events observed in exp02/exp04 are
    non-overlapping fragments of the utterance, not a repeated cumulative
    string - concatenation is the correct reconstruction).
    """
    turns: list[dict] = []
    start_t = None
    end_t = None
    pieces: list[str] = []
    for t, text, finished in events:
        if start_t is None:
            start_t = t
        end_t = t
        pieces.append(text)
        if finished:
            turns.append({"start": start_t, "end": end_t, "text": "".join(pieces), "unfinished": False})
            start_t = None
            pieces = []
    if pieces:
        turns.append({"start": start_t, "end": end_t, "text": "".join(pieces), "unfinished": True})
    return turns


def analyze(state: State) -> None:
    print("\n=== Reconstructed turns ===", flush=True)
    input_events = [(t, text, fin) for (t, kind, text, fin) in state.transcript_log if kind == "input"]
    output_events = [(t, text, fin) for (t, kind, text, fin) in state.transcript_log if kind == "output"]

    input_turns = segment_turns(input_events)
    output_turns = segment_turns(output_events)

    print(f"\n--- input_transcription turns ({len(input_turns)}) ---", flush=True)
    for i, turn in enumerate(input_turns):
        print(
            f"  [{i}] t={turn['start']:.1f}-{turn['end']:.1f}s "
            f"unfinished={turn['unfinished']} text={turn['text']!r}",
            flush=True,
        )

    print(f"\n--- output_transcription turns ({len(output_turns)}) ---", flush=True)
    for i, turn in enumerate(output_turns):
        print(
            f"  [{i}] t={turn['start']:.1f}-{turn['end']:.1f}s "
            f"unfinished={turn['unfinished']} text={turn['text']!r}",
            flush=True,
        )

    print("\n=== Output audio (does translation actually happen?) ===", flush=True)
    total_out_s = state.total_output_bytes / (OUT_RATE * 2)
    speech_s = state.speech_frames * (FRAME_MS / 1000.0)
    frac = state.speech_frames / state.total_frames if state.total_frames else 0.0
    print(
        f"total output bytes: {state.total_output_bytes} ({total_out_s:.2f}s of 24kHz audio)\n"
        f"frames: {state.total_frames} total, {state.speech_frames} speech-bearing "
        f"(peak>{PEAK_THRESHOLD}) = {speech_s:.2f}s ({frac:.1%} of output frames)\n"
        f"first speech-bearing frame: "
        f"{'t=' + format(state.first_speech_t, '.2f') + 's' if state.first_speech_t is not None else 'never'}\n"
        f"last speech-bearing frame: "
        f"{'t=' + format(state.last_speech_t, '.2f') + 's' if state.last_speech_t is not None else 'never'}",
        flush=True,
    )


async def main() -> None:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)
    pcm = CLIP.read_bytes()
    sent_pcm = pad_to_block(pcm, BLOCK)
    total_input_s = len(sent_pcm) / (IN_RATE * 2)
    print(
        f"clip: {CLIP} - {len(pcm)} bytes ({len(pcm)/(IN_RATE*2):.2f}s), "
        f"padded to {len(sent_pcm)} bytes ({total_input_s:.2f}s) for sending; "
        f"target_language_code={TARGET_LANGUAGE!r}",
        flush=True,
    )

    state = State()

    start = time.monotonic()
    connect_started = time.monotonic()
    async with client.aio.live.connect(model=MODEL, config=config()) as session:
        connected_ms = (time.monotonic() - connect_started) * 1000
        print(f"{elapsed_prefix(start)} connected in {connected_ms:.0f} ms", flush=True)

        stop = asyncio.Event()
        recv_task = asyncio.create_task(receiver(session, start, stop, state))

        send_finished_t = await sender(session, start, sent_pcm)

        print(f"{elapsed_prefix(start)} waiting {TAIL_WAIT_S}s for tail...", flush=True)
        await asyncio.sleep(TAIL_WAIT_S)
        stop.set()
        await recv_task

    tail_end_t = time.monotonic() - start
    print(
        f"\n{elapsed_prefix(start)} session closed. send_finished_t={send_finished_t:.2f}s, "
        f"tail_end_t={tail_end_t:.2f}s",
        flush=True,
    )

    AUDIO_OUT.parent.mkdir(parents=True, exist_ok=True)
    AUDIO_OUT.write_bytes(bytes(state.audio_buf))
    print(f"wrote {len(state.audio_buf)} bytes of output audio to {AUDIO_OUT} (gitignored)", flush=True)

    analyze(state)


if __name__ == "__main__":
    asyncio.run(main())
