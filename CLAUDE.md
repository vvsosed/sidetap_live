# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sidetap_live` is a real-time two-way voice interpreter for any call on Linux —
Zoom, Viber, Telegram, Discord, Slack huddles — built by tapping **PipeWire**
instead of integrating with each platform's API. It runs **one WebSocket per
direction** against `gemini-3.5-live-translate-preview`, a model purpose-built
for continuous speech translation: audio in, translated audio out, no
turn-taking. Routing is **full replacement** — each party hears only the
translation — so pipeline health is not a diagnostic detail, it is the only
thing standing between the conversation and silence.

The PipeWire layer was **ported** from `sidetap` (the same author's cascaded
ASR → MT → TTS interpreter): copied and adapted with its tests, not imported.
The two repositories share no runtime dependency, and **nothing here writes to
sidetap.** The port is provenance, not coupling.

The two programs are **not compared.** A head-to-head was the original goal and
was dropped on 2026-09-23; this program is evaluated on whether it is a usable
interpreter, which is answerable without a baseline.

## Repository layout — read this first

`sidetap_live/` is the application; see **Architecture** below.

`tests/` holds **351 tests that run with no audio hardware, no network and no
credentials** — every subprocess, socket and clock sits behind a `Protocol` in
`ports.py`, with a real implementation in `adapters.py` and a fake in
`tests/conftest.py`. Verify that property still holds with:

```bash
env -u GOOGLE_APPLICATION_CREDENTIALS -u GEMINI_API_KEY uv run pytest -q
```

`docs/experiments/` records the six measurements the design rests on, in five
files: the sixth, the `1007` region-subtag post-mortem, is an addendum at the
end of `01-connect.md` rather than a file of its own, because it was found by
a real call failing rather than by an experiment. **Prefer a number from there
over a claim from memory.** Four of the six contradicted
either Google's documentation or the original design, and two of those would
have produced code that passed every offline test and failed only on a live
call.

`docs/manual-smoke.md` is the checklist for what the automated suite
structurally cannot verify: that ducking actually silences the original, that a
rotation is inaudible, that the remote party hears anything intelligible. Run
it by hand before trusting a change to `capture`, `routing`, `playout`,
`interpreter` or the duck.

`docs/superpowers/specs/` holds the design, revised several times as the
experiments landed. `docs/superpowers/plans/` holds the implementation plan and
its own execution record.

`tests/fixtures/pw_dump_real.json` is a **real `pw-dump`, scrubbed** — every
other graph fixture was hand-written from the same assumptions `graph.py` was
written from, so they cannot catch a wrong assumption; they share it. That one
can.

## Commands

**This project uses uv, not pip.** Never call `pip install` or activate `.venv`
by hand; `uv run` does both.

```bash
uv sync                          # install from uv.lock

# verify the PipeWire toolchain BEFORE debugging anything in Python
pw-cli --version                   # needs >= 0.3.60
pw-dump | head                     # graph as JSON
wpctl status                       # sinks/sources, incl. this program's nodes

uv run pytest -q                                    # 351 tests, no audio/network/creds
uv run sidetap-live devices                         # run this MID-CALL, not before
uv run sidetap-live doctor                          # environment checks
uv run sidetap-live doctor --install                # write the virtual-mic config (once)
uv run sidetap-live doctor --repair                 # replay the journal after a crash
uv run sidetap-live run --app zoom --their-lang ru-RU --my-lang en-US
```

**Credentials: `GEMINI_API_KEY` and nothing else.** This project uses no GCP
credentials — `gemini-3.5-live-translate-preview` is Gemini Developer API only,
not on Vertex. Consequences: no `--project`, no ADC, and **no region pinning**,
so the network term in the latency budget is whatever Google's edge gives us.

**Do not run `gcloud auth ...`** against this checkout. It is unnecessary here
and the machine is attached to a live GCP project that re-authenticating has
broken before.

## Architecture

### Signal flow

Both directions are the same `DirectionInterpreter`, instantiated twice.

```
=== DIRECTION "in"  (them -> you) ==============================================

 messenger  --additive pw-link-->  recorder --> DirectionInterpreter
 Stream/Output      (tap.py)      16k s16 100ms         |
      |                                      target = --my-lang
      |                                      echo    = false
      |                                                |
      |                                          24k s16 chunks
      v                                                v
 sidetap_live_duck <--[closed while SPEECH flows]-- playout --> headphones

=== DIRECTION "out" (you -> them) ==============================================

 USB mic --> recorder --> DirectionInterpreter --> playout
                         target = --their-lang        |
                         echo   = true                v
      messenger input <-- sidetap_virtmic <-- loopback <-- sidetap_tts_sink
       (your real mic is never linked here, except while bypassed)
```

