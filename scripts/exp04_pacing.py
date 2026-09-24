"""Experiment 4: does the translation fall progressively behind the speaker?

The plan's original measurement for this task was the ratio of output-audio
seconds to input-audio seconds, on the theory that a ratio persistently above
1.0 means backlog grows without bound. Experiments 2 and 3 showed that
measurement is meaningless here: this model emits a continuous 24 kHz output
byte stream almost the entire time a session is open, whether or not it is
translating anything (exp03: 587.5s of audio-shaped bytes out of 591.3s of
silence-fed session time; exp02: 0.04% of 20ms frames peaked >1000 when there
was nothing to translate, vs 75.5% when actively translating). A raw
bytes-out/bytes-in ratio sits near 1.0 regardless of whether the model is
keeping pace, so it can't answer the real question.

This experiment measures the real question two ways instead:

1. Transcription lag over time. `input_transcription` (what the model heard)
   and `output_transcription` (what it said) arrive as separate timestamped
   event streams. Two lag proxies are computed from them, cross-checked
   against each other:
     a. turn lag - pair the Nth *finished* input utterance with the Nth
        finished output utterance (in arrival order) and take the gap
        between their finish timestamps. Semantically clean (an utterance
        boundary is unambiguous regardless of whether `text` deltas are
        cumulative or incremental) but coarse if there are few pauses.
     b. event lag - pair the Nth input_transcription event overall with the
        Nth output_transcription event overall, purely by arrival order, no
        turn concept needed. Denser, useful as a cross-check.
   Both are reported per matched pair, bucketed into 10s windows by the
   input side's own timestamp, plus a first-half vs second-half comparison
   to say plainly whether the lag grows or stays flat.

2. Speech-bearing seconds, output vs input, per 10s bucket. Both streams are
   sliced into 20ms frames and a frame counts as "speech" if its peak sample
   exceeds 1000 (justified by exp02's measured gap: worst-case "nothing to
   say" peak was 1078 across ~200s; actively-translating frames cleared 1000
   in 75%+ of frames). This is a total-bytes-immune measurement: silence
   frames, however many arrive, never count. A per-bucket and *cumulative*
   output/input ratio both stay near 1.0 if pacing holds; a cumulative ratio
   that climbs bucket over bucket means the translated speech is genuinely
   outrunning its source, which is what would make a backlog accumulate.

After the input clip is fully sent, the receiver keeps running until
output speech goes quiet for TAIL_SPEECH_IDLE_S (not just "no bytes" -
exp02/exp03 established that byte silence never really happens) or
TAIL_MAX_S elapses. How long real speech keeps draining out after the
input stops is itself a direct, model-agnostic signal of backlog: a
system keeping pace drains in about one utterance's worth of tail; a
system building backlog keeps talking long after the speaker stopped.

Uses the config shape established in docs/experiments/01-connect.md:
`translation_config`, `input_audio_transcription`, `output_audio_transcription`
are top-level fields of `LiveConnectConfig`. Reuses exp02's receive-loop
structure (session.receive() is a per-turn generator, not a connection-long
one - it must be re-invoked in an outer loop). target_language_code="en" is
used deliberately: the clip's source is Russian (see docs/experiments/
02-voice-stability.md), and a "ru" target is a same-language no-op that
suppresses spoken output almost entirely.

Run (in background - takes ~96s of audio plus tail, so a few minutes total):
  set -a && . ~/.config/sidetap-live.env && set +a
  uv run python scripts/exp04_pacing.py
"""

from __future__ import annotations

import array
import asyncio
import os
import pathlib
import time
from collections import defaultdict
from dataclasses import dataclass, field

from google import genai
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"

IN_RATE = 16000
OUT_RATE = 24000
BLOCK = 3200  # 100 ms of 16 kHz s16 mono, matches the real send cadence
FRAME_MS = 20
IN_FRAME_BYTES = IN_RATE * 2 * FRAME_MS // 1000  # 640
OUT_FRAME_BYTES = OUT_RATE * 2 * FRAME_MS // 1000  # 960
PEAK_THRESHOLD = 1000  # exp02: worst "nothing to say" peak was 1078 over ~200s;
                        # translating frames clear this 75%+ of the time.
