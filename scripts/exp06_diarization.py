"""Experiment 6: does this model label the remote speakers, and what does it cost?

The IN direction taps the messenger's OUTPUT port, where Zoom/Viber have
already mixed every remote participant into one stream. There is no per-person
separation to be had from PipeWire, so if a three-way call is ever to be
readable, the model has to tell the voices apart itself.

`google-genai` 2.24.0 exposes exactly that:

    types.AudioTranscriptionConfig(diarization=True)
    types.Transcription.speaker_label      # 'e.g. "spk_1", "spk_2"'

The field existing proves nothing here. Four of this project's six
measurements contradicted Google's own documentation, and `translation_config`
nested under `generation_config` type-checks, connects, emits only a
DeprecationWarning and silently produces a chatbot. So this script asks the
API rather than the docs.

Three questions:

1. **Does `speaker_label` arrive at all** on `gemini-3.5-live-translate-preview`?
   It is a translation-specialised model and `diarization` lives on the shared
   AudioTranscriptionConfig used by every Live model, so it may simply be
   ignored.
2. **Does it track the right voice?** Phase A feeds an alternating two-voice
   clip built from the two existing fixtures, so the true speaker at every
   moment is known. Agreement is reported as: of the turn boundaries in the
   input, how many produced a label change, and how many label changes
   happened where no boundary was.
3. **What does it cost?** Phase B feeds the same clip with diarization off and
   compares first-output offset and output/input speech ratio against phase A.
   Every latency figure the design rests on - lag flat at 0.24 -> 0.25 s
   across 96 s (experiment 4) - was measured without it.

And the one that matters most in production:

4. **Do labels survive a rotation?** (`--rotation`, ~11 minutes.) This program
   opens a new session every ~9 minutes, make-before-break, passing the
   resumption handle. If "spk_1" after the seam is not the same person as
   "spk_1" before it, labels are stable only inside a 9-minute window and an
   hour-long call carries six independent labelings with nothing to reconcile
   them - which is worse than no labels, because it looks authoritative and
   is not.

Run:
    GEMINI_API_KEY=... uv run python scripts/exp06_diarization.py
    GEMINI_API_KEY=... uv run python scripts/exp06_diarization.py --rotation

Writes nothing but stdout. Record the findings in
docs/experiments/06-diarization.md.
"""

import argparse
import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from google import genai
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"
TARGET = "ru"

RATE = 16000
BLOCK_BYTES = 3200                      # 100 ms of s16 mono at 16 kHz
TURN_S = 6.0                            # one speaker's stretch in the built clip
TURNS = 8
TAIL_WAIT_S = 8.0

ROOT = Path(__file__).resolve().parent.parent
# Two clearly different voices. speech_en_16k.raw is Russian speech despite
# its name (see docs/experiments/02-voice-stability.md); that is a confound
# for translation but not for "are these two different people", which is what
# phase A measures.
VOICE_A = ROOT / "tests/fixtures/accented_en_16k.raw"
VOICE_B = ROOT / "tests/fixtures/speech_en_16k.raw"

# GoAway arrives ~540 s in. Run past it.
ROTATION_RUN_S = 660.0