**The echo asymmetry is a consequence of the duck policy, not a preference.**
On IN, `echo=False` means a remote party already speaking your language
produces no output, so the duck opens and you hear them raw — the degenerate
case handles itself. OUT has no raw path to fall through to: your real
microphone is never linked to the messenger, so silence there means the remote
party hears *nothing at all*.

### Concurrency: threads, with one asyncio island per session

`google-genai`'s Live API is asyncio-native; the ported capture and playout
code is subprocess-and-thread shaped. Rather than convert the tested
foundation, the asyncio island is confined inside `live.py`: each
`GeminiLiveSession` owns one thread running one event loop and presents the
**synchronous** `InterpreterSession` Protocol outward.

Per direction: a capture thread (`capture.py`), an interpreter pump thread, a
receive thread per live session, and a playout thread. Two more run
session-wide: the routing watcher and the capture health poller — the latter
also samples `overlap_pct`, because that is a property of the two tracks
together and neither pump can see the other.

Textual owns the main thread and **the pipeline never calls into it** — workers
write to a lock-guarded `Metrics` snapshot that `tui.py` polls at 10 Hz. That
one-way dependency is what makes `--no-tui` and the headless suite the same
code path.

### Modules

| Module | Role | Origin |
|---|---|---|
| `types.py` | value types, constants, the session event alphabet; stdlib only | ported, trimmed |
| `ports.py` | every `Protocol` — one real impl, one fake each | ported + `InterpreterSession` |
| `graph.py` | parses `pw-dump` into a `PwGraph`; pure, never spawns | ported |
| `recorder.py` | builds `pw-record` argv, frames stdout into blocks | ported |
| `tap.py` | `AppTap` — links a matching application's ports | ported |
| `capture.py` | owns recorders, queues and capture threads | ported |
| `routing.py` | duck loopback lifecycle, re-route, journal, restore | ported |
| `adapters.py` | the real ports — every subprocess on the audio path | ported |
| `activity.py` | `SpeechActivity` observes speech; `OverlapWatch` | new |
| `live.py` | `GeminiLiveSession` — the only module that talks to Gemini | new |
| `interpreter.py` | `DirectionInterpreter` — lifecycle, rotation, send/receive | new |
| `preroll.py` | `PreRoll` — bounded ring of recent capture | new |
| `cost.py` | audio-token rates as configuration | new |
| `playout.py` | chunk queue, `DuckControl`, `has_speech` | rewritten |
| `metrics.py` | backlog, offset, overlap, rotations, cost | ported, reshaped |
| `transcript.py` | event-stream `.jsonl` + interleaved `.md` | rewritten |
| `tui.py` | Textual dashboard, hotkeys | ported, reshaped |
| `doctor.py` | environment checks, virtual-mic config | ported, API checks replaced |
| `run.py` | `Session` — builds every stage, startup/shutdown | ported, rewired |
| `cli.py` / `__main__.py` | argparse, `doctor`/`devices`/`run` | ported, flags changed |

Deleted with no successor: `asr.py`, `segment.py`, `translate.py`, `tts.py`,
`rotation.py`, `vad.py`.

`rotation.py` went because its whole job was mapping a recognition offset back
onto the session timeline *through gated silence*. With no gate there are no
offsets to correct, so sidetap's "audio time is not elapsed time" invariant has
no successor here — its absence is deliberate, not an oversight.

## Session lifecycle

```
  SUSPENDED ──speech onset──> OPENING ──ready──> RUNNING
      ^                          ^                 │
      │                          │            GoAway(time_left)
      └──idle > IDLE_SUSPEND_S───┼─────────────────┤
                                 │                 v
                                 └────────── OVERLAPPING
```

`OVERLAPPING` means **two sessions are live and being fed the same audio**,
with the replacement's output discarded until it takes over. The switch happens
when the replacement is warm *and* the outgoing output has fallen silent.

A failed open falls back to `SUSPENDED`, not `OPENING` — see the invariants.

## Invariants

Each says what breaks if violated, because "don't do X" reads as a style
preference and these are not.

- **Audio contract.** Capture: s16 / 16 kHz / mono, 100 ms blocks
  (`BLOCK_BYTES` = 3200). Playout: s16 / 24 kHz / mono. Both are the model's
  own contract. **No sample-rate conversion in Python, in either direction** —
  `pw-record` and `pw-cat` already do it, and adding it here gets it wrong at
  the boundary where the two rates meet. This is why the audio path carries no
  numpy.
- **Identify PipeWire nodes by `object.serial` — except `wpctl`, which resolves
  against `object.id`.** `Router.duck_id` and `Router.duck_serial` are two
  separate fields for exactly this reason. Conflate them and the duck silently
  never closes: the original plays under every translation for the whole call,
  with nothing on screen explaining why.
- **Third-party imports are lazy**, inside the function bodies that need them —
  `google.genai` in `live.py`, `cli.py` and `run.py`; `webrtcvad` in
  `activity.py`. This is what lets 351 tests import the package with no
  credentials configured at all.
