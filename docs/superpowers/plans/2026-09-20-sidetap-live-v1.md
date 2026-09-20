# sidetap_live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-box speech-to-speech interpreter on `gemini-3.5-live-translate-preview`, reusing sidetap's PipeWire layer, so the two systems can be compared on whether turn-free translation dissolves the half-duplex cadence.

**Architecture:** One WebSocket session per direction. Capture (ported from sidetap, unchanged) streams 16 kHz s16 mono at 100 ms into a Live session; 24 kHz s16 mono comes back and goes straight to `pw-cat`. A per-direction state machine handles session rotation at conversational pauses, idle suspension, and the ~10-minute connection cap. The duck is driven by output flow, not input.

**Tech Stack:** Python 3.13, `uv`, `google-genai`, `webrtcvad-wheels`, `textual`, `pytest`. PipeWire ≥ 0.3.60 via `pw-record`/`pw-cat`/`pw-link`/`pw-loopback`/`wpctl`.

**Spec:** `docs/superpowers/specs/2026-09-20-sidetap-live-design.md`

---

## Execution status — as of 2026-09-20, branch `sidetap-live-v1`

**156 tests pass** with no audio hardware, no network and no credentials.

| Task | State |
|---|---|
| 1 (Steps 1–3, skeleton) | done — `google-genai` resolved to **2.24.0** |
| 1 (Steps 4–7), 2, 3, 4, 5, 6 | **blocked**: need `GEMINI_API_KEY` and `tests/fixtures/speech_en_16k.raw` |
| 7, 8, 9, 10, 11 — Phase 1 port | done |
| 12 `activity.py`, 15 `cost.py`, 16 `preroll.py`, 23 `OverlapWatch` | done |
| 13, 14, 17–22, 24–30 | **held behind the Task 6 checkpoint** |

Phase 1 was run **before** Phase 0, deviating from the ordering above. That gate exists to stop Phases 2+ being built on an unmeasured seam design; Phase 1 is a mechanical copy of the PipeWire layer that no experiment outcome can invalidate. Tasks 12, 15, 16 and 23 were run for the same reason — a ring buffer, a rate table, a VAD wrapper and a time integral depend on none of the six measurements.

**What each held task is actually waiting on**, so the gate is not mistaken for caution:

- **13, 14** — the SDK's real config shape. The `build_config` and `parse_message` code in this plan was written from the REST documentation (`translationConfig`, `targetLanguageCode`, `inputAudioTranscription`). `google-genai` 2.24.0 may name or nest these differently, and `parse_message` additionally depends on where transcription fields sit on a message object. Task 1 Step 5 and Task 5 Step 3 settle both.
- **17, 18** — experiment 4's output pacing. Whether the input/output ratio exceeds 1.0 decides if `--lag-cap` is load-bearing or a safety valve that never fires.
- **19–21** — experiment 2's seam verdict. The model card warns voices may shift after long pauses and this design rotates *at* pauses. If the voice changes at the seam, the state machine these tasks build is the wrong one and the spec's *Session continuity* decision reopens.

**Two amendments already made to this plan during execution**, both committed: the package-rename preamble (the `sed` misses `monkeypatch` dotted-path string literals, and must not touch PipeWire node names), and Task 8's "three Protocols" typo for four.

**One decision taken that this plan did not anticipate**, applied in Task 10: anything this process creates in the PipeWire graph gets this program's name — `sidetap_live_duck`, `sidetap_live.<track>.<uuid>` capture nodes, `~/.local/state/sidetap_live/` journal — while the permanent shared virtual mic keeps sidetap's (`sidetap_virtmic`, `sidetap_tts_sink`, `90-sidetap-mic.conf`, all byte-identical). Without this the head-to-head produces two programs whose graph nodes cannot be told apart, and either program's `doctor --repair` could tear down the other's live duck. `recorder.py` is consequently no longer byte-identical to sidetap's.

**One external change to watch:** `sidetap` gained 17 commits on a branch `streaming-playout` during this session. Task 18's premise — "sidetap queues whole utterances" — describes sidetap's `main`, not that branch. If it lands, re-read sidetap's `playout.py` and update both Task 18 and the spec's *Playout and the duck* comparison before implementing.

---

## Read this before Task 1

**The source repository is `/home/vvsosed/Documents/repo2/sidetap`.** It is referred to below as `$SIDETAP`. Set it once per shell:

```bash
export SIDETAP=/home/vvsosed/Documents/repo2/sidetap
```

Never modify `$SIDETAP` except in Task 31, which is explicitly cross-repo.

**Three conventions are non-negotiable, because the ported code depends on them:**

1. **`uv` only.** Never `pip install`, never activate `.venv` by hand. `uv run` does both, from the lockfile.
2. **Every subprocess, socket and clock sits behind a `Protocol` in `ports.py`**, with one real implementation in `adapters.py` and one fake in `tests/conftest.py`. The whole suite must run with no audio hardware, no network and no API key.
3. **Identify PipeWire nodes by `object.serial` — except `wpctl`, which resolves against `object.id`.** Conflating them makes the duck silently never close. See the `VolumeControl` docstring in `ports.py`.

**Do not run `gcloud auth ...`.** This machine is attached to a live GCP project and re-authenticating has broken it before. This project does not use GCP credentials at all — it uses `GEMINI_API_KEY`.

## Appending tests to a ported file: check the function names first

When a task says "append to `tests/test_x.py`", the file may already contain a
test of the same name from the port. Python does not error on two top-level
`def`s with the same name — the second rebinds the first, and **pytest silently
collects only the last one.** The earlier test vanishes from the suite while
everything still reports PASS.

This actually happened: Task 13's `test_fakes_satisfy_their_protocols` collided
with the ported one in `tests/test_ports.py`, and appending it verbatim dropped
the original Protocol-conformance test — a silent loss of coverage that no
failure would have surfaced. The new one was renamed to
`test_session_fakes_satisfy_their_protocols`.

Before appending, run:

```bash
grep -oE '^def (test_[a-z0-9_]+)' tests/test_x.py | sort | uniq -d
```

after the append, and confirm it prints nothing. Rename the NEW test if it
clashes; never delete the existing one to make room.

The remaining tasks that append to ported test files — 22, 24, 25, 26, 27 —
have been checked against their upstream counterparts and are collision-free
as written.

## The package rename is not just imports — read this before any port task

Every port task below runs a `sed` over `from sidetap.` / `import sidetap`. **That sed is necessary but not sufficient.** Two other things carry the string `sidetap` and they must be treated differently from each other:

1. **Dotted-path string literals**, chiefly `monkeypatch.setattr("sidetap.adapters.something", ...)`. These are import paths written as strings, so the sed's `\bfrom sidetap\.` anchor never sees them. Left alone they raise `ModuleNotFoundError` at test time. **These must be renamed to `sidetap_live.`.** Found in `test_adapters.py`, and still to come in `test_doctor.py`, `test_routing.py` and `test_cli.py`.

2. **PipeWire node names**, e.g. `f"sidetap.{spec.track}.{uuid}"` in `recorder.py` and the `"sidetap.remote.deadbeef"` fixtures in `test_tap.py`/`test_recorder.py`. These are *runtime identifiers in the audio graph*, not import paths. Task 10 decides what this program calls itself in the graph; until then, do not touch them.

So after running the prescribed sed, always run:

```bash
grep -rnE '["'"'"']sidetap\.' sidetap_live/ tests/
```

and **classify every hit by hand**. A blind second sed would rename the node names too and break the recorder and tap tests for the wrong reason.

---

## File Structure

### `sidetap_live/` — the application

| File | Responsibility | Origin |
|---|---|---|
| `types.py` | value types and constants; stdlib only | ported, trimmed (Task 7) |
| `ports.py` | every `Protocol`; one real impl, one fake each | ported + `InterpreterSession` (Tasks 9, 13) |
| `graph.py` | parse `pw-dump` into `PwGraph`; pure, never spawns | ported verbatim (Task 8) |
| `recorder.py` | build `pw-record` argv, frame stdout into blocks | ported verbatim (Task 8) |
| `tap.py` | `AppTap` — link a matching application's ports | ported verbatim (Task 8) |
| `capture.py` | own recorders, queues, capture threads | ported verbatim (Task 8) |
| `routing.py` | duck loopback lifecycle, re-route, journal, restore | ported, state dir renamed (Task 10) |
| `adapters.py` | the real ports — every subprocess on the audio path | ported, trimmed (Task 9) |
| `metrics.py` | backlog, offset, overlap, rotations, cost | ported, reshaped (Tasks 11, 24) |
| `activity.py` | `SpeechActivity` — observes speech/silence, gates nothing | new, from `vad.py` (Task 12) |
| `live.py` | `GeminiLiveSession` — the only thing that talks to Gemini | new (Task 14) |
| `cost.py` | audio-token rates as configuration | rewritten (Task 15) |
| `preroll.py` | `PreRoll` — bounded ring of recent capture | new (Task 16) |
| `playout.py` | chunk queue, writer thread, `DuckControl` with hysteresis | rewritten (Tasks 17, 18) |
| `interpreter.py` | `DirectionInterpreter` — state machine, send and receive | new (Tasks 19–21) |
| `transcript.py` | event-stream `.jsonl` + interleaved `.md` | ported, reshaped (Task 22) |
| `tui.py` | Textual dashboard, hotkeys | ported, headline row replaced (Task 24) |
| `doctor.py` | environment checks, virtual-mic config | ported, API checks replaced (Task 25) |
| `cli.py` / `__main__.py` | argparse, `doctor`/`devices`/`run` dispatch | ported, flags changed (Task 26) |
| `run.py` | `Session` — builds every stage, startup/shutdown, signals | ported, rewired (Task 27) |

`activity.py` also gains `OverlapWatch` in Task 23. Tasks 28–30 are the end-to-end test, the cross-repo change to `$SIDETAP/sidetap/transcript.py`, and the documentation.

**Deleted with no successor**, and never copied in: `asr.py`, `segment.py`, `translate.py`, `tts.py`, `rotation.py`, `vad.py`.

### `scripts/` — experiment harnesses

`exp01_connect.py`, `exp02_voice_stability.py`, `exp03_session_limits.py`, `exp04_pacing.py`, `exp05_detection.py`. These are throwaway measurement tools, not part of the package, and are not imported by it.

### `docs/experiments/` — one Markdown file per experiment, recording what was measured

---

## Phase 0 — Experiments

**Nothing in Phase 1 starts until Phase 0 is finished and Task 6 has been answered.** The spec names six measurements; three of them can change the design, and one (Task 3) can invalidate the seam strategy outright. Building first and measuring later would mean rewriting `interpreter.py` after it has tests.

These scripts need a real `GEMINI_API_KEY` and real network. They are the only things in this repository that do.

### Task 1: Repository skeleton and a Live connection that works

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `sidetap_live/__init__.py`
- Create: `scripts/exp01_connect.py`
- Create: `docs/experiments/01-connect.md`

- [ ] **Step 1: Create the Python version pin**

```bash
cd /home/vvsosed/Documents/repo2/sidetap_live
echo "3.13" > .python-version
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "sidetap-live"
version = "0.1.0"
description = "Single-box real-time voice interpretation for any call, captured from PipeWire"
requires-python = ">=3.13"

dependencies = [
    "google-genai>=1.0.0",
    "webrtcvad-wheels>=2.0.14",
    "textual>=0.80",
]

[project.scripts]
sidetap-live = "sidetap_live.cli:main"

[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["sidetap_live"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 3: Create the empty package and install**

```bash
mkdir -p sidetap_live tests scripts docs/experiments
touch sidetap_live/__init__.py
uv sync
```

Expected: `uv` creates `.venv` and writes `uv.lock`. If `google-genai` fails to resolve, stop and report the error — every later task depends on it.

- [ ] **Step 4: Write `scripts/exp01_connect.py`**

This measures cold-start latency (spec experiment 6) and proves the key works.

```python
"""Experiment 1: open a live-translate session, measure cold start.

Answers spec experiment 6 - how long does opening a session take? That bounds
both the idle-suspend wake cost and the size of a forced seam's hole.

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
        generation_config=types.GenerationConfig(
            translation_config=types.TranslationConfig(
                target_language_code=target,
                echo_target_language=False,
            ),
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
```

- [ ] **Step 5: Run it**

```bash
GEMINI_API_KEY="$GEMINI_API_KEY" uv run python scripts/exp01_connect.py
```

Expected: ten `connected in NNN ms` lines and a summary. If the SDK rejects `translation_config` or `TranslationConfig` does not exist under `types`, **stop and inspect the installed SDK** — `uv run python -c "from google.genai import types; print([n for n in dir(types) if 'ranslat' in n])"` — then correct the field names here and in every later task that uses them. The spec's field names come from the REST documentation (`translationConfig`, `targetLanguageCode`, `echoTargetLanguage`); the Python SDK may snake-case or nest them differently, and **every subsequent task assumes whatever this step establishes.**

- [ ] **Step 6: Write `docs/experiments/01-connect.md`**

Record: the exact SDK version (`uv run python -c "import google.genai; print(google.genai.__version__)"`), the exact config object that worked, the min/median/max cold-start figures, and whether the field names matched the REST docs.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .python-version uv.lock sidetap_live/__init__.py scripts/exp01_connect.py docs/experiments/01-connect.md
git commit -m "Stand up the project and measure Live cold-start latency"
```

### Task 2: Experiment — voice stability across a pause-rotation

**This is the experiment that can invalidate the seam design, so it runs first.** The model card warns voices *"may shift after long pauses"*; the design rotates sessions *at* pauses deliberately. If identity resets at every seam, rotate-at-a-pause is the worst option rather than the best, and the spec's *Session continuity* decision must be reopened before `interpreter.py` is written.

**Files:**
- Create: `scripts/exp02_voice_stability.py`
- Create: `docs/experiments/02-voice-stability.md`
- Create: `tests/fixtures/speech_en_16k.raw` (recorded in step 1)

- [ ] **Step 1: Record a source clip**

Record 40 seconds of yourself reading English continuously, with a deliberate ~1 s pause every 8 seconds. Raw s16 16 kHz mono, which is what the model wants and what `pw-record` produces:

```bash
pw-record --rate 16000 --channels 1 --format s16 --raw tests/fixtures/speech_en_16k.raw
# speak for ~40s, then Ctrl-C
ls -l tests/fixtures/speech_en_16k.raw
```

Expected: roughly 1,280,000 bytes (40 s × 16000 × 2). If the file is tiny, `pw-record` picked the wrong source — check `wpctl status` for the default source.

- [ ] **Step 2: Write `scripts/exp02_voice_stability.py`**

```python
"""Experiment 2: does the output voice survive a session rotation?

Feeds the SAME clip twice: once through one continuous session, and once
split across two sessions at a silent boundary - which is exactly what
rotate-at-a-pause does. Writes both outputs so they can be listened to
side by side.

If the second half of the rotated run sounds like a different speaker than
the second half of the continuous run, the seam design is invalid.

Run:  GEMINI_API_KEY=... uv run python scripts/exp02_voice_stability.py
"""

import asyncio
import os
import pathlib
import sys

from google import genai
from google.genai import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from exp01_connect import MODEL, config  # noqa: E402

CLIP = pathlib.Path("tests/fixtures/speech_en_16k.raw")
OUT = pathlib.Path("docs/experiments/audio")
BLOCK = 3200          # 100 ms of 16 kHz s16 mono
SPLIT_S = 24.0        # rotate here; must land inside one of your pauses
REALTIME = True       # feed at wall-clock speed, as the real app does


async def feed(client, pcm: bytes) -> bytes:
    """Stream `pcm` through one session, return the audio it produced."""
    out = bytearray()
    async with client.aio.live.connect(model=MODEL, config=config()) as session:

        async def send() -> None:
            for i in range(0, len(pcm), BLOCK):
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm[i : i + BLOCK], mime_type="audio/pcm;rate=16000")
                )
                if REALTIME:
                    await asyncio.sleep(0.1)

        async def recv() -> None:
            async for message in session.receive():
                data = getattr(message, "data", None)
                if data:
                    out.extend(data)

        sender = asyncio.create_task(send())
        receiver = asyncio.create_task(recv())
        await sender
        await asyncio.sleep(3.0)      # let the tail arrive
        receiver.cancel()
    return bytes(out)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    pcm = CLIP.read_bytes()
    split = int(SPLIT_S * 16000) * 2

    continuous = await feed(client, pcm)
    (OUT / "continuous.raw").write_bytes(continuous)
    print(f"continuous: {len(continuous)/48000:.1f}s of output")

    first = await feed(client, pcm[:split])
    second = await feed(client, pcm[split:])
    (OUT / "rotated.raw").write_bytes(first + second)
    print(f"rotated:    {len(first)/48000:.1f}s + {len(second)/48000:.1f}s")

    print("\nListen to both:")
    print(f"  pw-cat --playback --rate 24000 --channels 1 --format s16 --raw {OUT}/continuous.raw")
    print(f"  pw-cat --playback --rate 24000 --channels 1 --format s16 --raw {OUT}/rotated.raw")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Run it and listen to both outputs**

```bash
GEMINI_API_KEY="$GEMINI_API_KEY" uv run python scripts/exp02_voice_stability.py
pw-cat --playback --rate 24000 --channels 1 --format s16 --raw docs/experiments/audio/continuous.raw
pw-cat --playback --rate 24000 --channels 1 --format s16 --raw docs/experiments/audio/rotated.raw
```

- [ ] **Step 4: Write `docs/experiments/02-voice-stability.md`**

Answer one question in the first line, so a reader does not have to infer it: **does the voice change at the seam — yes or no?** Then record what you heard on each side, whether gender shifted, and whether the seam is audible for any other reason (a clipped word, a repeated clause).

- [ ] **Step 5: Commit**

```bash
git add scripts/exp02_voice_stability.py docs/experiments/02-voice-stability.md
git commit -m "Measure whether the output voice survives a session rotation"
```

Do **not** commit the `.raw` files — add them to `.gitignore`:

```bash
printf '\n# experiment audio\ndocs/experiments/audio/\ntests/fixtures/*.raw\n' >> .gitignore
git add .gitignore && git commit -m "Ignore experiment audio"
```

### Task 3: Experiment — GoAway, session resumption, context compression

Covers spec experiments 1 and 2. The rotation state machine needs advance warning (`GoAway` with `timeLeft`) and needs resumption handles for its forced fallback. Both are documented for the Live API in general and unverified for this preview model.

**Files:**
- Create: `scripts/exp03_session_limits.py`
- Create: `docs/experiments/03-session-limits.md`

- [ ] **Step 1: Write `scripts/exp03_session_limits.py`**

```python
"""Experiment 3: does this model send GoAway, and can a session be resumed?

Streams silence for 12 minutes - past the documented ~10 minute connection
cap - with session resumption and context compression enabled, logging every
non-audio message with a timestamp.

Three questions:
  1. Does a `go_away` message arrive, and does it carry `time_left`?
  2. Do `session_resumption_update` messages arrive with handles?
  3. Does reconnecting with the last handle work?