BUCKET_S = 10.0

CLIP = pathlib.Path("tests/fixtures/speech_en_16k.raw")

MIN_TAIL_S = 4.0        # wait at least this long after sending stops, always
TAIL_SPEECH_IDLE_S = 3.0  # ...then keep waiting until speech has been quiet this long
TAIL_MAX_S = 45.0        # ...or give up here regardless (LAG_CAP_S is 30s)


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
    # Measurement 2: per-bucket 20ms-frame counts.
    input_bucket_speech: dict = field(default_factory=lambda: defaultdict(int))
    input_bucket_total: dict = field(default_factory=lambda: defaultdict(int))
    output_bucket_speech: dict = field(default_factory=lambda: defaultdict(int))
    output_bucket_total: dict = field(default_factory=lambda: defaultdict(int))
    output_leftover: bytearray = field(default_factory=bytearray)
    total_output_bytes: int = 0
    first_output_byte_t: float | None = None
    last_output_speech_t: float | None = None
    # Measurement 1: raw transcription events (t, kind, text, finished).
    transcript_log: list = field(default_factory=list)


def precompute_input_frames(sent_pcm: bytes, state: State) -> None:
    """Input send timing is deterministic (realtime feed from t=0), so each
    20ms input frame's nominal time == its position in the clip. No need to
    watch the sender to know when a given input frame's speech 'happened'.
    """
    n = len(sent_pcm) // IN_FRAME_BYTES
    for k in range(n):
        frame = sent_pcm[k * IN_FRAME_BYTES : (k + 1) * IN_FRAME_BYTES]
        t = k * (FRAME_MS / 1000.0)
        bucket = int(t // BUCKET_S)
        state.input_bucket_total[bucket] += 1
        if frame_peak(frame) > PEAK_THRESHOLD:
            state.input_bucket_speech[bucket] += 1


def process_output_chunk(data: bytes, t: float, state: State) -> None:
    if state.first_output_byte_t is None:
        state.first_output_byte_t = t
    state.total_output_bytes += len(data)
    buf = bytes(state.output_leftover) + data
    n = len(buf) // OUT_FRAME_BYTES
    bucket = int(t // BUCKET_S)
    for k in range(n):
        frame = buf[k * OUT_FRAME_BYTES : (k + 1) * OUT_FRAME_BYTES]
        state.output_bucket_total[bucket] += 1
        if frame_peak(frame) > PEAK_THRESHOLD:
            state.output_bucket_speech[bucket] += 1
            state.last_output_speech_t = t
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
    """See exp02/exp03: session.receive() ends per-turn, not per-connection."""
    try:
        while not stop.is_set():
            gen = session.receive()
            try:
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(gen.__anext__(), timeout=0.5)
                    except TimeoutError:
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


async def wait_for_tail(start: float, state: State, send_finished_t: float) -> float:
    """Keep the session open until output speech has been quiet for
    TAIL_SPEECH_IDLE_S past send_finished_t, or TAIL_MAX_S elapses -
    whichever comes first. Returns the elapsed time (since `start`) at
    which waiting stopped, so the caller can report how long the tail ran.
    """
    while True:
        now = time.monotonic() - start
        since_send = now - send_finished_t
        last_speech = state.last_output_speech_t
        idle = now - last_speech if last_speech is not None and last_speech >= send_finished_t else since_send
        if since_send >= MIN_TAIL_S and idle >= TAIL_SPEECH_IDLE_S:
            print(
                f"{elapsed_prefix(start)} tail drained: speech idle for {idle:.1f}s "
                f"({since_send:.1f}s after send finished)",
                flush=True,
            )
            return now
        if since_send >= TAIL_MAX_S:
            print(
                f"{elapsed_prefix(start)} tail wait hit TAIL_MAX_S={TAIL_MAX_S}s "
                f"without going quiet - stopping anyway",
                flush=True,
            )
            return now
        await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def segment_turns(events: list[tuple[float, str, bool]]) -> list[dict]:
    """events: [(t, text, finished), ...] for ONE kind, in arrival order.

    A turn is a maximal run of events ending in a finished=True event (or,
    for a trailing partial turn with no finished event, ending at the last
    event seen - marked unfinished).
    """
    turns: list[dict] = []
    start_t = None
    end_t = None
    n = 0
    for t, _text, finished in events:
        if start_t is None:
            start_t = t
        end_t = t
        n += 1
        if finished:
            turns.append({"start": start_t, "end": end_t, "n": n, "unfinished": False})
            start_t = None
            n = 0
    if start_t is not None:
        turns.append({"start": start_t, "end": end_t, "n": n, "unfinished": True})
    return turns


def half_split_trend(pairs: list[tuple[float, float]]) -> tuple[float, float, float]:
    """pairs: [(anchor_t, lag_s), ...] sorted by anchor_t.

    Returns (first_half_mean_lag, second_half_mean_lag, delta) - the
    simplest possible "does it grow" test, robust to a handful of outliers.
    """
    if not pairs:
        return (float("nan"), float("nan"), float("nan"))
    mid = len(pairs) // 2
    first = [lag for _, lag in pairs[:mid]] or [lag for _, lag in pairs]
    second = [lag for _, lag in pairs[mid:]]
    fm = sum(first) / len(first)
    sm = sum(second) / len(second)
    return fm, sm, sm - fm


def analyze(state: State, total_input_s: float) -> None:
    print("\n=== Measurement 1: transcription lag ===", flush=True)

    input_events = [(t, text, fin) for (t, kind, text, fin) in state.transcript_log if kind == "input"]
    output_events = [(t, text, fin) for (t, kind, text, fin) in state.transcript_log if kind == "output"]

    # 1a. Turn lag (paired by finished-flag order).
    input_turns = segment_turns(input_events)
    output_turns = segment_turns(output_events)
    n_pairs = min(len(input_turns), len(output_turns))
    print(
        f"input turns: {len(input_turns)} (unfinished trailing: "
        f"{input_turns[-1]['unfinished'] if input_turns else 'n/a'}), "
        f"output turns: {len(output_turns)} (unfinished trailing: "
        f"{output_turns[-1]['unfinished'] if output_turns else 'n/a'})",
        flush=True,
    )
    turn_pairs = []
    print(f"{'#':>3} {'in_end':>8} {'out_end':>8} {'lag_s':>8}", flush=True)
    for i in range(n_pairs):
        a, b = input_turns[i], output_turns[i]
        lag = b["end"] - a["end"]
        turn_pairs.append((a["end"], lag))
        print(f"{i:>3} {a['end']:>8.2f} {b['end']:>8.2f} {lag:>8.2f}", flush=True)
    fm, sm, delta = half_split_trend(turn_pairs)
    print(
        f"turn lag: first-half mean={fm:.2f}s, second-half mean={sm:.2f}s, "
        f"delta={delta:+.2f}s ({'GROWING' if delta > 0.3 else 'flat' if abs(delta) <= 0.3 else 'SHRINKING'})",
        flush=True,
    )

    # 1b. Event lag (paired by arrival order, no turn concept).
    n_ev = min(len(input_events), len(output_events))
    event_pairs = []
    for i in range(n_ev):
        ta = input_events[i][0]
        tb = output_events[i][0]
        event_pairs.append((ta, tb - ta))
    fm2, sm2, delta2 = half_split_trend(event_pairs)
    print(
        f"event lag ({n_ev} pairs, {len(input_events)} input events / "
        f"{len(output_events)} output events): first-half mean={fm2:.2f}s, "
        f"second-half mean={sm2:.2f}s, delta={delta2:+.2f}s "
        f"({'GROWING' if delta2 > 0.3 else 'flat' if abs(delta2) <= 0.3 else 'SHRINKING'})",
        flush=True,
    )

    # Bucket both by the input-side anchor time, for a per-10s view.
    max_bucket = int(total_input_s // BUCKET_S) + 1
    print(f"\n{'bucket_s':>10} {'turn_lag_avg':>14} {'n':>3} {'event_lag_avg':>15} {'n':>3}", flush=True)
    for b in range(max_bucket + 1):
        lo, hi = b * BUCKET_S, (b + 1) * BUCKET_S
        tl = [lag for t, lag in turn_pairs if lo <= t < hi]
        el = [lag for t, lag in event_pairs if lo <= t < hi]
        tl_avg = f"{sum(tl)/len(tl):.2f}" if tl else "  n/a"
        el_avg = f"{sum(el)/len(el):.2f}" if el else "  n/a"
        print(f"{lo:>6.0f}-{hi:<3.0f} {tl_avg:>14} {len(tl):>3} {el_avg:>15} {len(el):>3}", flush=True)

    print("\n=== Measurement 2: speech-bearing seconds, output vs input ===", flush=True)
    max_bucket2 = max(
        list(state.input_bucket_total.keys()) + list(state.output_bucket_total.keys()) + [0]
    )
    cum_in = 0.0
    cum_out = 0.0
    print(
        f"{'bucket_s':>10} {'in_speech_s':>11} {'out_speech_s':>12} {'ratio':>7} "
        f"{'cum_in_s':>9} {'cum_out_s':>10} {'cum_ratio':>9}",
        flush=True,
    )
    for b in range(max_bucket2 + 1):
        lo, hi = b * BUCKET_S, (b + 1) * BUCKET_S
        in_s = state.input_bucket_speech.get(b, 0) * (FRAME_MS / 1000.0)
        out_s = state.output_bucket_speech.get(b, 0) * (FRAME_MS / 1000.0)
        cum_in += in_s
        cum_out += out_s
        ratio = f"{out_s/in_s:.2f}" if in_s > 0 else ("  -" if out_s == 0 else " inf")
        cum_ratio = f"{cum_out/cum_in:.2f}" if cum_in > 0 else "  -"
        print(
            f"{lo:>6.0f}-{hi:<3.0f} {in_s:>11.2f} {out_s:>12.2f} {ratio:>7} "
            f"{cum_in:>9.2f} {cum_out:>10.2f} {cum_ratio:>9}",
            flush=True,
        )

    total_in_speech = sum(state.input_bucket_speech.values()) * (FRAME_MS / 1000.0)
    total_out_speech = sum(state.output_bucket_speech.values()) * (FRAME_MS / 1000.0)
    total_out_all = state.total_output_bytes / (OUT_RATE * 2)
    print(
        f"\ntotals: input speech-bearing = {total_in_speech:.2f}s, "
        f"output speech-bearing = {total_out_speech:.2f}s, "
        f"ratio = {total_out_speech/total_in_speech:.3f}, "
        f"(raw output bytes = {total_out_all:.2f}s of audio, for reference only)",
        flush=True,
    )


async def main() -> None:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)
    pcm = CLIP.read_bytes()
    sent_pcm = pad_to_block(pcm, BLOCK)
    total_input_s = len(sent_pcm) / (IN_RATE * 2)
    print(
        f"clip: {len(pcm)} bytes ({len(pcm)/(IN_RATE*2):.2f}s), "
        f"padded to {len(sent_pcm)} bytes ({total_input_s:.2f}s) for sending",
        flush=True,
    )

    state = State()
    precompute_input_frames(sent_pcm, state)

    start = time.monotonic()
    connect_started = time.monotonic()
    async with client.aio.live.connect(model=MODEL, config=config()) as session:
        connected_ms = (time.monotonic() - connect_started) * 1000
        print(f"{elapsed_prefix(start)} connected in {connected_ms:.0f} ms", flush=True)

        stop = asyncio.Event()
        recv_task = asyncio.create_task(receiver(session, start, stop, state))

        send_finished_t = await sender(session, start, sent_pcm)
        tail_end_t = await wait_for_tail(start, state, send_finished_t)
        stop.set()
        await recv_task

    print(
        f"\n{elapsed_prefix(start)} session closed. send_finished_t={send_finished_t:.2f}s, "
        f"tail_end_t={tail_end_t:.2f}s, tail duration={tail_end_t - send_finished_t:.2f}s",
        flush=True,
    )
    if state.last_output_speech_t is not None:
        print(
            f"last speech-bearing output frame arrived at t={state.last_output_speech_t:.2f}s "
            f"({state.last_output_speech_t - send_finished_t:+.2f}s relative to send finish)",
            flush=True,
        )
    else:
        print("no speech-bearing output frame ever arrived (peak never exceeded threshold)", flush=True)

    analyze(state, total_input_s)


if __name__ == "__main__":
    asyncio.run(main())