def config(*, diarize: bool, handle: str | None = None) -> types.LiveConnectConfig:
    """The production shape from live.py, plus the flag under test."""
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=TARGET,
            echo_target_language=False,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(
            diarization=True if diarize else None
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


def build_alternating_clip() -> tuple[bytes, list[tuple[float, str]]]:
    """A -> B -> A -> ... and the ground truth of who is speaking when.

    Built rather than recorded because no two-speaker fixture exists, and a
    real one would need two people in a room. The turns are hard-cut, which
    is easier than a real call: nobody talks over anybody. If diarization
    cannot manage this, it certainly cannot manage overlap.
    """
    a = VOICE_A.read_bytes()
    b = VOICE_B.read_bytes()
    turn_bytes = int(TURN_S * RATE * 2)

    pcm = bytearray()
    truth: list[tuple[float, str]] = []
    offsets = {"A": 0, "B": 0}
    for i in range(TURNS):
        who = "A" if i % 2 == 0 else "B"
        src = a if who == "A" else b
        start = offsets[who]
        chunk = src[start : start + turn_bytes]
        if len(chunk) < turn_bytes:            # wrap rather than pad with silence
            chunk = chunk + src[: turn_bytes - len(chunk)]
            offsets[who] = turn_bytes - len(chunk)
        else:
            offsets[who] = start + turn_bytes
        truth.append((len(pcm) / (RATE * 2), who))
        pcm.extend(chunk)
    return bytes(pcm), truth


@dataclass
class Fragment:
    t: float
    text: str
    speaker: str | None


@dataclass
class Run:
    diarize: bool
    fragments: list[Fragment] = field(default_factory=list)
    first_output_t: float | None = None
    output_bytes: int = 0
    connected_ms: float = 0.0
    handles: list[str] = field(default_factory=list)
    go_away_t: float | None = None


async def receive(session, run: Run, start: float, stop: asyncio.Event) -> None:
    async for message in session.receive():
        if stop.is_set():
            return
        now = time.monotonic() - start
        data = getattr(message, "data", None)
        if data:
            run.output_bytes += len(data)
            if run.first_output_t is None:
                run.first_output_t = now
        content = getattr(message, "server_content", None)
        if content is not None:
            src = getattr(content, "input_transcription", None)
            if src is not None and getattr(src, "text", ""):
                run.fragments.append(
                    Fragment(now, src.text, getattr(src, "speaker_label", None))
                )
        go_away = getattr(message, "go_away", None)
        if go_away is not None and run.go_away_t is None:
            run.go_away_t = now
            print(f"  [{now:6.1f}s] GoAway, time_left={getattr(go_away, 'time_left', None)!r}")
        update = getattr(message, "session_resumption_update", None)
        if update is not None and getattr(update, "new_handle", None):
            if getattr(update, "resumable", False):
                run.handles.append(update.new_handle)


async def feed(session, pcm: bytes, start: float, label: str) -> None:
    """Wall-clock, 100 ms at a time - the rate the real capture path sends."""
    for i in range(0, len(pcm), BLOCK_BYTES):
        block = pcm[i : i + BLOCK_BYTES]
        if len(block) < BLOCK_BYTES:
            break
        await session.send_realtime_input(
            audio=types.Blob(data=block, mime_type="audio/pcm;rate=16000")
        )
        await asyncio.sleep(0.1)
    print(f"  [{time.monotonic() - start:6.1f}s] {label}: finished sending")


async def one_run(client, pcm: bytes, *, diarize: bool) -> Run:
    run = Run(diarize=diarize)
    start = time.monotonic()
    connect_started = time.monotonic()
    async with client.aio.live.connect(model=MODEL, config=config(diarize=diarize)) as session:
        run.connected_ms = (time.monotonic() - connect_started) * 1000
        print(f"  connected in {run.connected_ms:.0f} ms  (diarize={diarize})")
        stop = asyncio.Event()
        task = asyncio.create_task(receive(session, run, start, stop))
        await feed(session, pcm, start, f"diarize={diarize}")
        await asyncio.sleep(TAIL_WAIT_S)
        stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
    return run


def report_labels(run: Run, truth: list[tuple[float, str]]) -> None:
    labelled = [f for f in run.fragments if f.speaker]
    print(f"\n  fragments               : {len(run.fragments)}")
    print(f"  carrying a speaker_label: {len(labelled)}")
    if not labelled:
        print("  VERDICT: the model returned NO labels - diarization is not")
        print("           honoured by this model, and the flag is inert.")
        return
    labels = sorted({f.speaker for f in labelled})
    print(f"  distinct labels         : {labels}")

    # Ground truth: which built-in turn each fragment falls in.
    def true_speaker(t: float) -> str:
        who = truth[0][1]
        for at, w in truth:
            if t >= at:
                who = w
        return who

    # A fragment's timestamp is when the model SENT it, which trails the audio.
    # So this is agreement in ordering, not a frame-accurate score.
    changes_expected = len(truth) - 1
    observed = []
    prev = None
    for f in labelled:
        if f.speaker != prev:
            observed.append((f.t, f.speaker, true_speaker(f.t)))
            prev = f.speaker
    print(f"  turn boundaries in input: {changes_expected}")
    print(f"  label changes observed  : {len(observed) - 1}")
    print("  label changes, with the true speaker at that moment:")
    for t, label, who in observed:
        print(f"    [{t:6.1f}s] {label:8s}  true={who}")
    mapping: dict[str, set[str]] = {}
    for f in labelled:
        mapping.setdefault(f.speaker, set()).add(true_speaker(f.t))
    print("  label -> true speakers it covered:")
    for label, whos in sorted(mapping.items()):
        flag = "" if len(whos) == 1 else "   <-- covers BOTH, not tracking"
        print(f"    {label:8s} {sorted(whos)}{flag}")


def report_cost(on: Run, off: Run) -> None:
    print("\n=== phase B: what diarization costs ===")
    for name, run in (("diarize=True ", on), ("diarize=False", off)):
        first = f"{run.first_output_t:.2f}s" if run.first_output_t is not None else "never"
        secs = run.output_bytes / (24000 * 2)
        print(
            f"  {name}: connect {run.connected_ms:5.0f} ms   "
            f"first output {first:>7}   output audio {secs:6.1f}s   "
            f"fragments {len(run.fragments)}"
        )
    if on.first_output_t is not None and off.first_output_t is not None:
        delta = on.first_output_t - off.first_output_t
        print(f"  first-output difference : {delta:+.2f}s")
        print("  (experiment 4 measured lag flat at 0.24 -> 0.25 s WITHOUT this.")
        print("   A difference of more than a tenth or so is a reason not to")
        print("   turn it on by default.)")


async def rotation_probe(client, pcm: bytes) -> None:
    """Does a voice keep its label across a session seam?

    Runs one session past GoAway, then opens a replacement on the resumption
    handle - what interpreter.py does - and feeds it the same clip. If the
    same physical voice comes back under a different label, labels are
    session-scoped and an hour-long call has six unrelated labelings.
    """
    print("\n=== phase C: do labels survive a rotation? (~11 min) ===")
    run = Run(diarize=True)
    start = time.monotonic()
    async with client.aio.live.connect(model=MODEL, config=config(diarize=True)) as session:
        stop = asyncio.Event()
        task = asyncio.create_task(receive(session, run, start, stop))
        while time.monotonic() - start < ROTATION_RUN_S:
            await feed(session, pcm, start, "session 1")
            if run.go_away_t is not None:
                break
        stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)

    before = [f.speaker for f in run.fragments if f.speaker]
    print(f"  session 1: {len(before)} labelled fragments, labels={sorted(set(before))}")
    print(f"  GoAway at {run.go_away_t}, handles seen: {len(run.handles)}")
    if not run.handles:
        print("  no resumption handle - cannot test the seam")
        return

    run2 = Run(diarize=True)
    start2 = time.monotonic()
    async with client.aio.live.connect(
        model=MODEL, config=config(diarize=True, handle=run.handles[-1])
    ) as session:
        stop = asyncio.Event()
        task = asyncio.create_task(receive(session, run2, start2, stop))
        await feed(session, pcm, start2, "session 2 (resumed)")
        await asyncio.sleep(TAIL_WAIT_S)
        stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)

    after = [f.speaker for f in run2.fragments if f.speaker]
    print(f"  session 2: {len(after)} labelled fragments, labels={sorted(set(after))}")
    print("  The clip starts with voice A both times, so compare the FIRST")
    print("  label of each session:")
    print(f"    session 1 first label: {before[0] if before else None}")
    print(f"    session 2 first label: {after[0] if after else None}")
    if before and after:
        verdict = "STABLE" if before[0] == after[0] else "RENUMBERED across the seam"
        print(f"  VERDICT: {verdict}")