Run:  GEMINI_API_KEY=... uv run python scripts/exp03_session_limits.py
"""

import asyncio
import os
import time

from google import genai
from google.genai import types

from exp01_connect import MODEL

BLOCK = 3200
SILENCE = b"\x00" * BLOCK
RUN_S = 12 * 60


def config(handle: str | None = None) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        generation_config=types.GenerationConfig(
            translation_config=types.TranslationConfig(
                target_language_code="ru",
                echo_target_language=False,
            ),
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=types.SessionResumptionConfig(handle=handle),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
    )


async def run_once(client, handle: str | None, budget_s: float) -> tuple[str | None, str]:
    """Stream silence until the connection ends or the budget runs out.

    Returns (last resumption handle, why it ended).
    """
    started = time.monotonic()
    last_handle = handle
    reason = "budget exhausted"
    async with client.aio.live.connect(model=MODEL, config=config(handle)) as session:

        async def send() -> None:
            while time.monotonic() - started < budget_s:
                await session.send_realtime_input(
                    audio=types.Blob(data=SILENCE, mime_type="audio/pcm;rate=16000")
                )
                await asyncio.sleep(0.1)

        async def recv() -> str:
            nonlocal last_handle
            async for message in session.receive():
                t = time.monotonic() - started
                if getattr(message, "go_away", None) is not None:
                    print(f"[{t:7.1f}s] GO_AWAY time_left={message.go_away.time_left!r}")
                    return "go_away"
                update = getattr(message, "session_resumption_update", None)
                if update is not None:
                    last_handle = update.new_handle
                    print(f"[{t:7.1f}s] RESUMPTION handle={str(last_handle)[:24]}... resumable={update.resumable}")
                if getattr(message, "data", None):
                    print(f"[{t:7.1f}s] AUDIO {len(message.data)} bytes (unexpected from silence)")
            return "receive ended"

        sender = asyncio.create_task(send())
        reason = await recv()
        sender.cancel()
    return last_handle, reason


async def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    handle, reason = await run_once(client, None, RUN_S)
    print(f"\nfirst connection ended: {reason}; handle={'yes' if handle else 'NO'}")
    if handle:
        print("\nreconnecting with the handle...")
        _, reason = await run_once(client, handle, 60)
        print(f"resumed connection ended: {reason}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it** (this takes ~13 minutes)

```bash
GEMINI_API_KEY="$GEMINI_API_KEY" uv run python scripts/exp03_session_limits.py 2>&1 | tee /tmp/exp03.log
```

- [ ] **Step 3: Write `docs/experiments/03-session-limits.md`**

Record, each as a yes/no with the evidence line beside it: did `go_away` arrive; did it carry a usable `time_left`; at what elapsed time; did resumption handles arrive and how often; did reconnecting with a handle succeed. If `session_resumption` or `context_window_compression` was rejected at connect time, that is the headline finding — record the exact error.

- [ ] **Step 4: Commit**

```bash
git add scripts/exp03_session_limits.py docs/experiments/03-session-limits.md
git commit -m "Measure GoAway, session resumption and context compression"
```

### Task 4: Experiment — output pacing

Covers spec experiment 4. Decides whether `backlog_s` measures anything or sits at zero all call, which is the headline metric of the whole comparison.

**Files:**
- Create: `scripts/exp04_pacing.py`
- Create: `docs/experiments/04-pacing.md`

- [ ] **Step 1: Write `scripts/exp04_pacing.py`**

```python
"""Experiment 4: does generated audio track input at realtime?

Feeds the recorded clip at wall-clock speed and records, every second, how
much input has been sent and how much output has arrived. If output
consistently exceeds input, backlog grows without bound and the lag cap
matters; if it tracks, the queue stays shallow and the half-duplex problem
is genuinely gone.

Run:  GEMINI_API_KEY=... uv run python scripts/exp04_pacing.py
"""

import asyncio
import os
import pathlib
import time

from google import genai
from google.genai import types

from exp01_connect import MODEL, config

CLIP = pathlib.Path("tests/fixtures/speech_en_16k.raw")
BLOCK = 3200
IN_BYTES_PER_S = 32000     # 16 kHz s16 mono
OUT_BYTES_PER_S = 48000    # 24 kHz s16 mono


async def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    pcm = CLIP.read_bytes()
    sent = 0
    received = 0
    first_out_at: float | None = None
    started = time.monotonic()

    async with client.aio.live.connect(model=MODEL, config=config()) as session:

        async def send() -> None:
            nonlocal sent
            for i in range(0, len(pcm), BLOCK):
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm[i : i + BLOCK], mime_type="audio/pcm;rate=16000")
                )
                sent += BLOCK
                await asyncio.sleep(0.1)

        async def recv() -> None:
            nonlocal received, first_out_at
            async for message in session.receive():
                data = getattr(message, "data", None)
                if data:
                    if first_out_at is None:
                        first_out_at = time.monotonic() - started
                    received += len(data)

        async def sample() -> None:
            while True:
                await asyncio.sleep(1.0)
                t = time.monotonic() - started
                print(
                    f"[{t:5.1f}s] in={sent/IN_BYTES_PER_S:5.1f}s "
                    f"out={received/OUT_BYTES_PER_S:5.1f}s "
                    f"ratio={received/OUT_BYTES_PER_S/max(sent/IN_BYTES_PER_S, 0.001):.2f}"
                )

        sender = asyncio.create_task(send())
        receiver = asyncio.create_task(recv())
        sampler = asyncio.create_task(sample())
        await sender
        await asyncio.sleep(5.0)
        receiver.cancel()
        sampler.cancel()

    print(f"\ntime to first output: {first_out_at:.2f}s" if first_out_at else "\nNO OUTPUT AT ALL")
    print(f"input {sent/IN_BYTES_PER_S:.1f}s -> output {received/OUT_BYTES_PER_S:.1f}s "
          f"(ratio {received/OUT_BYTES_PER_S/(sent/IN_BYTES_PER_S):.2f})")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it**

```bash
GEMINI_API_KEY="$GEMINI_API_KEY" uv run python scripts/exp04_pacing.py 2>&1 | tee /tmp/exp04.log
```

- [ ] **Step 3: Write `docs/experiments/04-pacing.md`**

Record: time to first output (this is `offset_s`'s floor), the final input-to-output ratio, and whether the per-second ratio trends upward across the clip. A ratio persistently above 1.0 means backlog grows and `--lag-cap` will fire in real calls; at or below 1.0 it will not.

- [ ] **Step 4: Commit**

```bash
git add scripts/exp04_pacing.py docs/experiments/04-pacing.md
git commit -m "Measure whether generated audio tracks input at realtime"
```

### Task 5: Experiment — accented-English source detection

Covers spec experiment 5. The model auto-detects the source language and the model card flags non-native accents; the author speaking accented English into the OUT direction is the case most likely to hit it.

**Files:**
- Create: `scripts/exp05_detection.py`
- Create: `docs/experiments/05-detection.md`

- [ ] **Step 1: Write `scripts/exp05_detection.py`**

```python
"""Experiment 5: is accented English detected as English?

Feeds the recorded English clip with target=ru (the OUT direction's real
config) and prints the input transcription. If the transcript comes back as
Russian or transliterated nonsense, source detection failed and the OUT
direction is unreliable for this speaker.

Run:  GEMINI_API_KEY=... uv run python scripts/exp05_detection.py
"""

import asyncio
import os
import pathlib
import time

from google import genai
from google.genai import types

from exp01_connect import MODEL, config

CLIP = pathlib.Path("tests/fixtures/speech_en_16k.raw")
BLOCK = 3200


async def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    pcm = CLIP.read_bytes()
    started = time.monotonic()

    async with client.aio.live.connect(model=MODEL, config=config("ru")) as session:

        async def send() -> None:
            for i in range(0, len(pcm), BLOCK):
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm[i : i + BLOCK], mime_type="audio/pcm;rate=16000")
                )
                await asyncio.sleep(0.1)

        async def recv() -> None:
            async for message in session.receive():
                t = time.monotonic() - started
                sc = getattr(message, "server_content", None)
                if sc is None:
                    continue
                if getattr(sc, "input_transcription", None):
                    print(f"[{t:5.1f}s] SOURCE: {sc.input_transcription.text!r}")
                if getattr(sc, "output_transcription", None):
                    print(f"[{t:5.1f}s] TARGET: {sc.output_transcription.text!r}")

        sender = asyncio.create_task(send())
        receiver = asyncio.create_task(recv())
        await sender
        await asyncio.sleep(5.0)
        receiver.cancel()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it**

```bash
GEMINI_API_KEY="$GEMINI_API_KEY" uv run python scripts/exp05_detection.py 2>&1 | tee /tmp/exp05.log
```

- [ ] **Step 3: Write `docs/experiments/05-detection.md`**

Record: whether `SOURCE` lines came back as English, roughly what word error rate you'd estimate by eye, and whether any stretch was detected as another language. Also record **where the transcription fields actually live on the message object** — the paths used here (`message.server_content.input_transcription.text`) are what `live.py` will parse in Task 14, and if they differ, Task 14 must match what you observed.

- [ ] **Step 4: Commit**

```bash
git add scripts/exp05_detection.py docs/experiments/05-detection.md
git commit -m "Measure source-language detection on accented English"
```

### Task 6: Decision checkpoint — reconcile the spec with what was measured

**This is a human decision point, not a code task. Do not start Phase 1 until it is resolved.**

- [ ] **Step 1: Re-read the spec's *Decisions* section against the five experiment documents**

- [ ] **Step 2: Answer each of these in writing, in the spec itself**

1. **Did the voice survive rotation (Task 2)?** If no, the *Session continuity* decision is invalid: rotate-at-a-pause makes the seam maximally audible. Change it to make-before-break (spec option B) and rewrite the *Session lifecycle* section before Task 19. Everything else in the plan stands.
2. **Does `go_away` carry `time_left` (Task 3)?** If no, `DRAINING` cannot be entered on warning. Replace it with a timer that rotates at `ROTATE_AFTER_S = 8 * 60`, and say so in *Session lifecycle*.
3. **Were `session_resumption` and `context_window_compression` accepted (Task 3)?** If resumption was rejected, the forced-seam fallback becomes a cold start — note it under *Failure handling* and raise `PREROLL_S` to cover the cold-start figure from Task 1.
4. **Is the output/input ratio above 1.0 (Task 4)?** If it is, backlog will grow in real calls and `--lag-cap` is load-bearing rather than a safety valve; say so in *Playout and the duck*.
5. **Did the SDK's field names match the REST documentation (Task 1)?** If not, record the real ones in the spec so Task 14 has a single source of truth.

- [ ] **Step 3: Commit the reconciled spec**

```bash
git add docs/superpowers/specs/2026-09-20-sidetap-live-design.md
git commit -m "Reconcile the design with what the experiments measured"
```

---

## Phase 1 — Port the audio layer

Every module in this phase already exists in `$SIDETAP` with passing tests. **They use relative imports (`from .ports import ...`), so the package files copy in with no edits at all.** Only the test files need changing, and only to rename the package.

The value of this phase is not the code — it is arriving at Phase 2 with a green suite that proves the audio layer survived the move.

### Task 7: Port `types.py`, trimmed

**Files:**
- Create: `sidetap_live/types.py`
- Test: `tests/test_types.py`

- [ ] **Step 1: Copy the original and the test**

```bash
export SIDETAP=/home/vvsosed/Documents/repo2/sidetap
cp "$SIDETAP/sidetap/types.py" sidetap_live/types.py
cp "$SIDETAP/tests/test_types.py" tests/test_types.py
cp "$SIDETAP/tests/__init__.py" tests/__init__.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g; s/\bimport sidetap\b/import sidetap_live/g; s/\bfrom sidetap import\b/from sidetap_live import/g' tests/test_types.py
```

- [ ] **Step 2: Delete the cascade-only types**

Remove from `sidetap_live/types.py`: `AsrResult`, `Unit`, `Translated`, `Latency`, `Record`. They describe a segmenter's output and a three-stage latency breakdown, neither of which exists here.

Then remove the tests that covered them from `tests/test_types.py`.

- [ ] **Step 3: Replace the constants block**

`LAG_CAP_S` rises from 20 to 30 (it is a safety valve now, not a working limit) and four constants are new. Replace the existing `LAG_CAP_S` / `DEAD_AIR_S` / `NO_AUDIO_S` block with:

```python
# Seconds of un-spoken audio past which playout starts dropping at an output
# silence boundary.
#
# A safety valve, not a working limit. Backlog growth is the experimental
# result this whole project exists to measure - see docs/experiments/04 - so
# the cap sits high enough that an ordinary call never reaches it and only a
# runaway does.
LAG_CAP_S = 30.0

# Seconds of continuous speech into a direction with nothing coming out.
DEAD_AIR_S = 6.0

# No audio at all reaching a capture queue for this long. An unlinked capture
# node delivers ZERO BYTES rather than silence, and with no gate in the path
# that now means we simply stop sending - which looks healthy at every stage
# downstream. This watchdog is the only thing that sees it.
NO_AUDIO_S = 15.0

# Silence long enough to rotate a session inside. Short enough to occur in
# ordinary conversation within a GoAway window, long enough that a breath
# between clauses does not trigger it. A false pause rotates mid-sentence.
ROTATE_PAUSE_S = 0.7

# Silence after which a session is closed entirely. Reopening costs the
# cold-start latency measured in docs/experiments/01-connect.md, paid only
# when someone starts talking again after most of a minute of nothing.
IDLE_SUSPEND_S = 45.0

# Playout must be idle this long before the duck reopens. Without the hold it
# flaps in the gaps between output chunks and chops the original into
# fragments, which is heard as the duck failing rather than as hysteresis
# missing.
DUCK_HOLD_S = 0.4

# Seconds of recent capture kept for replay into a freshly opened session.
# Feeds the two non-ideal paths only: waking from suspend, and crossing a
# seam where no pause arrived. The happy path replays nothing.
PREROLL_S = 3.0
```

- [ ] **Step 4: Add the new types at the end of the file**

```python
class SessionState(StrEnum):
    """Where one direction's Live session is in its lifecycle.

    SUSPENDED and RUNNING are steady states; OPENING and DRAINING are
    transitions that must terminate. DRAINING is bounded by GoAway's
    time_left, OPENING by the connect call itself.
    """

    SUSPENDED = "suspended"
    OPENING = "opening"
    RUNNING = "running"
    DRAINING = "draining"


@dataclass(frozen=True)
class TranscriptEvent:
    """One line of transcript.

    Deliberately NOT a source/target pair. inputAudioTranscription and
    outputAudioTranscription arrive as two independently-drifting streams, so
    any pairing would be invented rather than observed - see the spec's
    Transcript section. `kind` is "source" or "target".
    """

    t: float
    direction: Direction
    kind: str
    text: str
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_types.py -q
```

Expected: PASS, with the deleted types' tests gone.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/types.py tests/test_types.py tests/__init__.py
git commit -m "Port types.py, trimmed of the cascade's segmenter types"
```

### Task 8: Port the capture chain — `graph`, `recorder`, `tap`, `capture`

These four are the input side and need no edits whatsoever. They are copied together because `capture.py` imports the other three and their tests are interdependent.

**Files:**
- Create: `sidetap_live/graph.py`, `sidetap_live/recorder.py`, `sidetap_live/tap.py`, `sidetap_live/capture.py`
- Test: `tests/test_graph.py`, `tests/test_recorder.py`, `tests/test_tap.py`, `tests/test_capture.py`
- Create: `tests/fixtures/*.json`

- [ ] **Step 1: Copy the modules, their tests and the graph fixtures**

```bash
for m in graph recorder tap capture; do
  cp "$SIDETAP/sidetap/$m.py" "sidetap_live/$m.py"
  cp "$SIDETAP/tests/test_$m.py" "tests/test_$m.py"
done
mkdir -p tests/fixtures
cp "$SIDETAP"/tests/fixtures/*.json tests/fixtures/
```

- [ ] **Step 2: Rename the package in the tests**

```bash
sed -i 's/\bfrom sidetap\./from sidetap_live./g; s/\bimport sidetap\b/import sidetap_live/g; s/\bfrom sidetap import\b/from sidetap_live import/g' tests/test_graph.py tests/test_recorder.py tests/test_tap.py tests/test_capture.py
grep -rn '\bsidetap\b' tests/test_graph.py tests/test_recorder.py tests/test_tap.py tests/test_capture.py || echo "no stale references"
```

Expected: `no stale references`. A hit here means the `sed` missed a form — fix it by hand before continuing.

- [ ] **Step 3: Copy `ports.py` and `conftest.py` so the tests can import**

These are finished properly in Task 9; this step only unblocks the suite.

```bash
cp "$SIDETAP/sidetap/ports.py" sidetap_live/ports.py
cp "$SIDETAP/sidetap/adapters.py" sidetap_live/adapters.py
cp "$SIDETAP/tests/conftest.py" tests/conftest.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g' tests/conftest.py
```

- [ ] **Step 4: Remove the dangling type imports**

`ports.py` imports `AsrResult` and `Unit` from `types.py`, which Task 7 deleted. Delete these four Protocols from `sidetap_live/ports.py` entirely — `Recognizer`, `Segmenter`, `Translator`, `Synthesizer` — and drop the now-unused import line `from .types import AsrResult, Unit`.

In `tests/conftest.py`, delete `FakeTranslator`, `FakeSynthesizer`, `FakeRecognizer` and the `from sidetap_live.types import AsrResult` import.

- [ ] **Step 5: Run the capture-chain tests**

```bash
uv run pytest tests/test_graph.py tests/test_recorder.py tests/test_tap.py tests/test_capture.py -q
```

Expected: PASS, all four files. A failure here means something in the copy was not as self-contained as it looked — read the traceback rather than patching the test.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/ tests/
git commit -m "Port the capture chain: graph, recorder, tap, capture"
```

### Task 9: Finish `ports.py` and `adapters.py`

**Files:**
- Modify: `sidetap_live/adapters.py`
- Test: `tests/test_adapters.py`, `tests/test_ports.py`

- [ ] **Step 1: Copy the tests**

```bash
cp "$SIDETAP/tests/test_adapters.py" tests/test_adapters.py
cp "$SIDETAP/tests/test_ports.py" tests/test_ports.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g; s/\bimport sidetap\b/import sidetap_live/g' tests/test_adapters.py tests/test_ports.py
```

- [ ] **Step 2: Remove the tests for the deleted Protocols**

Delete any test in `tests/test_ports.py` referencing `Recognizer`, `Segmenter`, `Translator` or `Synthesizer`.

- [ ] **Step 3: Run**

```bash
uv run pytest tests/test_adapters.py tests/test_ports.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add sidetap_live/ tests/
git commit -m "Port ports.py and adapters.py"
```

### Task 10: Port `routing.py`, with its own state directory

The only substantive change in the whole phase. Two decisions are encoded here and both matter:

**The virtual microphone is shared with sidetap, deliberately.** Its node name, config filename and config content are identical. A messenger's saved input-device selection is tied to that name, and the entire reason the config is permanent rather than runtime-created is that Zoom and Viber otherwise lose the selection and fall back to your real microphone — with the remote party hearing your untranslated voice and nothing on screen saying so. Giving this program its own device name would reintroduce that failure every time you switched systems. `doctor --install` never overwrites an existing config, so the two installers are already compatible.

**The routing journal is not shared.** It records links *this process* made, and each program repairs its own with its own `--repair`.

**Files:**
- Create: `sidetap_live/routing.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Copy**

```bash
cp "$SIDETAP/sidetap/routing.py" sidetap_live/routing.py
cp "$SIDETAP/tests/test_routing.py" tests/test_routing.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g; s/\bimport sidetap\b/import sidetap_live/g' tests/test_routing.py
```

- [ ] **Step 2: Point the journal at this program's state directory**

Find `JOURNAL_PATH` in `sidetap_live/routing.py` and change only the directory component from `sidetap` to `sidetap_live`, so it reads `~/.local/state/sidetap_live/routing-journal.json`.

```bash
grep -n 'JOURNAL_PATH\|local/state' sidetap_live/routing.py
```

Then edit that line, and add above it:

```python
# Deliberately NOT shared with sidetap, even though the virtual mic below is.
# The journal describes links this process made; each program replays its own
# with its own `doctor --repair`. The virtual mic is shared for the opposite
# reason - a messenger's saved device selection is tied to the node name, and
# two names would mean re-selecting the microphone every time you switched
# between the two systems.
```

- [ ] **Step 3: Confirm the virtual-mic constants were NOT changed**

```bash
grep -n 'VIRTMIC_SINK\|VIRTMIC_SOURCE\|VIRTMIC_CONFIG_PATH' sidetap_live/routing.py
diff <(grep -E 'VIRTMIC_(SINK|SOURCE|CONFIG_PATH) *=' sidetap_live/routing.py) \
     <(grep -E 'VIRTMIC_(SINK|SOURCE|CONFIG_PATH) *=' "$SIDETAP/sidetap/routing.py") \
  && echo "virtual mic identity preserved"
```

Expected: `virtual mic identity preserved`. If this prints a diff, the shared-device decision has been broken — revert those three lines.

- [ ] **Step 4: Fix the journal path in the test**

```bash
grep -n 'state/sidetap' tests/test_routing.py
```

Update any literal path to match. Then run:

```bash
uv run pytest tests/test_routing.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/routing.py tests/test_routing.py
git commit -m "Port routing.py, keeping the shared virtual mic and splitting the journal"
```

### Task 11: Port `metrics.py` and strip the cascade's stage health

`metrics.py` is the TUI's only input and the one place worker threads write. Its shape changes because there are no longer three stages to report on.

**Files:**
- Create: `sidetap_live/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Copy**

```bash
cp "$SIDETAP/sidetap/metrics.py" sidetap_live/metrics.py
cp "$SIDETAP/tests/test_metrics.py" tests/test_metrics.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g' tests/test_metrics.py
```

- [ ] **Step 2: Write the failing test for the new shape**

Replace the contents of `tests/test_metrics.py` with:

```python
from sidetap_live.metrics import Health, Metrics
from sidetap_live.types import Direction, SessionState


def test_snapshot_is_a_deep_copy():
    metrics = Metrics()
    first = metrics.snapshot()
    metrics.set_backlog_s(Direction.IN, 1.5)
    assert first.directions[Direction.IN].backlog_s == 0.0
    assert metrics.snapshot().directions[Direction.IN].backlog_s == 1.5


def test_session_state_and_rotations_are_recorded():
    metrics = Metrics()
    metrics.set_session_state(Direction.OUT, SessionState.DRAINING)
    metrics.add_rotation(Direction.OUT, forced=True, replayed_s=1.2)
    metrics.add_rotation(Direction.OUT, forced=False, replayed_s=0.0)

    state = metrics.snapshot().directions[Direction.OUT]
    assert state.session_state is SessionState.DRAINING
    assert state.rotations == 2
    assert state.forced_rotations == 1
    assert state.replayed_s == 1.2


def test_health_defaults_ok_and_flips():
    metrics = Metrics()
    assert metrics.snapshot().directions[Direction.IN].session is Health.OK
    metrics.set_health(Direction.IN, session=Health.FAILED)
    assert metrics.snapshot().directions[Direction.IN].session is Health.FAILED


def test_overlap_is_session_wide_not_per_direction():
    metrics = Metrics()
    metrics.set_overlap_pct(12.5)
    assert metrics.snapshot().overlap_pct == 12.5
```

- [ ] **Step 3: Run it to verify it fails**

```bash
uv run pytest tests/test_metrics.py -q
```

Expected: FAIL with `AttributeError: 'DirectionState' object has no attribute 'backlog_s'`.

- [ ] **Step 4: Edit `sidetap_live/metrics.py`**

Change the import line to `from .types import Direction, SessionState`.

Replace `DirectionState` with:

```python
@dataclass
class DirectionState:
    # Live text from the two transcription streams. They drift relative to
    # each other by design; the TUI shows both rather than pairing them.
    source: str = ""
    target: str = ""

    # The headline number. Translated audio queued but not yet played.
    # Whether this stays bounded under continuous speech is the result this
    # project exists to measure.
    backlog_s: float = 0.0
    # Speech onset to the first output chunk of that stretch.
    offset_s: float = 0.0

    # Seconds of translated audio the lag cap threw away, not a count of
    # utterances: there are no utterances here to count.
    dropped_s: float = 0.0
    capture_dropped: int = 0
    dead_air: bool = False
    no_audio: bool = False

    session_state: SessionState = SessionState.SUSPENDED
    rotations: int = 0
    forced_rotations: int = 0
    replayed_s: float = 0.0

    session: Health = Health.OK
```

Replace `Snapshot` with:

```python
@dataclass
class Snapshot:
    directions: dict[Direction, DirectionState]
    cost_usd: float = 0.0
    bypassed: bool = False
    # Session-wide, not per-direction: it is a property of the two tracks
    # together. Fraction of wall clock where BOTH carry speech at once -
    # the most direct measure of whether the humans stopped taking turns.
    overlap_pct: float = 0.0
```

Delete `set_interim`, `set_final`, `set_queue_s`, `set_mt_model` and `add_dropped`. Keep `set_dead_air`, `set_no_audio`, `set_capture_dropped`, `add_cost` and `set_bypassed` exactly as ported. Add:

```python
    def set_text(self, direction: Direction, *, source: str | None = None,
                 target: str | None = None) -> None:
        with self._lock:
            state = self._states[direction]
            if source is not None:
                state.source = source
            if target is not None:
                state.target = target

    def set_backlog_s(self, direction: Direction, seconds: float) -> None:
        with self._lock:
            self._states[direction].backlog_s = seconds

    def set_dropped_s(self, direction: Direction, seconds: float) -> None:
        """Seconds of translated audio the lag cap threw away, cumulative.

        Distinct from `capture_dropped`, which counts blocks the CAPTURE queue
        discarded. The two overflow for unrelated reasons - this one when the
        model generates faster than realtime for long enough, that one when
        nothing is draining the queue - and a user who cannot tell them apart
        cannot act on either.
        """
        with self._lock:
            self._states[direction].dropped_s = seconds

    def set_offset_s(self, direction: Direction, seconds: float) -> None:
        with self._lock:
            self._states[direction].offset_s = seconds

    def set_session_state(self, direction: Direction, state: SessionState) -> None:
        with self._lock:
            self._states[direction].session_state = state

    def add_rotation(self, direction: Direction, *, forced: bool,
                     replayed_s: float) -> None:
        """One session handed over to its replacement.

        `forced` means no pause arrived before GoAway's time_left ran out, so
        the seam landed mid-speech and `replayed_s` of pre-roll was pushed
        into the new session. The clean/forced split is what says whether the
        pause assumption in the spec survived contact.
        """
        with self._lock:
            state = self._states[direction]
            state.rotations += 1
            if forced:
                state.forced_rotations += 1
            state.replayed_s += replayed_s

    def set_overlap_pct(self, value: float) -> None:
        with self._lock:
            self._overlap_pct = value
```

Change `set_health` to take only `session: Health | None = None`. In `__init__`, replace `self._mt_model = ""` with `self._overlap_pct = 0.0`, and in `snapshot()` swap `mt_model=self._mt_model` for `overlap_pct=self._overlap_pct`.

- [ ] **Step 5: Run to verify it passes**

```bash
uv run pytest tests/test_metrics.py -q
```

Expected: PASS, 4 tests.

- [ ] **Step 6: Run the whole suite so far**

```bash
uv run pytest -q
```

Expected: PASS. This is the checkpoint for the whole phase — the audio layer is now here and green.

- [ ] **Step 7: Commit**

```bash
git add sidetap_live/metrics.py tests/test_metrics.py
git commit -m "Port metrics.py, replacing stage health with session lifecycle"
```

---

## Phase 2 — The interpreter core

### Task 12: `activity.py` — observe speech, gate nothing

**Files:**
- Create: `sidetap_live/activity.py`
- Test: `tests/test_activity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_activity.py
import pytest

from sidetap_live.activity import SpeechActivity
from tests.conftest import FakeClock

SPEECH = b"\x40\x10" * 800     # 100 ms of 16 kHz s16
SILENCE = b"\x00\x00" * 800


def always(value: bool):
    return lambda pcm: value


def test_no_detector_never_reports_a_pause():
    """Without a detector, rotation falls back to a timer and idle-suspend
    never fires - rather than firing constantly on a detector saying nothing."""
    activity = SpeechActivity(None, FakeClock())
    assert activity.available is False
    assert activity.observe(SILENCE) is False
    assert activity.silence_s() == 0.0


def test_speech_resets_the_silence_clock():
    clock = FakeClock()
    activity = SpeechActivity(always(True), clock)
    clock.advance(5.0)
    activity.observe(SPEECH)
    assert activity.speaking is True
    assert activity.silence_s() == 0.0


def test_silence_accumulates_from_the_last_speech():
    clock = FakeClock()
    activity = SpeechActivity(always(True), clock)
    activity.observe(SPEECH)
    activity._detect = always(False)
    clock.advance(2.5)
    activity.observe(SILENCE)
    assert activity.speaking is False
    assert activity.silence_s() == pytest.approx(2.5)


def test_silence_accumulates_from_construction_when_nobody_has_spoken():
    """Starting the app before the call must still reach idle-suspend."""
    clock = FakeClock()
    activity = SpeechActivity(always(False), clock)
    clock.advance(60.0)
    assert activity.silence_s() == pytest.approx(60.0)


def test_it_is_not_a_gate():
    """Regression guard. sidetap's vad.py had .allows(pcm) and decided what to
    send; handing a continuous-audio model a discontinuous stream is the one
    thing this module must never do. If someone adds this method back, the
    rename did not do its job."""
    activity = SpeechActivity(always(True), FakeClock())
    assert not hasattr(activity, "allows")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_activity.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sidetap_live.activity'`.

- [ ] **Step 3: Write `sidetap_live/activity.py`**

```python
"""Speech activity observation. Never removes a byte.