- **Every state transition happens on the pump thread; the receive thread only
  records.** `GoAway` opens a replacement, `Closed` sets a flag, a handle is
  stored — and the pump acts on them when the next block arrives. Transitioning
  from the receive thread closes a session out from under the loop iterating
  it, and would need a second lock around the whole machine.
- **`activity.py` observes, it never gates.** Nothing may remove a byte from
  the audio stream. Handing a model that reasons over continuous audio a stream
  with the silence cut out changes what it hears. This is why the module is not
  called `vad.py` — the old name invites someone to reinstate gating, and
  `test_it_is_not_a_gate` guards it.
- **The duck triggers on audio ENERGY, not byte presence.** The model emits a
  continuous 24 kHz stream whether or not it is translating — measured at
  ~151 s of audio returned for 154 s of pure digital silence in. Keyed on bytes
  arriving, the duck closes on the first chunk and **never reopens**, muting
  the remote party for the entire call. Measured distributions: 0.04% of 20 ms
  frames above threshold when idle, 75.5% when translating, hence
  `SPEECH_PEAK = 2000`.
- **Use `has_speech()`, never `find_silence_boundary(...) is None`.** The
  latter reports where the *first* quiet frame is, which is right for the lag
  cap and wrong for "is this speech": a 250 ms chunk of clear speech routinely
  contains a quiet 20 ms frame inside a word. Inverting it makes the outgoing
  session read as silent during continuous speech and switches sessions
  mid-word on every rotation.
- **`translation_config` is a TOP-LEVEL field of `LiveConnectConfig`**, never
  nested under `generation_config`. `GenerationConfig` also exposes a
  `translation_config` field, so the nested form type-checks, connects, and
  emits only a `DeprecationWarning` — while producing a conversational agent
  with its own turn-taking instead of an interpreter, with nothing in the logs
  to say so. `test_translation_config_is_top_level_not_under_generation_config`
  guards it.
- **Rotation is make-before-break.** Open the replacement on `GoAway`, feed it
  the same audio with its output discarded, switch when it is warm and the
  outgoing output is silent. Serial rotation was measured to produce a **3.12 s
  hole** against the 0.80 s pause it was placed in, because a fresh session
  emits nothing for ~3 s. Closing inside `time_left` is **mandatory** — the
  server aborts with code 1008 otherwise.
- **A failed open falls back to SUSPENDED, not OPENING.** Nothing re-wakes an
  `OPENING` direction and nothing sends from one, so leaving it there makes the
  direction silently dead for the rest of the call, noticed only by the
  dead-air alarm. `SUSPENDED` is both true and recoverable, and
  `REOPEN_BACKOFF_S` paces the retry so a revoked key does not become a
  rate-limit ban.
- **The duck defaults open and fails open.** `DuckControl.close()` flips its
  flag only on a *successful* `wpctl` call, and `Playout.run()`'s `finally`
  opens it unconditionally. A duck stuck **closed** silences the person you are
  talking to and leaves them speaking to nobody, which is worse than this
  program not working at all.
- **Journal before you touch the graph.** `Router` writes every planned
  link/unlink to `~/.local/state/sidetap_live/routing-journal.json` atomically
  *before* calling the linker. A `kill -9` in that window is recoverable with
  `doctor --repair`; the other order leaves the graph rewired with nothing on
  disk to repair from.

## Graph identity: what is shared with sidetap and what is not

**Anything this process creates gets this program's name. The one permanent
shared device keeps sidetap's.**

| | |
|---|---|
| `sidetap_virtmic`, `sidetap_tts_sink`, `90-sidetap-mic.conf` | **shared, byte-identical** |
| `sidetap_live_duck`, `sidetap_live.<track>.<uuid>` capture nodes | ours |
| `~/.local/state/sidetap_live/` journal | ours |

The virtual mic is shared **deliberately**: a messenger's saved input-device
selection is tied to the node name, so two names would mean re-selecting your
microphone in Zoom every time you switched programs. `doctor --install` never
overwrites an existing config, so the two installers are compatible.

The duck is *not* shared for the opposite reason: after a crash left an
orphaned duck with a call routed through it at volume zero, a shared name would
let either program's `doctor --repair` tear down the other's live duck.

## Things that bite at runtime

- **`GoAway` is an instruction, not a warning.** It arrives ~540 s into a
  connection with `time_left` as the **string** `'50s'`, and overrunning that
  window is a 1008 abort.
- **Headphones matter**, or your own speakers re-enter your microphone and get
  translated back at the other party.
- **Wayland is irrelevant here** — audio capture needs no portal permission;
  that is a video-capture concern.
- **Output billing does not stop during pauses.** The model streams output
  continuously while a session is open, so a call with ordinary conversational
  gaps bills output the whole time. `IDLE_SUSPEND_S` only catches gaps past
  45 seconds.