# Every Transcription-shaped field on LiveServerContent. Phase A reads only
# input_transcription, which is the one production consumes - so "no labels"
# from phase A does not by itself prove the model never sends any.
TRANSCRIPTION_FIELDS = (
    "input_transcription",
    "interim_input_transcription",
    "output_transcription",
)


async def raw_probe(client, pcm: bytes) -> None:
    """Is the label simply on a name we do not read?

    Before concluding the flag is inert and deleting it, look at every
    transcription the server sends and every attribute on each one, rather
    than at the single field production happens to consume.
    """
    print("\n=== phase D: raw probe - every transcription field, every attribute ===")
    seen: dict[str, int] = {f: 0 for f in TRANSCRIPTION_FIELDS}
    attrs_seen: dict[str, set] = {f: set() for f in TRANSCRIPTION_FIELDS}
    labels: list[tuple[str, str]] = []
    shapes: list[str] = []

    start = time.monotonic()
    async with client.aio.live.connect(model=MODEL, config=config(diarize=True)) as session:
        stop = asyncio.Event()

        async def receive_raw() -> None:
            async for message in session.receive():
                if stop.is_set():
                    return
                content = getattr(message, "server_content", None)
                if content is None:
                    continue
                for name in TRANSCRIPTION_FIELDS:
                    obj = getattr(content, name, None)
                    if obj is None:
                        continue
                    seen[name] += 1
                    present = {
                        k for k, v in (getattr(obj, "model_dump", lambda: {})() or {}).items()
                        if v is not None
                    }
                    attrs_seen[name] |= present
                    if len(shapes) < 3:
                        shapes.append(f"{name}: {sorted(present)}")
                    label = getattr(obj, "speaker_label", None)
                    if label:
                        labels.append((name, label))

        task = asyncio.create_task(receive_raw())
        await feed(session, pcm, start, "raw probe")
        await asyncio.sleep(TAIL_WAIT_S)
        stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)

    print("  messages carrying each transcription field:")
    for name, count in seen.items():
        print(f"    {name:30s} {count}")
    print("  non-None attributes ever seen on each:")
    for name, attrs in attrs_seen.items():
        print(f"    {name:30s} {sorted(attrs) or '-'}")
    print("  first few shapes:")
    for shape in shapes:
        print(f"    {shape}")
    print(f"  speaker_label found anywhere: {len(labels)}")
    for name, label in labels[:10]:
        print(f"    {name} -> {label}")
    if not labels:
        print("  VERDICT: no speaker_label on ANY transcription name.")
        print("           The model accepts diarization=True and ignores it.")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rotation", action="store_true",
                        help="also run the ~11 minute seam probe")
    parser.add_argument("--raw", action="store_true",
                        help="dump every transcription field and attribute (~1 min)")
    args = parser.parse_args()

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set")
    for path in (VOICE_A, VOICE_B):
        if not path.exists():
            raise SystemExit(f"missing fixture: {path}")

    pcm, truth = build_alternating_clip()
    print(f"built a {len(pcm) / (RATE * 2):.1f}s clip, {TURNS} turns of {TURN_S}s")
    print("ground truth:", " ".join(f"{t:.0f}s={w}" for t, w in truth))

    client = genai.Client(api_key=key)

    if args.raw and not args.rotation:
        # --raw on its own is the follow-up to a phase A that found nothing;
        # no reason to pay for A and B again.
        await raw_probe(client, pcm)
        return

    print("\n=== phase A: does speaker_label arrive, and does it track? ===")
    on = await one_run(client, pcm, diarize=True)
    report_labels(on, truth)

    off = await one_run(client, pcm, diarize=False)
    off_labelled = [f for f in off.fragments if f.speaker]
    print(f"\n  control (diarize=False): {len(off_labelled)} labelled fragments")
    print("  (anything but 0 means the flag is not what produces labels)")

    report_cost(on, off)

    if args.raw:
        await raw_probe(client, pcm)

    if args.rotation:
        await rotation_probe(client, pcm)


if __name__ == "__main__":
    asyncio.run(main())