This is NOT a gate. sidetap's vad.py decided what to send; this only reports
whether speech is present, because a model that reasons over continuous audio
has to receive continuous audio. Two consumers need it: rotation has to find a
pause to rotate inside, and idle-suspend has to notice speech onset.

The module is deliberately not called vad.py. The old name would invite
someone to reinstate gating, and gating is the one thing it must not do.
"""

from __future__ import annotations

import logging
from typing import Callable

from .ports import Clock
from .types import TARGET_RATE

log = logging.getLogger(__name__)

# webrtcvad accepts 10, 20 or 30 ms frames only.
FRAME_MS = 20
FRAME_BYTES = TARGET_RATE * 2 * FRAME_MS // 1000

SpeechDetector = Callable[[bytes], bool]


def webrtc_detector(aggressiveness: int = 2) -> SpeechDetector | None:
    """Real detector, or None when webrtcvad is unavailable.

    `aggressiveness` runs 0-3, higher filtering more non-speech. It is
    inherited from sidetap, which inherited it from meetscribe, and it is NOT
    a settled value here - it was tuned to decide what to drop from a
    transcriber's stream, and the only question asked of it now is "has
    speech stopped for ROTATE_PAUSE_S". Being wrong costs something different:
    a false pause rotates the session mid-sentence. Tune it against
    docs/experiments/02-voice-stability.md rather than assuming it.

    The returned closure is stateful - webrtcvad adapts to the noise floor
    across calls - so give each track its own detector rather than sharing one.
    """
    try:
        import webrtcvad
    except ImportError:
        log.warning(
            "webrtcvad not installed - session rotation falls back to a timer "
            "and idle-suspend is disabled. Run: uv sync"
        )
        return None

    vad = webrtcvad.Vad(aggressiveness)

    def detect(pcm: bytes) -> bool:
        return any(
            vad.is_speech(pcm[i : i + FRAME_BYTES], TARGET_RATE)
            for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)
        )

    return detect


class SpeechActivity:
    """Reports whether speech is happening. Returns no audio, ever."""

    def __init__(self, detector: SpeechDetector | None, clock: Clock):
        self._detect = detector
        self._clock = clock
        self._speaking = False
        # Seeded at construction, not left None: starting the program before
        # the call means nobody has spoken yet, and that must still count as
        # silence or idle-suspend would never fire on exactly the session a
        # user is most likely to leave running.
        self._last_speech = clock.monotonic()

    @property
    def available(self) -> bool:
        return self._detect is not None

    @property
    def speaking(self) -> bool:
        return self._speaking

    def observe(self, pcm: bytes) -> bool:
        """Note whether this block carries speech. Returns that, nothing else."""
        if self._detect is None:
            return False
        self._speaking = self._detect(pcm)
        if self._speaking:
            self._last_speech = self._clock.monotonic()
        return self._speaking

    def silence_s(self) -> float:
        """Seconds since speech was last heard, or 0.0 with no detector."""
        if self._detect is None:
            return 0.0
        return self._clock.monotonic() - self._last_speech
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_activity.py -q
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/activity.py tests/test_activity.py
git commit -m "Add activity.py: observes speech, gates nothing"
```

### Task 13: The session event alphabet, its port, and its fake

Everything above the socket reacts to a **closed union** of six events. Anything the SDK sends that is not one of them is dropped at the adapter boundary, so the state machine has a finite input alphabet and its tests can enumerate it.

**Files:**
- Modify: `sidetap_live/types.py`
- Modify: `sidetap_live/ports.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_ports.py`

- [ ] **Step 1: Add the event types to the end of `sidetap_live/types.py`**

```python
@dataclass(frozen=True)
class AudioOut:
    """Translated audio, 24 kHz s16 mono, straight from the model."""

    pcm: bytes


@dataclass(frozen=True)
class SourceText:
    """A fragment of inputAudioTranscription - what the speaker said."""

    text: str


@dataclass(frozen=True)
class TargetText:
    """A fragment of outputAudioTranscription - what was spoken back."""

    text: str


@dataclass(frozen=True)
class GoAway:
    """The connection will end in `time_left_s`. The window to rotate in."""

    time_left_s: float


@dataclass(frozen=True)
class ResumptionHandle:
    """A handle the forced-seam fallback can reconnect with.

    Kept even though rotate-at-a-pause does not normally use it: a handle
    cannot be requested once GoAway has already arrived.
    """

    handle: str


@dataclass(frozen=True)
class Closed:
    """The session ended. `reason` is for the log and the health flag."""

    reason: str


SessionEvent = (
    AudioOut | SourceText | TargetText | GoAway | ResumptionHandle | Closed
)
```

- [ ] **Step 2: Add the ports to `sidetap_live/ports.py`**

Add `from .types import SessionEvent` to the imports, then:

```python
@runtime_checkable
class InterpreterSession(Protocol):
    """One live speech-to-speech translation session.

    Synchronous on purpose. The SDK underneath is asyncio-native, but the
    whole ported audio layer is subprocess-and-thread shaped, so the asyncio
    island is confined inside the real implementation (live.py) rather than
    leaking into every consumer.
    """

    def send(self, pcm: bytes) -> None:
        """Queue 100 ms of 16 kHz s16 mono. Never blocks the caller."""
        ...

    def events(self) -> Iterator[SessionEvent]:
        """Yield until the session ends. Returns when it has."""
        ...

    def close(self) -> None:
        """Tear down. Makes `events()` return."""
        ...


@runtime_checkable
class SessionFactory(Protocol):
    def open(
        self, target_lang: str, *, echo: bool, handle: str | None = None
    ) -> InterpreterSession:
        """`handle` resumes a previous session; None starts a fresh one."""
        ...
```

- [ ] **Step 3: Write the fake in `tests/conftest.py`**

```python
class FakeSession:
    """Scriptable InterpreterSession.

    Events queued before or during the test are yielded by events(); close()
    makes the iterator return, which is what lets a rotation test assert the
    old receive thread actually exits.
    """

    def __init__(self, *events):
        self.sent = bytearray()
        self.closed = False
        self._events: queue.Queue = queue.Queue()
        for event in events:
            self._events.put(event)

    def send(self, pcm: bytes) -> None:
        assert not self.closed, "sent to a closed session"
        self.sent.extend(pcm)

    def emit(self, event) -> None:
        """Push an event from the test, mid-run."""
        self._events.put(event)

    def events(self):
        while True:
            try:
                yield self._events.get(timeout=0.02)
            except queue.Empty:
                if self.closed:
                    return

    def close(self) -> None:
        self.closed = True

    @property
    def sent_s(self) -> float:
        return len(self.sent) / (TARGET_RATE * 2)


class FakeSessionFactory:
    def __init__(self):
        self.sessions: list[FakeSession] = []
        self.opens: list[tuple[str, bool, str | None]] = []

    def open(self, target_lang: str, *, echo: bool, handle: str | None = None):
        self.opens.append((target_lang, echo, handle))
        session = FakeSession()
        self.sessions.append(session)
        return session


@pytest.fixture
def fake_sessions() -> FakeSessionFactory:
    return FakeSessionFactory()
```

Add `import queue` and `from sidetap_live.types import TARGET_RATE` to the top of `tests/conftest.py`.

- [ ] **Step 4: Write the conformance test**

Append to `tests/test_ports.py`:

```python
from sidetap_live.ports import InterpreterSession, SessionFactory
from tests.conftest import FakeSession, FakeSessionFactory


def test_fakes_satisfy_their_protocols():
    assert isinstance(FakeSession(), InterpreterSession)
    assert isinstance(FakeSessionFactory(), SessionFactory)


def test_close_ends_the_event_iterator():
    session = FakeSession()
    session.close()
    assert list(session.events()) == []
```

- [ ] **Step 5: Run**

```bash
uv run pytest tests/test_ports.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/types.py sidetap_live/ports.py tests/conftest.py tests/test_ports.py
git commit -m "Define the session event alphabet, its port and its fake"
```

### Task 14: `live.py` — the Gemini adapter

**Before writing this, re-read `docs/experiments/01-connect.md` and `05-detection.md`.** They record the real SDK field names and the real message shape. Where this task's code disagrees with what was measured, **the measurement wins** — correct the code here rather than reconciling later.

The design point: `parse_message` is a **pure function**, separate from the socket. That is what makes the translation from SDK message to `SessionEvent` testable with no network, and it is where every `getattr` guard lives.

**Files:**
- Create: `sidetap_live/live.py`
- Test: `tests/test_live.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_live.py
from types import SimpleNamespace

import pytest

from sidetap_live.live import build_config, parse_message, seconds_of
from sidetap_live.types import (
    AudioOut,
    GoAway,
    ResumptionHandle,
    SourceText,
    TargetText,
)


def message(**kwargs) -> SimpleNamespace:
    base = dict(data=None, server_content=None, go_away=None,
                session_resumption_update=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_audio_becomes_one_event():
    assert parse_message(message(data=b"\x01\x02")) == [AudioOut(pcm=b"\x01\x02")]


def test_both_transcriptions_come_through_separately():
    content = SimpleNamespace(
        input_transcription=SimpleNamespace(text="hello"),
        output_transcription=SimpleNamespace(text="privet"),
    )
    assert parse_message(message(server_content=content)) == [
        SourceText(text="hello"),
        TargetText(text="privet"),
    ]


def test_empty_transcription_text_is_not_an_event():
    content = SimpleNamespace(
        input_transcription=SimpleNamespace(text=""),
        output_transcription=None,
    )
    assert parse_message(message(server_content=content)) == []


def test_go_away_carries_seconds():
    go = SimpleNamespace(time_left="42s")
    assert parse_message(message(go_away=go)) == [GoAway(time_left_s=42.0)]


def test_unresumable_update_yields_no_handle():
    update = SimpleNamespace(new_handle="abc", resumable=False)
    assert parse_message(message(session_resumption_update=update)) == []
    update = SimpleNamespace(new_handle="abc", resumable=True)
    assert parse_message(message(session_resumption_update=update)) == [
        ResumptionHandle(handle="abc")
    ]


def test_unknown_message_is_dropped_not_raised():
    """The state machine above needs a finite input alphabet."""
    assert parse_message(SimpleNamespace(tool_call="something new")) == []


@pytest.mark.parametrize(
    "value,expected",
    [("42s", 42.0), ("0.5s", 0.5), (42, 42.0), (42.5, 42.5), (None, 0.0)],
)
def test_seconds_of_accepts_every_shape_the_sdk_might_use(value, expected):
    assert seconds_of(value) == expected


def test_config_sets_target_and_echo():
    config = build_config(target_lang="ru", echo=True, handle=None)
    assert config.translation_config.target_language_code == "ru"
    assert config.translation_config.echo_target_language is True


def test_translation_config_is_top_level_not_under_generation_config():
    """Regression guard for a silent failure mode.

    GenerationConfig also has a translation_config field, so the nested form
    type-checks and connects with only a DeprecationWarning - producing a
    conversational agent instead of an interpreter, with nothing in the logs
    saying so. See docs/experiments/01-connect.md.
    """
    config = build_config(target_lang="ru", echo=False, handle=None)
    assert config.translation_config is not None
    assert config.generation_config is None
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_live.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sidetap_live.live'`.

- [ ] **Step 3: Write `sidetap_live/live.py`**

```python
"""The only thing in this package that talks to Gemini.

google-genai's Live API is asyncio-native and everything else here is
threaded, because the ported capture and playout code is subprocess-and-thread
shaped and converting it would risk the tested foundation to serve the part
being measured. So the asyncio island is confined to this module: each
GeminiLiveSession owns one thread running one event loop, and presents the
synchronous InterpreterSession Protocol outward.

`parse_message` is deliberately pure and outside the class. Translating an SDK
message into this package's own event types is the part most likely to be
wrong and most worth testing, and it needs no network to test.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

from .types import (
    TARGET_RATE,
    AudioOut,
    Closed,
    GoAway,
    ResumptionHandle,
    SessionEvent,
    SourceText,
    TargetText,
)

log = logging.getLogger(__name__)

MODEL = "gemini-3.5-live-translate-preview"

# 10 s of 100 ms blocks. Past this the socket is not draining and blocking
# would stall the capture pump and then pw-record itself.
OUTBOUND_BLOCKS = 100
CLOSE_TIMEOUT_S = 3.0

_SENTINEL = object()


def seconds_of(value) -> float:
    """Coerce whatever `time_left` turns out to be into seconds.

    The REST documentation describes a duration; the SDK may hand back a
    protobuf Duration, a timedelta, a float, or the string form "42s".
    docs/experiments/03-session-limits.md records which one it actually was -
    this accepts all of them rather than guessing, because being wrong here
    means the rotation window is silently zero.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.rstrip("s") or 0.0)
    for attr in ("total_seconds", "seconds"):
        got = getattr(value, attr, None)
        if callable(got):
            return float(got())
        if got is not None:
            return float(got)
    log.warning("unrecognised duration %r; treating as 0", value)
    return 0.0


def parse_message(message) -> list[SessionEvent]:
    """Pure: one SDK message in, zero or more SessionEvents out.

    Everything unrecognised is dropped HERE, which is what gives the state
    machine above a finite input alphabet. Written with getattr throughout
    because the preview SDK's message shape is not stable enough to unpack.
    """
    events: list[SessionEvent] = []

    data = getattr(message, "data", None)
    if data:
        events.append(AudioOut(pcm=bytes(data)))

    content = getattr(message, "server_content", None)
    if content is not None:
        source = getattr(content, "input_transcription", None)
        if source is not None and getattr(source, "text", ""):
            events.append(SourceText(text=source.text))
        target = getattr(content, "output_transcription", None)
        if target is not None and getattr(target, "text", ""):
            events.append(TargetText(text=target.text))

    go_away = getattr(message, "go_away", None)
    if go_away is not None:
        events.append(GoAway(time_left_s=seconds_of(getattr(go_away, "time_left", None))))

    update = getattr(message, "session_resumption_update", None)
    if update is not None:
        handle = getattr(update, "new_handle", None)
        if handle and getattr(update, "resumable", False):
            events.append(ResumptionHandle(handle=handle))

    return events


def build_config(*, target_lang: str, echo: bool, handle: str | None):
    """The session config, as the SDK's own typed objects.

    MEASURED, not assumed - see docs/experiments/01-connect.md. The REST
    documentation nests translationConfig under generationConfig; in
    google-genai 2.24.0 it is a TOP-LEVEL field of LiveConnectConfig,
    alongside input_audio_transcription and output_audio_transcription.

    That distinction is load-bearing and silent. `GenerationConfig` ALSO
    exposes a `translation_config` field, so the nested form type-checks AND
    connects, emitting only a DeprecationWarning. A session built the wrong
    way does not fail - it comes up as a conversational agent with its own
    turn-taking instead of an interpreter, which is the exact behaviour this
    project exists to avoid, with nothing in the logs to say so. Do not
    "simplify" this by moving the field under generation_config.

    Imported inside the function so the package imports with no SDK present,
    as with every other third-party dependency here.
    """
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target_lang,
            echo_target_language=echo,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


class GeminiLiveSession:
    """Real InterpreterSession. Owns one thread running one event loop."""

    def __init__(self, *, connect, config: dict, model: str = MODEL):
        """`connect(model=..., config=...)` returns an async context manager.

        Injected rather than imported so a test can drive this class without
        the SDK. run.py passes `genai.Client(...).aio.live.connect`.
        """
        self._connect = connect
        self._config = config
        self._model = model
        self._outbound: queue.Queue = queue.Queue(maxsize=OUTBOUND_BLOCKS)
        self._inbound: queue.Queue = queue.Queue()
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="live-session"
        )
        self._thread.start()

    def send(self, pcm: bytes) -> None:
        try:
            self._outbound.put_nowait(pcm)
        except queue.Full:
            # Audio is lost either way once the socket stops draining; this
            # way the process survives and the NO_AUDIO watchdog upstream
            # stays meaningful instead of being masked by a stalled pump.
            log.warning("live outbound queue full; dropped a block")

    def events(self):
        while True:
            item = self._inbound.get()
            if item is _SENTINEL:
                return
            yield item

    def close(self) -> None:
        self._closing.set()
        self._outbound.put(_SENTINEL)
        self._thread.join(timeout=CLOSE_TIMEOUT_S)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            log.exception("live session ended abnormally")
            self._inbound.put(Closed(reason=str(exc)))
        else:
            self._inbound.put(Closed(reason="ended"))
        finally:
            # Always, even if Closed could not be queued: events() must
            # return or the receive thread above leaks for the whole call.
            self._inbound.put(_SENTINEL)

    async def _main(self) -> None:
        async with self._connect(model=self._model, config=self._config) as session:
            sender = asyncio.create_task(self._send_loop(session))
            receiver = asyncio.create_task(self._recv_loop(session))
            _, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    async def _send_loop(self, session) -> None:
        loop = asyncio.get_running_loop()
        while not self._closing.is_set():
            # The outbound queue is thread-safe but blocking, so it is drained
            # on a worker thread rather than stalling the event loop - which
            # would stop the receive side too, for as long as nobody speaks.
            pcm = await loop.run_in_executor(None, self._outbound.get)
            if pcm is _SENTINEL:
                return
            await session.send_realtime_input(
                audio={"data": pcm, "mime_type": f"audio/pcm;rate={TARGET_RATE}"}
            )

    async def _recv_loop(self, session) -> None:
        async for message in session.receive():
            for event in parse_message(message):
                self._inbound.put(event)


def build_factory(client, *, model: str = MODEL):
    """A SessionFactory closing over one genai client."""

    class _Factory:
        def open(self, target_lang: str, *, echo: bool, handle: str | None = None):
            return GeminiLiveSession(
                connect=client.aio.live.connect,
                config=build_config(target_lang=target_lang, echo=echo, handle=handle),
                model=model,
            )

    return _Factory()
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_live.py -q
```

Expected: PASS, 12 tests (the parametrised case counts as five).

- [ ] **Step 5: Verify the real SDK accepts the dict config**

```bash
GEMINI_API_KEY="$GEMINI_API_KEY" uv run python -c "
import asyncio, os
from google import genai
from sidetap_live.live import MODEL, build_config
async def main():
    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    cfg = build_config(target_lang='ru', echo=False, handle=None)
    async with client.aio.live.connect(model=MODEL, config=cfg) as s:
        print('connected with dict config:', s is not None)
asyncio.run(main())
"
```

Expected: `connected with dict config: True`. If the SDK rejects the dict, convert `build_config` to return the typed objects from `docs/experiments/01-connect.md` and change the two assertions in `test_config_sets_target_and_echo` to match.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/live.py tests/test_live.py
git commit -m "Add live.py: the only module that talks to Gemini"
```

### Task 15: `cost.py` — audio-token rates

**Files:**
- Create: `sidetap_live/cost.py`
- Test: `tests/test_cost.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cost.py
import pytest

from sidetap_live.cost import Rates, input_seconds, output_seconds


def test_one_minute_of_input():
    # 25 tokens/s x 60 s = 1500 tokens; 1500/1e6 x $3.50
    assert Rates().input_usd(60.0) == pytest.approx(0.00525)


def test_one_minute_of_output():
    assert Rates().output_usd(60.0) == pytest.approx(0.0315)


def test_bytes_convert_at_each_side_s_own_rate():
    """Capture is 16 kHz, playout is 24 kHz. Using one rate for both would
    understate output by a third."""
    assert input_seconds(32_000) == pytest.approx(1.0)
    assert output_seconds(48_000) == pytest.approx(1.0)


def test_rates_are_configuration_not_constants():
    doubled = Rates(input_per_million=7.0)
    assert doubled.input_usd(60.0) == pytest.approx(0.0105)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_cost.py -q
```

Expected: FAIL, no module named `sidetap_live.cost`.

- [ ] **Step 3: Write `sidetap_live/cost.py`**

```python
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
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_cost.py -q
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/cost.py tests/test_cost.py
git commit -m "Add cost.py: audio-token rates, exact quantities"
```

### Task 16: `preroll.py` — the ring that feeds the two non-ideal paths

**Files:**
- Create: `sidetap_live/preroll.py`
- Test: `tests/test_preroll.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preroll.py
import pytest

from sidetap_live.preroll import PreRoll
from sidetap_live.types import BLOCK_BYTES

BLOCK = b"\x01" * BLOCK_BYTES


def test_it_keeps_only_the_most_recent_seconds():
    ring = PreRoll(seconds=1.0)          # 10 blocks at 100 ms
    for i in range(25):
        ring.add(bytes([i]) * BLOCK_BYTES)
    blocks = ring.drain()
    assert len(blocks) == 10
    assert blocks[0][0] == 15            # oldest surviving block
    assert blocks[-1][0] == 24


def test_seconds_reports_what_is_held():
    ring = PreRoll(seconds=3.0)
    for _ in range(5):
        ring.add(BLOCK)
    assert ring.seconds() == pytest.approx(0.5)


def test_drain_empties_it():
    ring = PreRoll(seconds=3.0)
    ring.add(BLOCK)
    assert ring.drain() == [BLOCK]
    assert ring.drain() == []
    assert ring.seconds() == 0.0
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_preroll.py -q
```

Expected: FAIL, no module named `sidetap_live.preroll`.

- [ ] **Step 3: Write `sidetap_live/preroll.py`**

```python
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
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_preroll.py -q
```

Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/preroll.py tests/test_preroll.py
git commit -m "Add preroll.py: recent capture for the two non-ideal paths"
```

---

### Task 17: `DuckControl` with a level, and the silence-boundary finder

Two pure-ish pieces, written and tested before the queue that drives them.

`DuckControl` keeps sidetap's two hard-won properties verbatim — it flips its internal flag only on a *successful* `wpctl` call, and it resolves a callable `object_id` on every transition rather than once at construction. Both are load-bearing and both are explained in the code.

What is new is `level`: `--duck-level 0.2` is booth mode, where the original is held under the translation rather than silenced.

**Files:**
- Create: `sidetap_live/playout.py`
- Test: `tests/test_playout.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_playout.py
import pytest

from sidetap_live.playout import CHUNK_BYTES, DuckControl, find_silence_boundary
from tests.conftest import FakeVolumeControl

LOUD = (b"\x00\x40" * (CHUNK_BYTES // 2))     # peak 0x4000
QUIET = (b"\x00\x00" * (CHUNK_BYTES // 2))


def test_duck_only_calls_on_a_transition():
    volume = FakeVolumeControl()
    duck = DuckControl(volume, object_id=7)
    duck.close()
    duck.close()
    duck.close()
    assert volume.calls == [(7, 0.0)]
    duck.open()
    duck.open()
    assert volume.calls == [(7, 0.0), (7, 1.0)]


def test_duck_level_is_configurable_for_booth_mode():
    volume = FakeVolumeControl()
    DuckControl(volume, object_id=7, level=0.2).close()
    assert volume.calls == [(7, 0.2)]


def test_a_failed_call_leaves_the_flag_alone_so_the_next_one_retries():
    volume = FakeVolumeControl(ok=False)
    duck = DuckControl(volume, object_id=7)
    duck.close()
    assert duck.is_open is True
    duck.close()
    assert volume.calls == [(7, 0.0), (7, 0.0)]


def test_a_callable_object_id_is_resolved_on_every_transition():
    """Router.engage() returns before pw-loopback has registered the duck, so
    the id is still None when Session.setup() builds this. Reading it once
    meant the duck was never created and the original played under every
    translation for the whole call, with nothing logged."""
    volume = FakeVolumeControl()
    ids = iter([None, 42])
    duck = DuckControl(volume, object_id=lambda: next(ids))
    duck.close()
    assert volume.calls == []
    assert duck.is_open is True
    duck.close()
    assert volume.calls == [(42, 0.0)]


def test_silence_boundary_finds_the_first_quiet_frame():
    pcm = bytearray(LOUD + LOUD + QUIET + LOUD)
    assert find_silence_boundary(pcm) == 2 * CHUNK_BYTES


def test_silence_boundary_is_none_when_it_is_loud_throughout():
    """No boundary means run long rather than cut a word in half."""
    assert find_silence_boundary(bytearray(LOUD * 4)) is None


def test_silence_boundary_ignores_a_trailing_partial_frame():
    pcm = bytearray(LOUD + QUIET[: CHUNK_BYTES // 2])
    assert find_silence_boundary(pcm) is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_playout.py -q
```

Expected: FAIL, no module named `sidetap_live.playout`.

- [ ] **Step 3: Write the first half of `sidetap_live/playout.py`**

```python
"""Speak translated audio, duck the original, bound the backlog.

One long-lived pw-cat per direction, fed raw PCM. Between chunks this writes
silence rather than stopping, which keeps pw-cat's buffer primed and gives
playout exact knowledge of when it is emitting speech - and THAT is what
drives the duck.

The duck is driven from the output queue, not from the remote party speaking.
sidetap does it the other way round, which means a dead pipeline leaves the
duck closed over a live call - the failure its "fails open" invariant exists
to prevent, patched with a finally clause. Here, no audio out means no duck,
by construction.
"""

from __future__ import annotations

import logging
import threading
from array import array
from typing import Callable

from .ports import AudioSink, VolumeControl
from .types import DUCK_HOLD_S, LAG_CAP_S, TTS_BYTES_PER_S, TTS_RATE, Direction

log = logging.getLogger(__name__)

CHUNK_MS = 20
CHUNK_BYTES = TTS_BYTES_PER_S * CHUNK_MS // 1000
SILENCE_CHUNK = b"\x00" * CHUNK_BYTES

# Ticks of not receiving a full chunk before the tail is flushed padded.
# 10 x 20 ms = 200 ms: long enough that a producer delivering at realtime in
# small pieces is never padded (which would stutter), short enough that a
# genuine tail is not left sitting in the buffer holding the duck closed.
STARVE_LIMIT_TICKS = 10

# Silent ticks before the duck reopens. Without the hold it flaps in the gaps
# between output chunks and chops the original into fragments, which is heard
# as the duck failing rather than as hysteresis missing.
DUCK_HOLD_TICKS = max(1, int(DUCK_HOLD_S * 1000 / CHUNK_MS))

# Peak amplitude, out of 32767, below which a frame counts as a pause in the
# OUTPUT audio. Generous: a false positive drops backlog at a slightly noisy
# moment, a false negative refuses to drop at all.
SILENCE_PEAK = 600


def find_silence_boundary(
    pcm: bytearray, frame_bytes: int = CHUNK_BYTES, threshold: int = SILENCE_PEAK
) -> int | None:
    """Byte offset of the first low-energy frame, or None if there is none.

    Dropping backlog at an arbitrary offset cuts a word in half; dropping at a
    pause in the output is inaudible beyond the missing sentence. Uses `array`
    rather than numpy because the audio path in this package carries no numpy,
    in either direction, exactly as in sidetap. A trailing partial frame is
    not examined - it may yet be filled.
    """
    for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        samples = array("h")
        samples.frombytes(bytes(pcm[start : start + frame_bytes]))
        if max(abs(s) for s in samples) < threshold:
            return start
    return None


class DuckControl:
    """Silences (or lowers) the remote party's original while we speak."""

    def __init__(
        self,
        volume: VolumeControl,
        object_id: int | Callable[[], int | None],
        level: float = 0.0,
    ):
        """`object_id` may be a callable, and for a live session it must be.

        Router.engage() finishes before pw-loopback has registered the duck
        with the graph - deliberately, because the alternative is journalling
        links to ports that do not exist yet - so the duck's object id is
        still None when Session.setup() builds this. Reading it once there
        meant the duck was never created at all, ducking never happened, and
        the user heard the original underneath every translation for the whole
        call, with nothing logged. Resolving it on each transition lets the id
        arrive a poll later, which is exactly when it does arrive.

        `level` is what "closed" means: 0.0 replaces the original entirely,
        0.2 holds it under the translation the way an interpreting booth does.
        """
        self._volume = volume
        self._object_id = object_id
        self._level = level
        self._closed = False

    def _resolve(self) -> int | None:
        if callable(self._object_id):
            return self._object_id()
        return self._object_id

    def close(self) -> None:
        # Only flip on a successful call. set_volume returns False rather than
        # raising when wpctl fails; flipping anyway would desync the flag from
        # the real volume, and the next transition would think it is already
        # in the target state and skip retrying. A duck that has not appeared
        # yet is the same case: not an error, just not yet.
        if self._closed:
            return
        object_id = self._resolve()
        if object_id is not None and self._volume.set_volume(object_id, self._level):
            self._closed = True

    def open(self) -> None:
        if not self._closed:
            return
        object_id = self._resolve()
        if object_id is not None and self._volume.set_volume(object_id, 1.0):
            self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_playout.py -q
```

Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/playout.py tests/test_playout.py
git commit -m "Add DuckControl with a configurable level and the silence-boundary finder"
```

### Task 18: `Playout` — a chunk queue, not an utterance queue

sidetap queues whole utterances with known durations. There are no utterances here, so this becomes one pending buffer drained a chunk at a time, with `pw-cat`'s own blocking write providing realtime pacing for free.

**Files:**
- Modify: `sidetap_live/playout.py`
- Test: `tests/test_playout.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_playout.py`:

```python
from sidetap_live.playout import DUCK_HOLD_TICKS, STARVE_LIMIT_TICKS, Playout
from sidetap_live.types import Direction
from tests.conftest import FakeAudioSink

SPEECH = b"\x00\x40" * (CHUNK_BYTES // 2)


def build(**kwargs):
    sink = FakeAudioSink()
    volume = FakeVolumeControl()
    duck = DuckControl(volume, object_id=7)
    return Playout(Direction.IN, sink, duck=duck, **kwargs), sink, volume


def test_a_full_chunk_is_written_and_closes_the_duck():
    playout, sink, volume = build()
    playout.submit(SPEECH)
    assert playout.tick() is True
    assert sink.chunks == [SPEECH]
    assert volume.calls == [(7, 0.0)]


def test_an_empty_queue_writes_silence_and_keeps_the_duck_shut_until_the_hold():
    playout, sink, volume = build()
    playout.submit(SPEECH)
    playout.tick()
    for _ in range(DUCK_HOLD_TICKS - 1):
        assert playout.tick() is False
    assert volume.calls == [(7, 0.0)]          # still closed, within the hold
    playout.tick()
    assert volume.calls == [(7, 0.0), (7, 1.0)]


def test_a_gap_shorter_than_the_hold_does_not_reopen_the_duck():
    """Chunks arriving unevenly must not chop the original into fragments."""
    playout, sink, volume = build()
    playout.submit(SPEECH)
    playout.tick()
    for _ in range(DUCK_HOLD_TICKS - 2):
        playout.tick()
    playout.submit(SPEECH)
    assert playout.tick() is True
    assert volume.calls == [(7, 0.0)]


def test_a_partial_tail_waits_then_is_flushed_padded():
    playout, sink, _ = build()
    playout.submit(SPEECH[: CHUNK_BYTES // 2])
    for _ in range(STARVE_LIMIT_TICKS):
        assert playout.tick() is False
    assert playout.tick() is True
    assert len(sink.chunks[-1]) == CHUNK_BYTES
    assert sink.chunks[-1].endswith(b"\x00" * (CHUNK_BYTES // 2))


def test_backlog_reports_seconds_pending():
    playout, _, _ = build()
    playout.submit(b"\x00" * TTS_BYTES_PER_S)
    assert playout.backlog_s() == pytest.approx(1.0)


def test_the_cap_drops_at_a_silence_boundary_only():
    playout, _, _ = build(lag_cap_s=0.5)
    loud = SPEECH * 25                                   # 0.5 s, all loud
    playout.submit(loud + QUIET + loud)
    assert playout.backlog_s() > 0.5
    # It dropped everything up to the quiet frame, and no further.
    assert playout.dropped_s == pytest.approx(len(loud) / TTS_BYTES_PER_S)


def test_the_cap_refuses_to_cut_a_word_in_half():
    playout, _, _ = build(lag_cap_s=0.1)
    playout.submit(SPEECH * 50)
    assert playout.dropped_s == 0.0
    assert playout.backlog_s() > 0.1


def test_suppressed_throws_the_queue_away_and_opens_the_duck():
    playout, sink, volume = build()
    playout.submit(SPEECH)
    playout.tick()
    playout.set_suppressed(True)
    assert playout.backlog_s() == 0.0
    assert playout.tick() is False
    assert volume.calls[-1] == (7, 1.0)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_playout.py -q
```

Expected: FAIL with `ImportError: cannot import name 'Playout'`.

- [ ] **Step 3: Append `Playout` to `sidetap_live/playout.py`**

```python
class Playout:
    """One direction's output: a pending buffer drained one chunk per tick."""

    def __init__(
        self,
        direction: Direction,
        sink: AudioSink,
        *,
        duck: DuckControl | None = None,
        lag_cap_s: float = LAG_CAP_S,
    ):
        self.direction = direction
        self.dropped_s = 0.0
        self.spoken_s = 0.0
        self.suppressed = False
        self._sink = sink
        self._duck = duck
        self._lag_cap_s = lag_cap_s
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._starved = 0
        self._idle_ticks = DUCK_HOLD_TICKS

    @property
    def duck(self) -> DuckControl | None:
        return self._duck

    def submit(self, pcm: bytes) -> None:
        with self._lock:
            self._pending.extend(pcm)
            self._trim_locked()

    def backlog_s(self) -> float:
        with self._lock:
            return len(self._pending) / TTS_BYTES_PER_S

    def flush(self) -> float:
        """Drop everything not yet handed to the sink. Returns seconds dropped.

        The chunk already passed to sink.write() cannot be recalled - pw-cat
        has it. sidetap measured 441 ms still sitting in pw-cat's own buffer
        at the moment the hotkey fires (docs/experiments/02-pwcat-playback.md
        in that repository); that audio is past this process's control and
        plays regardless.
        """
        with self._lock:
            seconds = len(self._pending) / TTS_BYTES_PER_S
            self._pending.clear()
            self._starved = 0
            return seconds

    def set_suppressed(self, value: bool) -> None:
        """Entering bypass throws the queue away.

        The conversation during bypass happens unmediated, so a translation of
        it is worth nothing by the time it plays - it would arrive as a voice
        recapping a minute the user has already had. And the cap lives below
        the suppressed branch in tick(), so a backlog built while suppressed
        is never trimmed.
        """
        self.suppressed = value
        if value:
            self.flush()

    def _trim_locked(self) -> None:
        """Drop the head of the buffer, but only at a pause in the output.

        Backlog growth is this project's experimental result, not a nuisance,
        so the cap sits high and is expected never to fire in an ordinary
        call. When it does, cutting mid-word would be worse than running long,
        so a buffer with no quiet frame in it is left alone.
        """
        while len(self._pending) / TTS_BYTES_PER_S > self._lag_cap_s:
            cut = find_silence_boundary(self._pending)
            if not cut:
                return
            del self._pending[:cut]
            self.dropped_s += cut / TTS_BYTES_PER_S
            log.warning(
                "%s playout %.1fs behind; dropped %.1fs at a pause (%.1fs total)",
                self.direction.value,
                len(self._pending) / TTS_BYTES_PER_S,
                cut / TTS_BYTES_PER_S,
                self.dropped_s,
            )

    def _take_locked(self) -> bytes | None:
        if len(self._pending) >= CHUNK_BYTES:
            chunk = bytes(self._pending[:CHUNK_BYTES])
            del self._pending[:CHUNK_BYTES]
            self._starved = 0
            return chunk
        if self._pending and self._starved >= STARVE_LIMIT_TICKS:
            # The producer has stopped rather than merely fallen behind.
            # Padding here splices at most one chunk of silence onto a tail
            # that was ending anyway; padding on every tick, which is what
            # doing this unconditionally would mean, would stutter.
            chunk = bytes(self._pending) + b"\x00" * (CHUNK_BYTES - len(self._pending))
            self._pending.clear()
            self._starved = 0
            return chunk
        self._starved += 1
        return None

    def tick(self) -> bool:
        """Write exactly one chunk. True if it carried speech."""
        if self.suppressed:
            if self._duck is not None:
                self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            return False

        with self._lock:
            chunk = self._take_locked()

        if chunk is None:
            self._idle_ticks += 1
            if self._duck is not None and self._idle_ticks >= DUCK_HOLD_TICKS:
                self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            return False

        self._idle_ticks = 0
        self.spoken_s += CHUNK_MS / 1000
        if self._duck is not None:
            self._duck.close()
        self._sink.write(chunk)
        return True

    def run(self, stop: threading.Event) -> None:
        """Pace comes from the sink.

        pw-cat blocks on write once its buffer is full, so this loop runs at
        real time with no sleep. But PwCatSink.write() swallows a dead pipe and
        becomes a no-op, and a no-op never blocks - so a sink that dies
        mid-call removes the only thing pacing this loop and it would pin a
        core until hangup. The fallback wait is not belt-and-braces; it is the
        whole reason the `failed` flag is readable from here.

        The finally is the module's one fail-safe. Leaving the duck closed
        silences the person you are talking to and leaves them speaking to
        nobody, which is worse than this program not working at all.
        """
        try:
            while not stop.is_set():
                self.tick()
                if getattr(self._sink, "failed", False):
                    stop.wait(CHUNK_MS / 1000)
        finally:
            if self._duck is not None:
                self._duck.open()
            self._sink.close()


def earcon(duration_s: float = 0.25, frequency: float = 880.0, level: float = 0.25) -> bytes:
    """A short tone for the dead-air alarm.

    During a call you are looking at the other person, not at a dashboard, so
    the OUT direction failing silently has to make a sound. Generated rather
    than shipped as an asset, and with math.sin rather than numpy, because the
    audio path deliberately has no numpy in it.
    """
    import math
    import struct

    samples = int(TTS_BYTES_PER_S * duration_s) // 2
    amplitude = int(32767 * level)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * i / TTS_RATE)))
        for i in range(samples)
    )
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_playout.py -q
```

Expected: PASS, 15 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/playout.py tests/test_playout.py
git commit -m "Rewrite Playout as a chunk queue with output-driven ducking"
```

---

### Task 19: `DirectionInterpreter` — open, send, suspend

**The invariant this task establishes, and the next two must not break:**

> **Every state transition happens on the pump thread. The receive thread only records.**

`GoAway` sets a deadline, `Closed` sets a flag, `ResumptionHandle` stores a string — and the pump thread acts on them when the next block arrives. Transitioning from the receive thread would mean closing a session out from under the loop iterating it, and would need a second lock around everything.

`feed()` is public so tests drive the machine directly, with no threads and no timing.

**Files:**
- Create: `sidetap_live/interpreter.py`
- Test: `tests/test_interpreter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_interpreter.py
import pytest

from sidetap_live.activity import SpeechActivity
from sidetap_live.cost import Rates
from sidetap_live.interpreter import DirectionInterpreter, InterpreterConfig
from sidetap_live.metrics import Metrics
from sidetap_live.playout import Playout
from sidetap_live.preroll import PreRoll
from sidetap_live.types import (
    BLOCK_BYTES,
    IDLE_SUSPEND_S,
    AudioChunk,
    Direction,
    SessionState,
)
from tests.conftest import FakeAudioSink, FakeClock, FakeSessionFactory

SPEECH = b"\x00\x40" * (BLOCK_BYTES // 2)
SILENCE = b"\x00\x00" * (BLOCK_BYTES // 2)


def build(*, echo=False, idle_suspend=True, speaking=True):
    clock = FakeClock()
    sessions = FakeSessionFactory()
    metrics = Metrics()
    playout = Playout(Direction.IN, FakeAudioSink())
    detector = (lambda pcm: pcm == SPEECH) if speaking else None
    interpreter = DirectionInterpreter(
        InterpreterConfig(
            direction=Direction.IN,
            target_lang="en",
            echo=echo,
            idle_suspend=idle_suspend,
        ),
        sessions=sessions,
        playout=playout,
        activity=SpeechActivity(detector, clock),
        metrics=metrics,
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )
    return interpreter, sessions, metrics, clock


def block(pcm):
    return AudioChunk(track="remote", pcm=pcm, t_start=0.0)


def test_it_starts_suspended_and_sends_nothing():
    interpreter, sessions, _, _ = build()
    assert interpreter.state is SessionState.SUSPENDED
    interpreter.feed(block(SILENCE))
    assert sessions.opens == []


def test_speech_opens_a_session_with_the_configured_target_and_echo():
    interpreter, sessions, _, _ = build(echo=True)
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING
    assert sessions.opens == [("en", True, None)]


def test_waking_replays_the_preroll_so_the_onset_is_not_lost():
    """The speech that woke the session happened before there was a session."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SPEECH))
    # Two silent blocks of pre-roll, plus the block that woke it.
    assert len(sessions.sessions[0].sent) == 3 * BLOCK_BYTES


def test_running_sends_every_block_including_silence():
    """No gate. A model reasoning over continuous audio gets continuous audio."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    sent_after_wake = len(sessions.sessions[0].sent)
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SILENCE))
    assert len(sessions.sessions[0].sent) == sent_after_wake + 2 * BLOCK_BYTES


def test_idle_suspends_and_closes_the_session():
    interpreter, sessions, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S + 1)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.SUSPENDED
    assert sessions.sessions[0].closed is True
    assert metrics.snapshot().directions[Direction.IN].session_state is SessionState.SUSPENDED


def test_no_idle_suspend_keeps_the_session_open():
    interpreter, sessions, _, clock = build(idle_suspend=False)
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S * 10)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING
    assert sessions.sessions[0].closed is False


def test_without_a_detector_it_opens_at_once_and_never_suspends():
    """Degraded mode: no webrtcvad means no pause detection, so the session is
    opened on the first block and held for the whole call."""
    interpreter, sessions, _, clock = build(speaking=False)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING
    clock.advance(IDLE_SUSPEND_S * 10)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING


def test_input_audio_is_billed():
    interpreter, _, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    # 100 ms x 25 tokens/s x $3.50/M
    assert metrics.snapshot().cost_usd == pytest.approx(0.1 * 25 * 3.50 / 1e6, rel=1e-6)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_interpreter.py -q
```

Expected: FAIL, no module named `sidetap_live.interpreter`.

- [ ] **Step 3: Write `sidetap_live/interpreter.py`**

```python
"""One direction of the interpreter, end to end.

Instantiated twice. IN reads the remote track and speaks into your
headphones; OUT reads the mic and speaks into the virtual mic's sink. Nothing
here knows which is which beyond its config.

THE INVARIANT: every state transition happens on the pump thread. The receive
thread only records - GoAway sets a deadline, Closed sets a flag, a resumption
handle is stored - and the pump acts on them when the next block arrives.
Transitioning from the receive thread would close a session out from under the
loop iterating it, and would need a second lock around the whole machine.
"""

from __future__ import annotations

import logging
import queue as queue_module
import threading
from dataclasses import dataclass
from typing import Callable

from .activity import SpeechActivity
from .cost import Rates, input_seconds, output_seconds
from .metrics import Health, Metrics
from .playout import Playout
from .ports import Clock, SessionFactory
from .preroll import PreRoll
from .types import (
    IDLE_SUSPEND_S,
    ROTATE_PAUSE_S,
    TARGET_RATE,
    AudioChunk,
    AudioOut,
    Closed,
    Direction,
    GoAway,
    ResumptionHandle,
    SessionState,
    SourceText,
    TargetText,
    TranscriptEvent,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InterpreterConfig:
    direction: Direction
    target_lang: str
    # IN is False: a remote party already speaking your language produces no
    # output, no audio flows, the duck opens and you hear them raw. OUT must
    # be True - your real mic is never linked to the messenger, so no output
    # means the remote party hears nothing at all.
    echo: bool
    idle_suspend: bool = True


class DirectionInterpreter:
    def __init__(
        self,
        config: InterpreterConfig,
        *,
        sessions: SessionFactory,
        playout: Playout,
        activity: SpeechActivity,
        metrics: Metrics,
        clock: Clock,
        rates: Rates | None = None,
        preroll: PreRoll | None = None,
        on_event: Callable[[TranscriptEvent], None] | None = None,
        session_t0: float = 0.0,
    ):
        self._config = config
        self._sessions = sessions
        self._playout = playout
        self._activity = activity
        self._metrics = metrics
        self._clock = clock
        self._rates = rates or Rates()
        self._preroll = preroll or PreRoll()
        self._on_event = on_event
        self._session_t0 = session_t0

        self._state = SessionState.SUSPENDED
        self._session = None
        self._handle: str | None = None

        # Written by the receive thread, read by the pump thread.
        self._lock = threading.Lock()
        self._goaway_at: float | None = None
        self._dead: str | None = None
        self._speech_at: float | None = None

    @property
    def direction(self) -> Direction:
        return self._config.direction

    @property
    def state(self) -> SessionState:
        return self._state

    # ---------- the pump thread ----------

    def feed(self, chunk: AudioChunk) -> None:
        """Handle one captured block. Public so tests drive it without threads."""
        speaking = self._activity.observe(chunk.pcm)
        self._preroll.add(chunk.pcm)
        if speaking:
            with self._lock:
                if self._speech_at is None:
                    self._speech_at = self._clock.monotonic()

        if self._take_dead() is not None:
            self._reopen()

        if self._state is SessionState.SUSPENDED:
            if not self._should_wake(speaking):
                return
            self._open(replay=True)
        elif self._state is SessionState.DRAINING:
            self._rotate_if_due()
        elif self._should_suspend():
            self._suspend()
            return

        self._send(chunk.pcm)

    def pump(self, chunks, stop: threading.Event) -> None:
        """Drain a capture queue into the session until told to stop."""
        while not stop.is_set():
            try:
                chunk = chunks.get(timeout=0.25)
            except queue_module.Empty:
                continue
            try:
                self.feed(chunk)
            except Exception:
                # One malformed block must not take the direction down for the
                # rest of the call.
                log.exception("interpreter pump error (%s)", self.direction.value)
        self._close("shutdown")

    def _should_wake(self, speaking: bool) -> bool:
        # With no detector there is no onset to wait for, so open at once and
        # hold the session for the whole call - the documented degraded mode.
        return speaking or not self._activity.available

    def _should_suspend(self) -> bool:
        if not self._config.idle_suspend or not self._activity.available:
            return False
        return self._activity.silence_s() >= IDLE_SUSPEND_S

    def _open(self, *, replay: bool, handle: str | None = None) -> float:
        """Open a session. Returns seconds of pre-roll replayed into it."""
        self._set_state(SessionState.OPENING)
        self._session = self._sessions.open(
            self._config.target_lang, echo=self._config.echo, handle=handle
        )
        with self._lock:
            self._goaway_at = None
            self._dead = None
        threading.Thread(
            target=self._receive,
            args=(self._session,),
            daemon=True,
            name=f"recv-{self.direction.value}",
        ).start()
        self._set_state(SessionState.RUNNING)
        self._metrics.set_health(self.direction, session=Health.OK)

        if not replay:
            return 0.0
        blocks = self._preroll.drain()
        for block in blocks:
            self._send(block)
        return sum(len(b) for b in blocks) / (TARGET_RATE * 2)

    def _suspend(self) -> None:
        self._close("idle")
        self._set_state(SessionState.SUSPENDED)

    def _close(self, reason: str) -> None:
        session = self._session
        self._session = None
        if session is not None:
            log.info("%s session closed (%s)", self.direction.value, reason)
            session.close()

    def _send(self, pcm: bytes) -> None:
        if self._session is None:
            return
        self._session.send(pcm)
        seconds = input_seconds(len(pcm))
        self._metrics.add_cost(self._rates.input_usd(seconds))

    def _set_state(self, state: SessionState) -> None:
        self._state = state
        self._metrics.set_session_state(self.direction, state)

    def _take_dead(self) -> str | None:
        with self._lock:
            reason, self._dead = self._dead, None
            return reason
```

The three methods `_rotate_if_due`, `_reopen` and `_receive` are written in Tasks 20 and 21. Add these stubs now so the module imports, and **delete them in the tasks that replace them**:

```python
    def _rotate_if_due(self) -> None:
        raise NotImplementedError("Task 20")

    def _reopen(self) -> None:
        raise NotImplementedError("Task 21")

    def _receive(self, session) -> None:
        raise NotImplementedError("Task 21")
```

- [ ] **Step 4: Make the receive stub harmless for this task's tests**

The tests in this task never produce events, but `_open` starts a receive thread that would raise immediately. Temporarily make the stub a no-op loop so the thread exits cleanly:

```python
    def _receive(self, session) -> None:
        for _ in session.events():   # replaced in Task 21
            pass
```

- [ ] **Step 5: Run to verify it passes**

```bash
uv run pytest tests/test_interpreter.py -q
```

Expected: PASS, 8 tests.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/interpreter.py tests/test_interpreter.py
git commit -m "Add DirectionInterpreter: open, send, idle-suspend"
```

### Task 20: Rotation — clean at a pause, forced at the deadline

**Files:**
- Modify: `sidetap_live/interpreter.py`
- Test: `tests/test_interpreter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_interpreter.py`:

```python
from sidetap_live.types import ROTATE_PAUSE_S, GoAway


def goaway(interpreter, seconds):
    """Record a GoAway the way the receive thread would."""
    interpreter.note_goaway(GoAway(time_left_s=seconds))


def test_goaway_moves_to_draining_without_rotating_yet():
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter, 30.0)
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.DRAINING
    assert len(sessions.sessions) == 1


def test_a_pause_rotates_cleanly_and_replays_nothing():
    interpreter, sessions, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter, 30.0)
    clock.advance(ROTATE_PAUSE_S + 0.1)
    interpreter.feed(block(SILENCE))

    assert interpreter.state is SessionState.RUNNING
    assert len(sessions.sessions) == 2
    assert sessions.sessions[0].closed is True
    # A fresh session, not a resumed one: the pause is the whole point.
    assert sessions.opens[1] == ("en", False, None)

    state = metrics.snapshot().directions[Direction.IN]
    assert (state.rotations, state.forced_rotations) == (1, 0)
    assert state.replayed_s == 0.0


def test_the_deadline_forces_a_rotation_that_replays_the_preroll():
    interpreter, sessions, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_handle("h-1")
    goaway(interpreter, 1.0)
    clock.advance(2.0)
    # Still speaking, so no pause has arrived.
    interpreter.feed(block(SPEECH))

    assert len(sessions.sessions) == 2
    # Forced seams resume, because there is context mid-sentence to keep.
    assert sessions.opens[1] == ("en", False, "h-1")
    state = metrics.snapshot().directions[Direction.IN]
    assert (state.rotations, state.forced_rotations) == (1, 1)
    assert state.replayed_s > 0.0


def test_a_forced_rotation_loses_no_audio():
    interpreter, sessions, _, clock = build()
    for _ in range(5):
        interpreter.feed(block(SPEECH))
    interpreter.note_handle("h-1")
    goaway(interpreter, 1.0)
    clock.advance(2.0)
    interpreter.feed(block(SPEECH))
    # The pre-roll (1 s = 10 blocks, but only 6 were ever captured) is pushed
    # into the new session ahead of the block that triggered the rotation.
    assert len(sessions.sessions[1].sent) >= 6 * BLOCK_BYTES


def test_without_a_detector_every_rotation_is_forced():
    interpreter, sessions, metrics, clock = build(speaking=False)
    interpreter.feed(block(SILENCE))
    goaway(interpreter, 1.0)
    clock.advance(2.0)
    interpreter.feed(block(SILENCE))
    assert metrics.snapshot().directions[Direction.IN].forced_rotations == 1
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_interpreter.py -q
```

Expected: FAIL with `AttributeError: 'DirectionInterpreter' object has no attribute 'note_goaway'`.

- [ ] **Step 3: Replace the `_rotate_if_due` stub and add the two recorders**

```python
    # ---------- recorded by the receive thread, acted on by the pump ----------

    def note_goaway(self, event: GoAway) -> None:
        """The connection will end. Start looking for somewhere to rotate.

        Public because Task 21's receive loop calls it, and because a test
        needs to inject one without threads.
        """
        with self._lock:
            self._goaway_at = self._clock.monotonic() + event.time_left_s
        if self._state is SessionState.RUNNING:
            self._set_state(SessionState.DRAINING)

    def note_handle(self, handle: str) -> None:
        self._handle = handle

    # ---------- rotation, on the pump thread ----------

    def _rotate_if_due(self) -> None:
        """Rotate inside a pause if one arrives; at the deadline if none does.

        The clean path deliberately opens a FRESH session rather than resuming:
        the spec's assumption is that literal interpretation carries almost no
        context across a sentence boundary, so a handle would buy nothing. The
        forced path resumes, because it lands mid-sentence where there IS
        context to keep. If docs/experiments/02-voice-stability.md showed the
        voice changing at a seam, this whole strategy is wrong and the spec's
        Session continuity decision must be reopened - see Task 6.
        """
        with self._lock:
            deadline = self._goaway_at
        paused = (
            self._activity.available
            and self._activity.silence_s() >= ROTATE_PAUSE_S
        )
        expired = deadline is not None and self._clock.monotonic() >= deadline
        if paused:
            self._rotate(forced=False)
        elif expired:
            self._rotate(forced=True)

    def _rotate(self, *, forced: bool) -> None:
        old = self._session
        # Drain BEFORE opening: _open(replay=True) drains it itself, and a
        # clean rotation must replay nothing at all.
        replayed_s = self._open(replay=forced, handle=self._handle if forced else None)
        if old is not None:
            old.close()
        self._metrics.add_rotation(
            self.direction, forced=forced, replayed_s=replayed_s
        )
        log.info(
            "%s rotated session (%s), replayed %.1fs",
            self.direction.value,
            "forced" if forced else "clean",
            replayed_s,
        )
```

Delete the `_rotate_if_due` stub added in Task 19.

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_interpreter.py -q
```

Expected: PASS, 13 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/interpreter.py tests/test_interpreter.py
git commit -m "Rotate sessions at a pause, forced at GoAway's deadline"
```

---

### Task 21: The receive side — audio out, transcript, offset, dead session

Three things fall out of one piece of state here. `_speech_at` is set when speech goes in and cleared when audio comes out, so it *is* the offset measurement and it *is* the dead-air alarm — no separate watcher class is needed for either.

**Files:**
- Modify: `sidetap_live/interpreter.py`
- Test: `tests/test_interpreter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_interpreter.py`:

```python
from sidetap_live.metrics import Health
from sidetap_live.types import (
    DEAD_AIR_S,
    TTS_BYTES_PER_S,
    AudioOut,
    Closed,
    ResumptionHandle,
    SourceText,
    TargetText,
)


def build_with_events(**kwargs):
    events = []
    interpreter, sessions, metrics, clock = build(**kwargs)
    interpreter._on_event = events.append
    return interpreter, sessions, metrics, clock, events


def test_audio_out_reaches_playout_and_is_billed():
    interpreter, _, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=b"\x00" * TTS_BYTES_PER_S))

    assert interpreter._playout.backlog_s() == pytest.approx(1.0)
    state = metrics.snapshot().directions[Direction.IN]
    assert state.backlog_s == pytest.approx(1.0)
    # 1 s of output at 25 tokens/s x $21/M, on top of the input already billed.
    assert metrics.snapshot().cost_usd > 25 * 21.0 / 1e6 * 0.9


def test_both_transcriptions_become_separate_events():
    interpreter, _, metrics, _, events = build_with_events()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(SourceText(text="privet"))
    interpreter.note_event(TargetText(text="hello"))

    assert [(e.kind, e.text) for e in events] == [
        ("source", "privet"),
        ("target", "hello"),
    ]
    state = metrics.snapshot().directions[Direction.IN]
    assert (state.source, state.target) == ("privet", "hello")


def test_offset_is_speech_onset_to_first_audio_out():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(1.4)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].offset_s == pytest.approx(1.4)


def test_offset_measures_the_stretch_not_every_chunk():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(1.4)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    clock.advance(5.0)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].offset_s == pytest.approx(1.4)


def test_dead_air_fires_when_speech_produces_nothing():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(DEAD_AIR_S + 1)
    interpreter.feed(block(SPEECH))
    assert metrics.snapshot().directions[Direction.IN].dead_air is True


def test_audio_out_clears_the_dead_air_alarm():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(DEAD_AIR_S + 1)
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].dead_air is False


def test_a_dead_session_is_reopened_on_the_last_handle():
    interpreter, sessions, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(ResumptionHandle(handle="h-9"))
    interpreter.note_event(Closed(reason="boom"))
    assert metrics.snapshot().directions[Direction.IN].session is Health.OK

    interpreter.feed(block(SPEECH))
    assert len(sessions.sessions) == 2
    assert sessions.opens[1] == ("en", False, "h-9")


def test_a_dead_session_while_suspended_is_not_reopened():
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S + 1)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.SUSPENDED

    interpreter.note_event(Closed(reason="closed by us"))
    interpreter.feed(block(SILENCE))
    assert len(sessions.sessions) == 1


def test_goaway_arriving_as_an_event_enters_draining():
    interpreter, _, _, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(GoAway(time_left_s=30.0))
    assert interpreter.state is SessionState.DRAINING
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_interpreter.py -q
```

Expected: FAIL with `AttributeError: ... has no attribute 'note_event'`.

- [ ] **Step 3: Fix the dead-session guard in `feed()`**

Task 19 wrote:

```python
        if self._take_dead() is not None:
            self._reopen()
```

Replace it with — a session closed *by us* on the way into SUSPENDED also reports `Closed`, and reopening on that would undo the suspension immediately:

```python
        if self._state is not SessionState.SUSPENDED and self._take_dead() is not None:
            self._reopen()
```

- [ ] **Step 4: Add the dead-air check to `feed()`**

Immediately before `self._send(chunk.pcm)` at the end of `feed()`:

```python
        self._check_dead_air()
```

- [ ] **Step 5: Replace the `_reopen` and `_receive` stubs**

```python
    # ---------- the receive thread ----------

    def _receive(self, session) -> None:
        """Drain one session's events. Records only - never transitions.

        Ends when session.events() returns, which close() makes happen. One
        thread per session, so a rotation retires the old one naturally
        instead of needing a flag read under a lock on every event.
        """
        for event in session.events():
            try:
                self.note_event(event)
            except Exception:
                # A malformed event must not end the direction's receive loop
                # for the rest of the session.
                log.exception("interpreter event error (%s)", self.direction.value)

    def note_event(self, event) -> None:
        """Handle one SessionEvent. Public so tests drive it without threads."""
        match event:
            case AudioOut(pcm=pcm):
                self._playout.submit(pcm)
                self._metrics.set_backlog_s(self.direction, self._playout.backlog_s())
                # submit() may have trimmed at a silence boundary, so read the
                # drop total back here rather than letting Playout reach into
                # Metrics - the dependency runs one way only.
                self._metrics.set_dropped_s(self.direction, self._playout.dropped_s)
                self._metrics.add_cost(self._rates.output_usd(output_seconds(len(pcm))))
                self._note_spoke()
            case SourceText(text=text):
                self._metrics.set_text(self.direction, source=text)
                self._emit("source", text)
            case TargetText(text=text):
                self._metrics.set_text(self.direction, target=text)
                self._emit("target", text)
            case GoAway():
                self.note_goaway(event)
            case ResumptionHandle(handle=handle):
                self.note_handle(handle)
            case Closed(reason=reason):
                with self._lock:
                    self._dead = reason
            case _:
                log.debug("ignoring %r", event)

    def _note_spoke(self) -> None:
        """First audio since speech started fixes this stretch's offset.

        Clearing _speech_at is what makes the metric measure the STRETCH
        rather than every chunk: the second and later chunks of the same
        utterance find it already None and leave the figure alone.
        """
        with self._lock:
            started, self._speech_at = self._speech_at, None
        if started is not None:
            self._metrics.set_offset_s(
                self.direction, self._clock.monotonic() - started
            )
        self._metrics.set_dead_air(self.direction, False)

    def _check_dead_air(self) -> None:
        """Speech went in and nothing has come out.

        Matters most on OUT: IN degrades gracefully now, because no audio out
        opens the duck and the user hears the unmediated call. On OUT there is
        no raw path to fall through to - the remote party hears nothing and
        has no way to know.
        """
        with self._lock:
            started = self._speech_at
        if started is not None and self._clock.monotonic() - started > DEAD_AIR_S:
            self._metrics.set_dead_air(self.direction, True)

    def _emit(self, kind: str, text: str) -> None:
        if self._on_event is None:
            return
        self._on_event(
            TranscriptEvent(
                t=self._clock.monotonic() - self._session_t0,
                direction=self.direction,
                kind=kind,
                text=text,
            )
        )

    def _reopen(self) -> None:
        """The session died with no GoAway. Come back on the last handle."""
        self._metrics.set_health(self.direction, session=Health.FAILED)
        self._close("died")
        self._open(replay=True, handle=self._handle)
```

Add `DEAD_AIR_S` to the `from .types import (...)` block.

- [ ] **Step 6: Run to verify it passes**

```bash
uv run pytest tests/test_interpreter.py -q
```

Expected: PASS, 22 tests.

- [ ] **Step 7: Run the whole suite**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add sidetap_live/interpreter.py tests/test_interpreter.py
git commit -m "Wire the receive side: audio, transcript, offset, dead session recovery"
```

### Task 22: `transcript.py` — events, not pairs

sidetap pairs source and target into one record because its segmenter defines the units. Here the two transcription streams drift independently and any pairing would be invented rather than observed, so the file carries timestamped events and the Markdown interleaves them.

**Files:**
- Create: `sidetap_live/transcript.py`
- Test: `tests/test_transcript.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transcript.py
import json

from sidetap_live.transcript import ENGINE, EventTranscript, render_markdown
from sidetap_live.types import Direction, TranscriptEvent


def event(t, direction, kind, text):
    return TranscriptEvent(t=t, direction=direction, kind=kind, text=text)


def test_the_header_names_the_engine_so_transcripts_are_comparable(tmp_path):
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.close()
    first = json.loads(transcript.jsonl_path.read_text().splitlines()[0])
    assert first["meta"]["engine"] == ENGINE


def test_events_are_appended_and_flushed_per_event(tmp_path):
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.write(event(1.0, Direction.IN, "source", "privet"))
    # Readable before close: an unclean exit must leave everything on disk.
    rows = [json.loads(line) for line in transcript.jsonl_path.read_text().splitlines()]
    assert rows[1] == {
        "t": 1.0,
        "direction": "in",
        "kind": "source",
        "text": "privet",
    }


def test_markdown_interleaves_both_directions_chronologically(tmp_path):
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.write(event(2.0, Direction.OUT, "source", "how are you"))
    transcript.write(event(1.0, Direction.IN, "source", "privet"))
    transcript.write(event(1.4, Direction.IN, "target", "hello"))
    text = transcript.close().read_text()
    assert text.index("privet") < text.index("hello") < text.index("how are you")


def test_writing_after_close_is_ignored_not_raised(tmp_path):
    """Playout threads outlive shutdown's join deadline by design - the graph
    matters more than a tidy exit - and a ValueError from a daemon thread
    would print a traceback over the TUI."""
    transcript = EventTranscript(tmp_path, session="s1")
    transcript.close()
    transcript.write(event(1.0, Direction.IN, "source", "late"))
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_transcript.py -q
```

Expected: FAIL, no module named `sidetap_live.transcript`.

- [ ] **Step 3: Write `sidetap_live/transcript.py`**

```python
"""Durable transcript: append-only JSONL of events, Markdown at close.

Deliberately NOT source/target pairs. inputAudioTranscription and
outputAudioTranscription arrive as two independently-drifting streams, so a
pairing would be this program's invention rather than an observation. The
Markdown interleaves them by time instead, which is honest about what is
actually known.

sidetap emits the same schema with engine "cascade" (see that repository's
transcript.py), which is what makes the two comparable.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
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


def render_markdown(session: str, events: list[TranscriptEvent]) -> str:
    """Chronological, both directions and both streams interleaved."""
    lines = [f"# Interpretation transcript {session}", "", f"_engine: {ENGINE} ({MODEL})_", ""]
    last: tuple[str, str] | None = None
    for event in sorted(events, key=lambda e: e.t):
        heading = (LABELS[event.direction], event.kind)
        if heading != last:
            lines.append("")
            label, kind = heading
            marker = "" if kind == "source" else " → "
            lines.append(f"**{label}{marker}** _{hhmmss(event.t)}_")
            last = heading
        lines.append(event.text)
    return "\n".join(lines) + "\n"


class EventTranscript:
    def __init__(self, outdir: Path, session: str | None = None):
        outdir.mkdir(parents=True, exist_ok=True)
        # Sub-second resolution: whole seconds meant two runs started within
        # the same second appended into one file.
        self.session = session or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.jsonl_path = outdir / f"{self.session}.jsonl"
        self.md_path = outdir / f"{self.session}.md"
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
                    "started": datetime.now(timezone.utc).isoformat(),
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

    def close(self) -> Path:
        with self._lock:
            if self._closed:
                return self.md_path
            self._closed = True
            self._jsonl.close()
            self.md_path.write_text(
                render_markdown(self.session, self._events), encoding="utf-8"
            )
        return self.md_path
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_transcript.py -q
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/transcript.py tests/test_transcript.py
git commit -m "Write the transcript as timestamped events rather than invented pairs"
```

---

## Phase 3 — Instrumentation and interface

### Task 23: `OverlapWatch` — the metric that measures the humans

Every other number describes the system. This one describes whether the two people stopped taking turns, which is the question the project exists to answer.

**Files:**
- Modify: `sidetap_live/activity.py`
- Test: `tests/test_activity.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity.py`:

```python
from sidetap_live.activity import OverlapWatch
from sidetap_live.types import Direction


def two_tracks(clock, in_speaking: bool, out_speaking: bool):
    tracks = {
        Direction.IN: SpeechActivity(always(in_speaking), clock),
        Direction.OUT: SpeechActivity(always(out_speaking), clock),
    }
    for activity in tracks.values():
        activity.observe(SPEECH if activity._detect(SPEECH) else SILENCE)
    return tracks


def test_no_overlap_when_they_take_turns():
    clock = FakeClock()
    tracks = two_tracks(clock, True, False)
    watch = OverlapWatch(tracks, clock)
    clock.advance(10.0)
    assert watch.sample() == pytest.approx(0.0)


def test_full_overlap_when_both_talk():
    clock = FakeClock()
    tracks = two_tracks(clock, True, True)
    watch = OverlapWatch(tracks, clock)
    clock.advance(10.0)
    assert watch.sample() == pytest.approx(100.0)


def test_overlap_is_a_running_fraction_of_wall_clock():
    clock = FakeClock()
    tracks = two_tracks(clock, True, True)
    watch = OverlapWatch(tracks, clock)
    clock.advance(5.0)
    watch.sample()
    tracks[Direction.OUT]._detect = always(False)
    tracks[Direction.OUT].observe(SILENCE)
    clock.advance(15.0)
    assert watch.sample() == pytest.approx(25.0)


def test_it_reports_zero_before_any_time_has_passed():
    clock = FakeClock()
    assert OverlapWatch(two_tracks(clock, True, True), clock).sample() == 0.0
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_activity.py -q
```

Expected: FAIL with `ImportError: cannot import name 'OverlapWatch'`.

- [ ] **Step 3: Append to `sidetap_live/activity.py`**

Add `from .types import Direction, TARGET_RATE` to the imports, then:

```python
class OverlapWatch:
    """Fraction of wall clock where BOTH tracks carry speech at once.

    The only metric in this package that measures the people rather than the
    program. sidetap's full-replacement routing plus finals-only commit
    forbids overlap by construction, so under it this sits near zero; if the
    two parties naturally begin talking over each other here and it keeps
    working, this rises. That is the project's chosen axis in its most direct
    form.

    Sampled by the session health poller rather than computed per block,
    because it is a property of the two tracks together and neither
    direction's pump can see the other.
    """

    def __init__(self, tracks: dict[Direction, SpeechActivity], clock: Clock):
        self._tracks = tracks
        self._clock = clock
        self._last = clock.monotonic()
        self._overlap_s = 0.0
        self._total_s = 0.0

    def sample(self) -> float:
        now = self._clock.monotonic()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0:
            self._total_s += elapsed
            # Attributed to the interval that just ENDED, using the speaking
            # flags as they stood through it. Sampling the flags and the clock
            # at the same instant is what keeps this a time integral rather
            # than a count of coincidences.
            if self._tracks and all(a.speaking for a in self._tracks.values()):
                self._overlap_s += elapsed
        return self.pct

    @property
    def pct(self) -> float:
        if self._total_s <= 0:
            return 0.0
        return 100.0 * self._overlap_s / self._total_s
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_activity.py -q
```

Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add sidetap_live/activity.py tests/test_activity.py
git commit -m "Add OverlapWatch: the metric that measures the humans"
```

### Task 24: `tui.py` — a new headline row

**Files:**
- Create: `sidetap_live/tui.py`
- Test: `tests/test_tui.py`

- [ ] **Step 1: Copy and rename**

```bash
cp "$SIDETAP/sidetap/tui.py" sidetap_live/tui.py
cp "$SIDETAP/tests/test_tui.py" tests/test_tui.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g; s/\bSidetapApp\b/SidetapLiveApp/g' sidetap_live/tui.py tests/test_tui.py
```

- [ ] **Step 2: Write the failing test**

Replace the body of `tests/test_tui.py` with:

```python
import pytest

from sidetap_live.metrics import Health, Metrics
from sidetap_live.tui import SidetapLiveApp, format_lag, format_rotations, health_marker
from sidetap_live.types import Direction, SessionState


def test_markers_distinguish_the_three_states():
    assert len({health_marker(h) for h in Health}) == 3


def test_rotations_show_the_clean_forced_split():
    """The split is what says whether the pause assumption survived contact."""
    assert format_rotations(0, 0) == "—"
    assert format_rotations(5, 0) == "5"
    assert format_rotations(5, 2) == "5 (2 forced)"


def test_lag_is_one_decimal():
    assert format_lag(1.234) == "1.2s"


@pytest.mark.asyncio
async def test_the_pane_shows_both_transcription_streams():
    metrics = Metrics()
    metrics.set_text(Direction.IN, source="privet", target="hello")
    metrics.set_backlog_s(Direction.IN, 0.4)
    metrics.set_session_state(Direction.IN, SessionState.RUNNING)

    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Static

        assert "privet" in str(app.query_one("#source-in", Static).renderable)
        assert "hello" in str(app.query_one("#target-in", Static).renderable)
        assert "0.4s" in str(app.query_one("#stats-in", Static).renderable)


@pytest.mark.asyncio
async def test_overlap_and_cost_are_in_the_subtitle():
    metrics = Metrics()
    metrics.set_overlap_pct(12.5)
    metrics.add_cost(1.23)
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "overlap 12%" in app.sub_title
        assert "$1.23" in app.sub_title
```

- [ ] **Step 3: Run to verify it fails**

```bash
uv run pytest tests/test_tui.py -q
```

Expected: FAIL with `ImportError: cannot import name 'format_rotations'`.

- [ ] **Step 4: Edit `sidetap_live/tui.py`**

Change the import line to:

```python
from .types import Direction, SessionState
```

Delete `format_latency` entirely and add:

```python
def format_rotations(total: int, forced: int) -> str:
    """The clean/forced split, not just a count.

    A call that rotated six times cleanly behaved as designed; one that forced
    four of six did not, and the spec's pause assumption is what needs
    revisiting. A bare total hides the difference.
    """
    if total == 0:
        return "—"
    return f"{total}" if forced == 0 else f"{total} ({forced} forced)"


def format_state(state: SessionState) -> str:
    return state.value
```

In `compose()`, replace the three text Statics with two — delete the `interim-{suffix}` line and keep:

```python
                Static("", classes="interim", id=f"source-{suffix}"),
                Static("", classes="target", id=f"target-{suffix}"),
```

In `refresh_from_metrics()`, replace the per-direction body with:

```python
            self.query_one(f"#source-{suffix}", Static).update(state.source)
            self.query_one(f"#target-{suffix}", Static).update(state.target)
            # The two alarms are named, not merely coloured. They point at
            # opposite ends of the pipeline - NO AUDIO means nothing is
            # arriving to work on, DEAD AIR means speech went in and nothing
            # came out - and a user who cannot tell them apart cannot act on
            # either.
            if state.no_audio:
                alarm = "  NO AUDIO ARRIVING"
            elif state.dead_air:
                alarm = "  DEAD AIR"
            else:
                alarm = ""
            self.query_one(f"#stats-{suffix}", Static).update(
                f"session {health_marker(state.session)} {format_state(state.session_state)}   "
                f"backlog {format_lag(state.backlog_s)}   "
                f"offset {format_lag(state.offset_s)}   "
                f"rot {format_rotations(state.rotations, state.forced_rotations)}   "
                f"dropped {format_lag(state.dropped_s)}/{state.capture_dropped}"
                f"{alarm}"
            )
            pane = self.query_one(f"#pane-{suffix}")
            pane.set_class(state.dead_air or state.no_audio, "alarm")
```

Replace the sub_title block with:

```python
        # Overlap leads because it is the result, not a diagnostic: it is the
        # fraction of the call where both people were talking at once.
        bypassed = "BYPASSED  " if snapshot.bypassed else ""
        self.sub_title = (
            f"{bypassed}overlap {snapshot.overlap_pct:.0f}%  "
            f"est. ${snapshot.cost_usd:.2f}"
        )
```

`DirectionState.dropped_s` and `Metrics.set_dropped_s` already exist from Task 11; nothing further is needed in `metrics.py` here.

- [ ] **Step 5: Run to verify it passes**

```bash
uv run pytest tests/test_tui.py -q
```

Expected: PASS, 5 tests.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/tui.py sidetap_live/metrics.py tests/test_tui.py
git commit -m "Replace the TUI's stage row with backlog, offset and rotations"
```

### Task 25: `doctor.py` — one live session instead of three cloud APIs

Two behavioural changes beyond swapping the API checks.

**The activity check stops being fatal.** In sidetap a missing `webrtcvad` multiplied the bill roughly twentyfold, silently, so `doctor` failed loudly. Here it costs nothing — it degrades rotation to forced-only and disables idle-suspend. That is a warning, and `Check` grows a `warn` field to say so.

**The virtual-mic check and installer are untouched.** sidetap_live shares that device deliberately; see Task 10.

**Files:**
- Create: `sidetap_live/doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Copy**

```bash
cp "$SIDETAP/sidetap/doctor.py" sidetap_live/doctor.py
cp "$SIDETAP/tests/test_doctor.py" tests/test_doctor.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g' tests/test_doctor.py
```

- [ ] **Step 2: Write the failing test**

Delete every test in `tests/test_doctor.py` that references `check_apis`, `check_asr_model`, `check_credentials` or `check_vad`, and append:

```python
from sidetap_live.doctor import (
    Check,
    check_activity,
    check_api_key,
    check_live_session,
    render_report,
)


def test_a_missing_key_fails():
    check = check_api_key(env={})
    assert check.ok is False
    assert "GEMINI_API_KEY" in check.detail


def test_a_present_key_is_not_echoed():
    """Never print a credential, not even partially."""
    check = check_api_key(env={"GEMINI_API_KEY": "sk-secret-value"})
    assert check.ok is True
    assert "secret" not in check.detail


def test_a_missing_detector_warns_rather_than_fails():
    """Unlike sidetap, where it multiplied the bill twentyfold in silence."""
    check = check_activity(detector=None)
    assert check.ok is True
    assert check.warn is True
    assert "idle-suspend" in check.detail


def test_a_working_detector_neither_fails_nor_warns():
    check = check_activity(detector=lambda pcm: True)
    assert (check.ok, check.warn) == (True, False)


def test_a_live_session_that_opens_passes():
    opened = []

    def factory(target_lang, *, echo, handle=None):
        opened.append(target_lang)

        class _Session:
            def send(self, pcm): ...
            def events(self): return iter(())
            def close(self): ...

        return _Session()

    check = check_live_session(factory)
    assert check.ok is True
    assert opened == ["en"]


def test_a_live_session_that_raises_fails_with_the_reason():
    def factory(target_lang, *, echo, handle=None):
        raise RuntimeError("PERMISSION_DENIED: model not available")

    check = check_live_session(factory)
    assert check.ok is False
    assert "PERMISSION_DENIED" in check.detail


def test_the_report_marks_warnings_distinctly():
    report = render_report([Check("activity", True, "degraded", warn=True)])
    assert "WARN" in report
    # A warning must not claim everything passed.
    assert "All checks passed" not in report
```

- [ ] **Step 3: Run to verify it fails**

```bash
uv run pytest tests/test_doctor.py -q
```

Expected: FAIL with `ImportError: cannot import name 'check_api_key'`.

- [ ] **Step 4: Edit `sidetap_live/doctor.py`**

Add `warn` to `Check`:

```python
@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    # A check that passed but degraded something. Distinct from ok=False so
    # that a missing speech detector does not read as a broken install.
    warn: bool = False
```

Delete `check_credentials`, `check_apis`, `check_asr_model` and `check_vad` entirely, along with the `_explain` helper if nothing else uses it. Add:

```python
def check_api_key(env: dict[str, str] | None = None) -> Check:
    """Is there a key at all?

    This project uses GEMINI_API_KEY and nothing else. It has no GCP
    credentials, no ADC and no --project: gemini-3.5-live-translate-preview
    is Developer API only. Never print any part of the value.
    """
    import os

    environ = os.environ if env is None else env
    if not environ.get("GEMINI_API_KEY"):
        return Check(
            "api key",
            False,
            "GEMINI_API_KEY is not set. Get one at aistudio.google.com/apikey",
        )
    return Check("api key", True, "GEMINI_API_KEY is set")


def check_activity(detector=None) -> Check:
    """Can we detect pauses?

    Not fatal, unlike sidetap's equivalent. There the silence gate was the
    difference between $0.10 and $2 an hour idle and its absence was silent;
    here nothing is gated, so a missing detector costs no money. It costs two
    behaviours: session rotation can no longer wait for a pause and always
    lands mid-speech, and idle-suspend never fires.
    """
    if detector is None:
        from .activity import webrtc_detector

        detector = webrtc_detector()
    if detector is None:
        return Check(
            "speech activity",
            True,
            "webrtcvad unavailable - every session rotation will be forced "
            "and idle-suspend is disabled. Run: uv sync",
            warn=True,
        )
    return Check("speech activity", True, "webrtcvad available")


def check_live_session(factory) -> Check:
    """Open one real session and close it.

    One cheap round trip here fails in a second, rather than two minutes into
    a live conversation with the graph already rewired.
    """
    try:
        session = factory("en", echo=False)
    except Exception as exc:
        return Check("live session", False, f"{type(exc).__name__}: {exc}")
    try:
        session.close()
    except Exception as exc:
        return Check("live session", False, f"opened but failed to close: {exc}")
    return Check("live session", True, "opened and closed a live-translate session")
```

Replace `render_report` with:

```python
def render_report(checks: list[Check]) -> str:
    if not checks:
        return "  No checks ran."
    width = max((len(c.name) for c in checks), default=0)
    lines = []
    for check in checks:
        status = "FAIL" if not check.ok else ("WARN" if check.warn else "OK  ")
        lines.append(f"  {status}  {check.name:<{width}}  {check.detail}")
    if all(c.ok and not c.warn for c in checks):
        lines.append("")
        lines.append("  All checks passed.")
    return "\n".join(lines)
```

- [ ] **Step 5: Run to verify it passes**

```bash
uv run pytest tests/test_doctor.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/doctor.py tests/test_doctor.py
git commit -m "Replace doctor's three cloud API checks with one live session"
```

---

## Phase 4 — Wiring

### Task 26: `cli.py` — the flags that survived

Nine flags go, because nothing behind them exists any more: `--project`, `--region`, `--mt-region`, `--tts-region`, `--mt-model`, `--voice-in`, `--voice-out`, `--phrase`, `--speaking-rate-in/out`.

`--phrase` is worth a note in the help text rather than silent removal. sidetap boosts recognition of names and jargon through Speech-to-Text phrase hints; this model exposes no equivalent, so names are at the model's mercy. That is a quality difference the comparison should surface, not a gap to paper over.

**Files:**
- Create: `sidetap_live/cli.py`, `sidetap_live/__main__.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Copy**

```bash
cp "$SIDETAP/sidetap/cli.py" sidetap_live/cli.py
cp "$SIDETAP/sidetap/__main__.py" sidetap_live/__main__.py
cp "$SIDETAP/tests/test_cli.py" tests/test_cli.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g; s/\bimport sidetap\b/import sidetap_live/g; s/\bfrom sidetap import\b/from sidetap_live import/g' tests/test_cli.py sidetap_live/__main__.py
```

- [ ] **Step 2: Write the failing test**

Delete every test in `tests/test_cli.py` referencing a removed flag or `speaking_rate`, and append:

```python
import pytest

from sidetap_live.cli import build_parser


def parse(*argv):
    return build_parser().parse_args(["run", "--app", "zoom",
                                      "--their-lang", "ru-RU",
                                      "--my-lang", "en-US", *argv])


def test_the_cascade_flags_are_gone():
    for flag in ("--project", "--region", "--mt-region", "--tts-region",
                 "--mt-model", "--voice-in", "--voice-out", "--phrase",
                 "--speaking-rate-in"):
        with pytest.raises(SystemExit):
            parse(flag, "x")


def test_duck_level_defaults_to_full_replacement():
    assert parse().duck_level == 0.0
    assert parse("--duck-level", "0.2").duck_level == 0.2


def test_duck_level_is_bounded():
    with pytest.raises(SystemExit):
        parse("--duck-level", "1.5")


def test_echo_out_defaults_on_because_silence_reaches_nobody():
    assert parse().echo_out is True
    assert parse("--no-echo-out").echo_out is False


def test_idle_suspend_defaults_on():
    assert parse().idle_suspend is True
    assert parse("--no-idle-suspend").idle_suspend is False


def test_lag_cap_survives_with_the_higher_default():
    from sidetap_live.types import LAG_CAP_S

    assert parse().lag_cap == LAG_CAP_S
```

- [ ] **Step 3: Run to verify it fails**

```bash
uv run pytest tests/test_cli.py -q
```

Expected: FAIL with `AttributeError: 'Namespace' object has no attribute 'duck_level'`.

- [ ] **Step 4: Edit `sidetap_live/cli.py`**

Change the imports at the top — drop `from .tts import MAX_SPEAKING_RATE, MIN_SPEAKING_RATE` and the `speaking_rate` helper function entirely.

Delete the nine `cloud.add_argument(...)` and `langs.add_argument(...)` calls for the removed flags, and the four `doctor.add_argument` calls for `--project`, `--region`, `--mt-region`, `--tts-region`, `--model`.

Add a `duck_level` type checker beside where `speaking_rate` was:

```python
def duck_level(value: str) -> float:
    """0.0 replaces the original entirely; 0.2 is interpreter-booth mode.

    Bounded rather than free: above 1.0 wpctl would AMPLIFY the original over
    the translation, which is the opposite of ducking and sounds like the
    program is broken rather than misconfigured.
    """
    level = float(value)
    if not 0.0 <= level <= 1.0:
        raise argparse.ArgumentTypeError("--duck-level must be between 0.0 and 1.0")
    return level
```

Add to the `run` subparser's output group:

```python
    out.add_argument(
        "--duck-level",
        type=duck_level,
        default=0.0,
        metavar="0.0-1.0",
        help="how loud the remote party's original stays while the "
             "translation speaks: 0.0 replaces it (default), 0.2 holds it "
             "under the way an interpreting booth does",
    )
    out.add_argument(
        "--echo-out",
        dest="echo_out",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="when you already speak their language, still send synthesised "
             "audio. On by default: your real mic is never linked to the "
             "messenger, so silence means they hear nothing at all",
    )
    out.add_argument(
        "--idle-suspend",
        dest="idle_suspend",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=f"close the session after {int(IDLE_SUSPEND_S)}s of silence and "
             "reopen on speech. On by default - leaving it off bills "
             "continuously for a session nobody is using",
    )
```

Change the `--lag-cap` import line to `from .types import IDLE_SUSPEND_S, LAG_CAP_S`.

In `_doctor`, replace the import block with:

```python
    from .doctor import (
        check_activity,
        check_api_key,
        check_linking,
        check_live_session,
        check_pipewire_version,
        check_tools,
        check_virtmic,
        install_virtmic_config,
        render_report,
    )
```

and the check list it builds with the same five PipeWire checks plus `check_api_key()`, `check_activity()`, and — unless `--no-api-check` — `check_live_session(...)` built from `live.build_factory(genai.Client(api_key=os.environ["GEMINI_API_KEY"]))`.

Finally, change the parser's `prog` to `sidetap-live` and every `sidetap ` string in a help or error message to `sidetap-live `:

```bash
grep -rn 'sidetap doctor\|sidetap devices\|sidetap run' sidetap_live/*.py
```

Update each hit. These strings are what a user is told to type next, so a stale one sends them to the other program.

- [ ] **Step 5: Run to verify it passes**

```bash
uv run pytest tests/test_cli.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sidetap_live/cli.py sidetap_live/__main__.py tests/test_cli.py
git commit -m "Port the CLI, dropping nine flags with nothing behind them"
```

### Task 27: `run.py` — build the session

The ported parts stay as they are: the virtual-mic pre-check before `Router` is constructed, `Router.repair()`/`engage()`, bypass and its unjournalled real-mic link, the shutdown ordering, signal handling, `_run_headless`. What changes is the middle of `setup()` and what `start()` spawns.

**Files:**
- Create: `sidetap_live/run.py`
- Test: `tests/test_run.py`

- [ ] **Step 1: Copy**

```bash
cp "$SIDETAP/sidetap/run.py" sidetap_live/run.py
cp "$SIDETAP/tests/test_run.py" tests/test_run.py
sed -i 's/\bfrom sidetap\./from sidetap_live./g; s/\bimport sidetap\b/import sidetap_live/g; s/\bfrom sidetap import\b/from sidetap_live import/g' tests/test_run.py
```

- [ ] **Step 2: Replace the import block**

```python
from .activity import OverlapWatch, SpeechActivity, webrtc_detector
from .adapters import PwCatSink, PwLoopbackFactory, WpctlVolumeControl
from .capture import CaptureConfig, CaptureError, PipeWireCapture
from .cost import Rates
from .interpreter import DirectionInterpreter, InterpreterConfig
from .live import build_factory
from .metrics import Health, Metrics
from .playout import DuckControl, Playout, earcon
from .ports import LinkResult
from .preroll import PreRoll
from .routing import JOURNAL_PATH, VIRTMIC_SINK, Router
from .transcript import EventTranscript
from .types import LAG_CAP_S, NO_AUDIO_S, TTS_RATE, Direction, TranscriptEvent
```

Delete `default_voice`, `build_direction_configs` and `_rate`.

- [ ] **Step 3: Replace the constructor's injectable ports**

In `Session.__init__`, replace `recognizer_factory`, `translator` and `synthesizer` with one parameter:

```python
        sessions=None,
```

and, in the body:

```python
        # The one port that reaches the network. Injected like every other,
        # and built lazily in setup() rather than here so that constructing a
        # Session in a test never needs an API key.
        self._sessions = sessions
```

Rename `self.pipelines` to `self.interpreters` and retype it `dict[Direction, DirectionInterpreter]`.

- [ ] **Step 4: Replace the middle of `setup()`**

Replace everything from `self.transcript = BilingualTranscript(...)` down to the end of the per-direction loop with the code below. The ported code **above** that line — the virtual-mic pre-check that defines `snapshot` and `virtmic`, `Router.repair()` and `Router.engage()` — stays exactly as it is, and in that order: the check is a pure snapshot read and `engage()` is the first irreversible act, so a fatal check must never have anything to undo.

```python
        self.transcript = EventTranscript(args.out, session=self._session_name)
        self.rates = Rates()

        if self._sessions is None:
            import os

            from google import genai

            key = os.environ.get("GEMINI_API_KEY")
            if not key:
                # Fail here rather than on the first block of audio: setup()
                # has not yet engaged the router, so nothing needs undoing.
                raise CaptureError(
                    "GEMINI_API_KEY is not set. Run: sidetap-live doctor"
                )
            self._sessions = build_factory(genai.Client(api_key=key))

        # One detector per track. webrtcvad adapts to the noise floor across
        # calls, and the two tracks have very different ones - a raw room
        # microphone against audio already compressed and noise-suppressed by
        # the far end - so sharing one would let each spoil the other.
        self.activity = {
            d: SpeechActivity(webrtc_detector(), self._clock) for d in Direction
        }
        self.overlap = OverlapWatch(self.activity, self._clock)

        # Where each direction's audio is PLAYED. `snapshot` and `virtmic`
        # both come from the ported pre-check above, which is unchanged.
        # OUT must never fall back to None - pw-cat with no target
        # autoconnects to the default sink, so the outbound translation would
        # come out of your own speakers while the remote party heard silence,
        # with nothing saying so.
        default_sink = snapshot.node_by_name(snapshot.default_sink or "")
        sink_targets = {
            Direction.IN: default_sink.serial if default_sink else None,
            Direction.OUT: virtmic.serial,
        }
        # Which language each direction translates INTO. Crossed on purpose:
        # what reaches your ears is in your language, what reaches theirs is
        # in theirs.
        lang_targets = {
            Direction.IN: args.my_lang,
            Direction.OUT: args.their_lang,
        }
        # One origin for both directions. Taken inside the loop, the two
        # tracks would be stamped against origins milliseconds apart, and an
        # interleaved transcript exists so the two streams line up.
        session_t0 = self._clock.monotonic()

        for direction in Direction:
            sink = PwCatSink(
                self._launcher, target=sink_targets[direction], rate=TTS_RATE
            )
            self.sinks[direction] = sink
            # Only the IN direction ducks. OUT's audio goes to the virtual
            # mic, where there is no original to duck - the remote party
            # never hears your real voice at all.
            duck = (
                DuckControl(
                    self._volume,
                    object_id=lambda: self.router.duck_id,
                    level=args.duck_level,
                )
                if direction is Direction.IN
                else None
            )
            playout = Playout(direction, sink, duck=duck, lag_cap_s=args.lag_cap)
            self.playouts[direction] = playout

            self.interpreters[direction] = DirectionInterpreter(
                InterpreterConfig(
                    direction=direction,
                    target_lang=lang_targets[direction],
                    # False on IN so that a remote party already speaking your
                    # language produces no output, no audio flows, the duck
                    # opens and you hear them raw. True on OUT because there
                    # is no raw path there to fall through to.
                    echo=args.echo_out if direction is Direction.OUT else False,
                    idle_suspend=args.idle_suspend,
                ),
                sessions=self._sessions,
                playout=playout,
                activity=self.activity[direction],
                metrics=self.metrics,
                clock=self._clock,
                rates=self.rates,
                preroll=PreRoll(),
                on_event=self.transcript.write,
                session_t0=session_t0,
            )

        self.capture = PipeWireCapture(
            CaptureConfig(
                mic=args.mic, remote=args.remote, app=args.app,
            ),
            graph=self._graph,
            launcher=self._launcher,
            linker=self._linker,
            clock=self._clock,
        )
```

- [ ] **Step 5: Replace what `start()` spawns**

Per direction, two threads instead of four:

```python
        for direction, interpreter in self.interpreters.items():
            self._spawn(
                interpreter.pump,
                (self.capture.queues[direction.track], self.stop),
                f"pump-{direction.value}",
            )
            self._spawn(
                self.playouts[direction].run,
                (self.stop,),
                f"playout-{direction.value}",
            )
```

- [ ] **Step 6: Carry the rest of `Session` over unchanged, fixing only the rename**

`set_bypass`, `_set_bypass_locked`, `_link_real_mic`, `alarm_dead_air`, `_join_workers`, `shutdown`, `run_session` and `_run_headless` are ported **as they are**. Three of their properties are load-bearing and must survive intact:

- **Bypass has three effects and all three matter** — the duck opens and stays open, your real mic is linked straight into the virtual mic, and playout is suppressed on both directions. Describing or implementing only one is how a user ends up with translated speech talking over the unmediated conversation it was meant to replace.
- **The real-mic link is not journalled and is unlinked by replay, not by recomputation.** The default source can change mid-call (a headset gets plugged in) and recomputing from a fresh snapshot would unlink the wrong pair while leaving the real link live. `shutdown()` tears it down itself, first, because `doctor --repair` cannot find it.
- **`Router.restore()` always terminates the loopback, even with an empty journal.** The duck is created by `engage()`, not by routing a stream, so starting before the call and quitting before it begins leaves one to orphan.

The only edits are mechanical: every `self.pipelines` becomes `self.interpreters`, and `_on_direction_fatal` reads from the new dict. Run `grep -n 'pipelines\|recognizer\|translator\|synthesizer' sidetap_live/run.py` and confirm it comes back empty.

- [ ] **Step 7: Extend the health poller with the overlap sample**

In `_poll_capture_health`, after the existing NO_AUDIO check, add:

```python
            # Sampled here rather than per block because it is a property of
            # the two tracks together, and neither pump can see the other.
            self.metrics.set_overlap_pct(self.overlap.sample())
```

- [ ] **Step 8: Write the failing test**

Replace `tests/test_run.py`'s pipeline-specific tests with:

```python
import pytest

from sidetap_live.metrics import Metrics
from sidetap_live.types import Direction
from tests.conftest import FakeSessionFactory


def test_in_never_echoes_and_out_always_may(session_args, fake_ports):
    """The asymmetry is a consequence of the duck policy, not a preference."""
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    assert session.interpreters[Direction.IN]._config.echo is False
    assert session.interpreters[Direction.OUT]._config.echo is True


def test_only_the_in_direction_has_a_duck(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    assert session.playouts[Direction.IN].duck is not None
    assert session.playouts[Direction.OUT].duck is None


def test_targets_are_crossed(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    assert session.interpreters[Direction.IN]._config.target_lang == session_args.my_lang
    assert session.interpreters[Direction.OUT]._config.target_lang == session_args.their_lang


def test_setup_fails_before_engaging_when_the_virtual_mic_is_missing(
    session_args, fake_ports_without_virtmic
):
    """Nothing may be rewired before a fatal check, or there is nobody left
    to restore it."""
    session = build_session(session_args, fake_ports_without_virtmic,
                            sessions=FakeSessionFactory())
    with pytest.raises(Exception, match="doctor"):
        session.setup()
    assert session.router is None
```

Reuse whatever `session_args` / `fake_ports` / `build_session` helpers the ported `tests/test_run.py` already defines, adding `duck_level=0.0`, `echo_out=True`, `idle_suspend=True` to the args fixture and removing the cascade flags from it.

- [ ] **Step 9: Run**

```bash
uv run pytest tests/test_run.py -q
```

Expected: PASS.

- [ ] **Step 10: Run the whole suite**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add sidetap_live/run.py tests/test_run.py
git commit -m "Wire the session: two interpreters, two playouts, one overlap watch"
```

---

## Phase 5 — End to end, cross-repo, and docs

### Task 28: One headless end-to-end test

Every previous test exercises one module. This one drives capture through the interpreter into playout with nothing real behind it, which is what catches a wiring mistake that unit tests each think is somebody else's problem.

**Files:**
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_smoke.py
"""End to end through the fakes. No audio hardware, no network, no API key."""

import threading

import pytest

from sidetap_live.activity import SpeechActivity
from sidetap_live.capture import DroppingQueue
from sidetap_live.cost import Rates
from sidetap_live.interpreter import DirectionInterpreter, InterpreterConfig
from sidetap_live.metrics import Metrics
from sidetap_live.playout import CHUNK_BYTES, DuckControl, Playout
from sidetap_live.preroll import PreRoll
from sidetap_live.transcript import EventTranscript
from sidetap_live.types import (
    BLOCK_BYTES,
    TTS_BYTES_PER_S,
    AudioChunk,
    AudioOut,
    Direction,
    SourceText,
    TargetText,
)
from tests.conftest import (
    FakeAudioSink,
    FakeClock,
    FakeSessionFactory,
    FakeVolumeControl,
)

SPEECH = b"\x00\x40" * (BLOCK_BYTES // 2)


def test_speech_in_becomes_ducked_audio_out_and_a_transcript(tmp_path):
    clock = FakeClock()
    metrics = Metrics()
    sessions = FakeSessionFactory()
    volume = FakeVolumeControl()
    sink = FakeAudioSink()
    transcript = EventTranscript(tmp_path, session="smoke")

    playout = Playout(
        Direction.IN, sink, duck=DuckControl(volume, object_id=42)
    )
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.IN, target_lang="en", echo=False),
        sessions=sessions,
        playout=playout,
        activity=SpeechActivity(lambda pcm: True, clock),
        metrics=metrics,
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(),
        on_event=transcript.write,
    )

    # 1. Speech arrives, a session opens, audio is sent.
    interpreter.feed(AudioChunk(track="remote", pcm=SPEECH, t_start=0.0))
    assert len(sessions.sessions) == 1
    assert sessions.sessions[0].sent

    # 2. The model answers with audio and both transcription streams.
    interpreter.note_event(SourceText(text="privet"))
    interpreter.note_event(AudioOut(pcm=b"\x00\x40" * (TTS_BYTES_PER_S // 2)))
    interpreter.note_event(TargetText(text="hello"))

    # 3. Playout speaks it and the duck closes while it does.
    assert playout.tick() is True
    assert volume.calls == [(42, 0.0)]
    assert len(sink.chunks[0]) == CHUNK_BYTES

    # 4. The duck reopens once the audio runs out.
    while playout.backlog_s() > 0:
        playout.tick()
    for _ in range(50):
        playout.tick()
    assert volume.calls[-1] == (42, 1.0)

    # 5. Both streams reached the transcript, unpaired and in order.
    path = transcript.close()
    text = path.read_text()
    assert text.index("privet") < text.index("hello")


def test_the_pump_thread_drains_a_capture_queue_and_stops(tmp_path):
    """The threaded path, not just feed()."""
    clock = FakeClock()
    sessions = FakeSessionFactory()
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.OUT, target_lang="ru", echo=True),
        sessions=sessions,
        playout=Playout(Direction.OUT, FakeAudioSink()),
        activity=SpeechActivity(lambda pcm: True, clock),
        metrics=Metrics(),
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(),
    )
    queue = DroppingQueue()
    for _ in range(3):
        queue.put(AudioChunk(track="mic", pcm=SPEECH, t_start=0.0))

    stop = threading.Event()
    thread = threading.Thread(target=interpreter.pump, args=(queue, stop))
    thread.start()
    try:
        for _ in range(100):
            if sessions.sessions and len(sessions.sessions[0].sent) >= 3 * BLOCK_BYTES:
                break
            threading.Event().wait(0.01)
        else:
            pytest.fail("pump never drained the queue")
    finally:
        stop.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert sessions.sessions[0].closed is True
```

- [ ] **Step 2: Run to verify it fails, then passes**

```bash
uv run pytest tests/test_smoke.py -q
```

If it fails on anything other than an import, **the failure is real wiring** — fix the module, not the test.

- [ ] **Step 3: Run the whole suite**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 4: Confirm the suite needs nothing from the environment**

```bash
env -u GEMINI_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS uv run pytest -q
```

Expected: PASS. A failure here means something imports the SDK at module level or reads a credential at collection time — find it and make the import lazy.

- [ ] **Step 5: Commit**

```bash
git add tests/test_smoke.py
git commit -m "Add an end-to-end test through the fakes"
```

### Task 29: Make sidetap's transcripts comparable

**This task modifies `$SIDETAP`, the other repository.** It is the only one that does. Without it the two systems produce transcripts in different schemas and the head-to-head has no textual evidence.

The schemas must share four required keys — `t`, `direction`, `kind`, `text`. `latency_ms` is optional and only sidetap emits it; an optional field one side omits does not harm comparability, and discarding sidetap's stage timings to force a match would destroy data for no gain.

**Files:**
- Modify: `$SIDETAP/sidetap/transcript.py`
- Test: `$SIDETAP/tests/test_transcript.py`

- [ ] **Step 1: Write the failing test in the other repository**

```bash
cd "$SIDETAP"
```

Append to `tests/test_transcript.py`:

```python
import json

from sidetap.transcript import ENGINE, events_of
from sidetap.types import Direction, Latency, Record, Unit


def a_record():
    return Record(
        unit=Unit(direction=Direction.IN, text="privet", t_start=1.0, t_end=1.0),
        target_text="hello",
        latency=Latency(asr_ms=100.0, mt_ms=50.0, tts_ms=200.0),
    )


def test_a_paired_record_becomes_two_events():
    """sidetap_live cannot pair its two transcription streams, so the shared
    schema is events. A pair is expressible as events; events are not
    expressible as a pair."""
    assert events_of(a_record()) == [
        {"t": 1.0, "direction": "in", "kind": "source", "text": "privet"},
        {"t": 1.0, "direction": "in", "kind": "target", "text": "hello",
         "latency_ms": 350.0},
    ]


def test_the_header_names_the_engine(tmp_path):
    transcript = BilingualTranscript(tmp_path, session="s1")
    transcript.close()
    first = json.loads(transcript.jsonl_path.read_text().splitlines()[0])
    assert first["meta"]["engine"] == ENGINE == "cascade"


def test_a_dropped_utterance_still_emits_both_events(tmp_path):
    """Dropped audio was still recognised and translated; leaving it out
    would make the two transcripts disagree about what was said."""
    record = a_record()
    record = type(record)(unit=record.unit, target_text=record.target_text,
                          latency=record.latency, dropped=True)
    assert len(events_of(record)) == 2
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_transcript.py -q
```

Expected: FAIL with `ImportError: cannot import name 'ENGINE'`.

- [ ] **Step 3: Edit `$SIDETAP/sidetap/transcript.py`**

Add near the top:

```python
ENGINE = "cascade"
```

Add beside `record_to_dict`:

```python
def events_of(record: Record) -> list[dict]:
    """One paired record as two transcript events.

    sidetap_live writes this same schema (see that repository's
    transcript.py) because its two transcription streams drift independently
    and cannot honestly be paired. Events are the common denominator: a pair
    is expressible as two events, but two drifting streams are not
    expressible as a pair. `latency_ms` is sidetap-only and optional - the
    single-box engine has no stage breakdown to report, and discarding these
    timings to force a match would destroy data for nothing.
    """
    return [
        {
            "t": record.unit.t_start,
            "direction": record.direction.value,
            "kind": "source",
            "text": record.unit.text,
        },
        {
            "t": record.unit.t_end,
            "direction": record.direction.value,
            "kind": "target",
            "text": record.target_text,
            "latency_ms": record.latency.total_ms,
        },
    ]
```

In `BilingualTranscript.__init__`, after opening the handle, write a meta line:

```python
        self._write_line(
            {
                "meta": {
                    "engine": ENGINE,
                    "session": self.session,
                    "started": datetime.now(timezone.utc).isoformat(),
                }
            }
        )
```

Add the `_write_line` helper used by both, and change `write()` to emit `events_of(record)` instead of `record_to_dict(record)`:

```python
    def _write_line(self, payload: dict) -> None:
        self._jsonl.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._jsonl.flush()
```

```python
            self._records.append(record)
            for event in events_of(record):
                self._write_line(event)
```

Keep `record_to_dict` if other tests use it; otherwise delete it. The Markdown rendering is unchanged.

- [ ] **Step 4: Run the other repository's whole suite**

```bash
uv run pytest -q
```

Expected: PASS, all tests. Fix any test that asserted the old JSONL shape.

- [ ] **Step 5: Commit in the other repository**

```bash
git add sidetap/transcript.py tests/test_transcript.py
git commit -m "Write the transcript as events, so sidetap_live's is comparable

sidetap_live runs a single speech-to-speech model whose two transcription
streams drift independently and cannot honestly be paired into one record.
Events are the common denominator: a pair is expressible as two events, but
two drifting streams are not expressible as a pair.

latency_ms stays as an optional field on the target event. The single-box
engine has no stage breakdown to report, and discarding these timings to
force an identical schema would destroy data for no gain."
cd /home/vvsosed/Documents/repo2/sidetap_live
```

### Task 30: `README.md`, `CLAUDE.md` and the manual smoke checklist

**Files:**
- Create: `README.md`, `CLAUDE.md`, `docs/manual-smoke.md`

- [ ] **Step 1: Write `docs/manual-smoke.md`**

Start from sidetap's and change what differs:

```bash
cp "$SIDETAP/docs/manual-smoke.md" docs/manual-smoke.md
```

Then replace its contents with checks for what this suite structurally cannot verify. Each item is a thing to do and an observation to record:

1. **The virtual mic is enumerated and still selected.** Start a Zoom call, open its audio settings, confirm `sidetap_virtmic` appears and is still the saved selection after running both programs. *This is the shared-device decision from Task 10 being exercised — if the selection is lost, that decision is wrong.*
2. **Ducking actually silences the original.** With `--duck-level 0.0`, confirm you hear no trace of the remote party's own voice while the translation plays, and that their voice returns within half a second of it stopping.
3. **The duck does not flap.** Listen for the original being chopped into fragments between output chunks. If it happens, `DUCK_HOLD_S` is too short.
4. **A rotation is inaudible.** Run a call past ten minutes while talking normally. Note whether you can hear the seam, and whether the voice changes at it. *If the voice changes, experiment 2's finding is being confirmed in the field and the spec's Session continuity decision must be reopened.*
5. **Idle-suspend wakes fast enough.** Stay silent for a minute, then speak. Record how much of your first word was lost.
6. **A remote party speaking your language falls through.** Have them say a sentence in your own language. You should hear their real voice, not silence and not a synthetic echo.
7. **Dead air on OUT is noticed.** Kill the network mid-call while speaking. Confirm the earcon sounds and the pane shows DEAD AIR.
8. **The remote party hears something intelligible.** The only check that needs a second human. Record their unprompted description of the quality.
9. **Overlap actually rises.** Deliberately talk over each other for a minute. Record what `overlap` reads afterwards and whether the conversation remained followable. *This is the experiment.*

- [ ] **Step 2: Write `README.md`**

Cover, in this order: what it is and how it differs from sidetap; requirements (`GEMINI_API_KEY`, PipeWire ≥ 0.3.60, and that the virtual mic is shared with sidetap so `doctor --install` is a no-op if sidetap already ran it); `uv sync`; the one-time setup; **start your call first, then run `devices`** — the same first-run confusion sidetap documents, since it is a PipeWire property and has not changed; running a call; what you hear and what they hear under duck policy C; hotkeys; the cost table from the spec; and a **Known limitations** section listing only measured things, never aspirations.

Known limitations must include, at minimum: no region pinning, because the model is Developer API only; voice replication is inconsistent by the model's own card and this program does not control it; `--phrase` has no equivalent, so names are at the model's mercy; session rotation every ~10 minutes with whatever experiment 2 measured about its audibility; and that this has been run against one setup by one person.

- [ ] **Step 3: Write `CLAUDE.md`**

Start from sidetap's and rewrite the Architecture and Invariants sections:

```bash
cp "$SIDETAP/CLAUDE.md" CLAUDE.md
```

The invariants that carry over verbatim: the audio contract; `object.serial` except `wpctl`; lazy SDK imports; the duck defaults open and fails open; journal before you touch the graph. **Delete** the keepalive invariant and "audio time is not elapsed time" — neither has a successor here, and leaving them would send a reader looking for code that does not exist.

Add three that are new and load-bearing:

- **Every state transition happens on the pump thread; the receive thread only records.** Transitioning from the receive thread closes a session out from under the loop iterating it.
- **`activity.py` observes, it never gates.** Handing a model that reasons over continuous audio a stream with the silence cut out changes what it hears. This is why the module is not called `vad.py`.
- **The duck is driven by output, not input.** No audio out means the duck opens, by construction rather than by a `finally` clause.

Also record the cross-repo relationship: the PipeWire layer is copied from sidetap, not shared, and a fix in one is not a fix in the other.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md docs/manual-smoke.md
git commit -m "Document what this is, what it does not do, and what only a human can check"
```

---

## Done looks like

- [ ] `uv run pytest -q` passes with no audio hardware, no network and no API key.
- [ ] `env -u GEMINI_API_KEY uv run pytest -q` passes.
- [ ] `uv run sidetap-live doctor` reports every check passing, or warning only on `speech activity`.
- [ ] `uv run sidetap-live run --app zoom --their-lang ru-RU --my-lang en-US` interprets a real two-way call in both directions.
- [ ] A session survives an hour, crossing five or six connection boundaries, with the clean/forced split recorded.
- [ ] `backlog_s`, `offset_s` and `overlap_pct` are recorded for at least one real call under each system, against a second human.
- [ ] Both systems write a transcript whose JSONL shares the four required keys.
- [ ] `docs/experiments/` holds all six measurements, and any decision they contradicted has been revisited in the spec rather than silently left standing.
