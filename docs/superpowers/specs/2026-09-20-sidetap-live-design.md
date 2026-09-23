# sidetap_live — single-box voice interpretation on PipeWire

Status: design, approved 2026-09-20.

## What this is

A second implementation of sidetap's interpreter, built on a single
speech-to-speech model instead of a cascade, so the two can be run against real
calls and compared.

The audio surface is identical to sidetap's: the remote party's voice is tapped
from the messenger's own playback stream, and translated speech is injected
through a permanent virtual microphone the messenger selects as an ordinary
input device. Any messenger PipeWire can see, no platform API, no cooperation
from the other side.

What changes is everything between capture and playout. sidetap runs streaming
ASR → machine translation → streaming TTS, three vendors and three failure
modes per direction. This runs one WebSocket per direction against
`gemini-3.5-live-translate-preview`, a model purpose-built for continuous
speech translation: audio in, translated audio out, no turn-taking.

Python, managed with `uv`. Linux and PipeWire only.

## The question this exists to answer

**REVISED 2026-09-23: this is no longer a head-to-head against sidetap. It is
an evaluation of sidetap_live on its own terms.**

The question is whether a single speech-to-speech model makes a genuinely
usable interpreter for a real call — not whether it beats a cascade.

Concretely, three things have to hold at once:

1. **You can talk without pausing for the machine.** The model claims to
   translate "as the speaker talks without waiting for turns." If that holds,
   the conversation has a normal cadence rather than a walkie-talkie one.
2. **Interruption still works.** Two people talking over each other is normal
   conversation, not an error case. If overlap makes the interpreter fall
   behind or garble, the cadence is still being dictated by the machine.
3. **It survives a real call.** An hour, crossing the connection cap five or
   six times, without a dropout anyone has to work around.

These are absolute statements, answerable without a baseline: `backlog_s` near
zero, `offset_s` near a quarter-second, `overlap_pct` rising when people
actually interrupt each other, and no forced rotations. *Measuring the result*
defines them precisely.

Latency, cost and robustness are instrumented because they explain whether
those three hold, not because they decide a contest.

## Relationship to sidetap

`sidetap_live` is a standalone repository with its own `pyproject.toml` and
`uv.lock`. It is not a fork of, and does not depend on, `sidetap`.

sidetap's PipeWire layer — graph parsing, the additive per-application tap,
`pw-record` framing, routing and the duck, the virtual-mic installer — is
**ported**: copied with its tests, the same way sidetap itself was ported from
meetscribe. The two repositories share no runtime dependency.

A shared package was considered and rejected. A common dependency would make
every change a negotiation between two consumers, and sidetap is an actively
developed project with its own direction. The cost is real and accepted
knowingly: **a bug fixed in one repository's `routing.py` is not fixed in the
other's.** That is tolerable because the audio layer is the mature, stable part
and all the uncertain work sits above it.

**Nothing in this project writes to sidetap, and nothing depends on it at
runtime.** The port is provenance, not coupling.

The structural conventions are carried over for the same reason sidetap carried
them from meetscribe: every subprocess, socket and clock sits behind a
`Protocol` in `ports.py`, with one real implementation and one fake, and the
suite runs with no audio hardware, no network and no credentials.

## Decisions

Each records the alternative rejected, because the rejections carry information
the choice alone does not.

### Platform: Gemini Developer API, not Vertex

`gemini-3.5-live-translate-preview` is available on the Gemini Developer API
with API-key authentication only. It is not on Vertex AI; a request for it on
the developer forum was redirected to sales with no timeline.

Consequences, all accepted:

- Auth is `GEMINI_API_KEY`, not ADC. sidetap's `--project` and its careful
  separation of ADC's default project from the one with APIs enabled have no
  successor here.
- **No region pinning.** sidetap pins Speech-to-Text to `europe-west3` for a
  ~30 ms RTT from central Europe. That control does not exist for this model,
  so the network term in the latency budget is whatever Google's edge gives us.
  This is a measured finding of the comparison, not a blocker for it.
- Billing runs outside the GCP project sidetap uses.
- It is a preview model and may change or be withdrawn.

The alternative was `gemini-3.8-live` on Vertex — native audio, ADC,
region-pinnable — *prompted* to act as an interpreter. Rejected because it has
no `translationConfig` and its agentic turn-taking, barge-in and
answer-the-question instincts are precisely what the initial research spike
identified as disqualifying. Building the comparison against a model fighting
its own design would measure the prompt, not the approach.

### What you hear: the duck follows the output, not the input

sidetap closes the duck because the remote party is speaking, and opens it in
the gaps between their utterances. A turn-free model has no gaps to open in —
the translation arrives while they are still talking — so that policy would
silence the original for the entire call.

Three policies were weighed:

- **Hard replacement.** Duck to zero whenever the session is active. Truthful
  to "never two voices at once", but the first seconds of every utterance are
  total silence: no cue that they started talking, no tone, no interruption
  signal.
- **Interpreter booth.** Hold the original under at ~20%, translation on top.
  How a real interpreting booth sounds, and what the research spike suggested.
  Rejected as the default because 20% of a voice speaking *your own* language
  competes for attention in a way a foreign one does not.
- **Lead-in only — chosen.** The duck closes when translated audio starts
  flowing and opens when it stops.

Lead-in wins on a structural argument rather than a matter of taste: it inverts
the duck's trigger from input to output. sidetap's duck closes on the remote
speaking, so a dead pipeline leaves it closed over a live call — the exact
failure its "fails open" invariant exists to prevent, patched with a `finally`
clause. Driving it from "translated audio is flowing" makes that self-healing
by construction: no audio out, no duck. It also buys back the "they started
talking" cue that hard replacement destroys.

Booth mode stays one flag away. `--duck-level` defaults to `0.0`; `0.2` is the
booth.

### Session continuity: make before break

**REVISED 2026-09-20 after experiment 2. The original decision was to rotate
into a conversational pause; it was wrong, and the measurement is unambiguous.**

A Live API WebSocket lives about ten minutes. Experiment 3 measured the exact
terms: `go_away` arrives at t=540 s carrying `time_left='50s'`, and the server
aborts with code 1008 if the client has not closed by then. An hour-long call
crosses that boundary five or six times.

**Why rotate-at-a-pause failed.** It rested on the claim that a seam placed in
a silence is inaudible. Experiment 2 measured the seam it actually produces:

| | |
|---|---|
| silence the rotation produced | **3.12 s** |
| pause it was deliberately placed in | 0.80 s |
| longest silence anywhere in the unrotated run | 0.60 s |

The hole is nearly four times larger than the pause meant to hide it, because
**a freshly opened session needs ~3 s before it emits any audio at all** —
3.06 s measured in `live.py`'s smoke check, 3.54 s in a raw SDK probe. That is
not connection latency (~500 ms, experiment 1); it is the model's own lead-in.
No pause in ordinary speech is long enough to conceal it. A listener confirmed
the seam is audible in the rotated run and not in the continuous one.

**The chosen strategy is make-before-break.** On `GoAway`:

1. Open the replacement session immediately and feed it the **same** audio as
   the live one. Both are now hearing the conversation.
2. Discard the replacement's output while it warms up. Its first ~3 s of
   output covers audio the live session has already translated and spoken.
3. Switch playout to the replacement once **both** conditions hold: it has
   produced energy-bearing output (so it is warm), and the outgoing session's
   output is currently silent (so the join lands in a gap).
4. Close the outgoing session. Closing is mandatory, not tidy-up — an
   unclosed connection takes a 1008 abort at the deadline.

There is no hole, because the replacement was already producing before the
switch. The overlap costs ~5 s of doubled input audio per rotation, about half
a cent an hour.

**Finding the join point is easy in a way finding a source pause was not.**
The original design had to wait for the speaker to stop; experiment 2 counted
only 11 pauses of ≥0.4 s in 96 s of dense speech, and just 2 reaching the 0.7 s
the design wanted. But this design waits for a gap in the *output* stream, and
the same run contained **154 internal output silences**. A join point is
always a second or two away.

**What this removes.** `ROTATE_PAUSE_S` and the clean/forced rotation
distinction both go: every rotation is now overlapped, so there is no degraded
path to fall back to and nothing to count separately. The pre-roll ring
survives, but for one consumer rather than two — waking a suspended session.
Crossing a seam no longer replays anything, because nothing was missed.

**What was rejected, and why the rejection reversed.** This document originally
argued against make-before-break on the grounds that hearing a sentence twice
is more disorienting than a gap. That reasoning assumed a sub-second gap. At
3.12 s it does not hold, and the double-speak risk is contained by discarding
the replacement's output until the switch — by which point it is producing in
real time, not replaying.

`contextWindowCompression` is enabled regardless. It addresses the other cap:
without it an audio-only session dies at 15 minutes even if the connection
survives.

### No gate on the audio stream

sidetap's `SilenceGate` drops silent blocks before they are billed, and is
load-bearing enough that `sidetap doctor` fails loudly rather than quietly when
`webrtcvad` is missing — without it the idle cost rises about twentyfold.

Here the stream is sent continuously and ungated. Handing a model that reasons
over continuous audio a stream with the silence cut out changes what it hears,
and the cost saving does not justify that risk when cost is not a deciding
axis.

Silence detection still exists, in a strictly weaker role: **`activity.py`
observes, it does not gate.** It reports speech/silence and never removes a
byte. Two consumers need it — rotation needs to find a pause, and idle-suspend
needs to detect speech onset. The module is renamed rather than ported under
its old name because calling it `vad.py` invites someone to reinstate gating.

It is `webrtcvad-wheels` behind the same lazy import sidetap uses. Absent, the
package still runs: rotation degrades to timer-based and idle-suspend is
disabled, which `doctor` reports as a warning rather than a failure.

sidetap's `aggressiveness = 2` and `SILENCE_TAIL_BLOCKS = 5` are *not* carried
over as settled values. They were inherited from meetscribe, a transcriber with
no latency budget, to decide what to *drop*; here the only question asked of
the detector is "has speech stopped for `ROTATE_PAUSE_S`", which is a different
question with different consequences for being wrong. A false pause rotates the
session mid-sentence. Both constants are tuned against experiment 3, not
assumed.

The idle cost is instead removed by closing the session entirely after
`IDLE_SUSPEND_S` (45 s) of silence and reopening on speech onset — the same
machinery rotation already needs, pointed at nothing. See *Latency and cost*.

## Architecture

### Signal flow

Both directions are the same `DirectionInterpreter`, instantiated twice. They
differ in source node, destination sink, target language and echo policy.

```
=== DIRECTION "in"  (them -> you) ==============================================

 messenger  --additive pw-link-->  recorder --> DirectionInterpreter
 Stream/Output      (tap.py)      16k s16 100ms         |
      |                                      target = --my-lang
      |                                      echo    = false
      |                                                |
      |                                          24k s16 chunks
      v                                                v
 sidetap_duck  <--[closed while audio flows]--  playout --> headphones

=== DIRECTION "out" (you -> them) ==============================================

 USB mic --> recorder --> DirectionInterpreter --> playout
                         target = --their-lang        |
                         echo   = true                v
      messenger input <-- sidetap_virtmic <-- loopback <-- sidetap_tts_sink
       (your real mic is never linked here,   [permanent, from pipewire.conf.d]
        except while bypassed)
```

The audio contract needs no adaptation in either direction.
`gemini-3.5-live-translate-preview` takes raw 16-bit PCM, 16 kHz, mono,
little-endian, in 100 ms chunks, and returns raw 16-bit PCM, 24 kHz, mono,
little-endian. Those are sidetap's `BLOCK_BYTES = 3200` and `TTS_RATE = 24_000`
exactly. No resampling is added in Python, on either side, for the same reason
sidetap adds none.

### Concurrency: threads, with one asyncio island

sidetap is threaded because Google's streaming SDKs are synchronous generator
APIs. `google-genai`'s Live session is asyncio-native, so this design keeps the
ported threaded structure and confines asyncio to one place: each
`DirectionInterpreter` owns a thread that runs its own event loop, and talks to
the rest of the process through `queue.Queue` exactly as sidetap's workers do.

The alternative — converting the ported capture, tap, routing and playout code
to asyncio — was rejected. It is subprocess-and-thread shaped, it is the tested
part, and rewriting it would put the comparison's foundation at risk to serve
the part being measured.

Per direction: a capture thread (`capture.py`, ported), an interpreter thread
owning the asyncio loop and the Live session, and a playout thread. Two more
run session-wide: the routing watcher and the capture health poller. Textual
owns the main thread and **the pipeline never calls into it** — workers write
to a lock-guarded `Metrics` snapshot that `tui.py` polls at 10 Hz. That one-way
dependency is what makes `--no-tui` and the headless suite the same code path.

### Modules

| Module | Role | Origin |
|---|---|---|
| `types.py` | value types and audio constants; stdlib only | ported, trimmed |
| `ports.py` | every `Protocol` — one real implementation, one fake | ported + `InterpreterSession` |
| `graph.py` | parses `pw-dump` into a `PwGraph`; pure, never spawns | ported |
| `recorder.py` | builds `pw-record` argv, frames stdout into blocks | ported |
| `tap.py` | `AppTap` — links a matching application's ports | ported |
| `capture.py` | owns recorders, queues and capture threads | ported |
| `activity.py` | `SpeechActivity` — observes speech/silence, gates nothing | rewritten from `vad.py` |
| `live.py` | the Live session adapter over `google-genai` | new |
| `interpreter.py` | `DirectionInterpreter` — lifecycle, rotation, pumps | new |
| `playout.py` | chunk queue, writer thread, `DuckControl` with hysteresis | rewritten |
| `routing.py` | duck loopback lifecycle, re-route, journal, restore | ported |
| `metrics.py` | backlog, offset, overlap, rotations, cost — the TUI's only input | ported, new fields |
| `cost.py` | audio-token rates as a config value | rewritten |
| `doctor.py` | environment checks, writes the virtual-mic config | ported, API checks replaced |
| `transcript.py` | event-stream `.jsonl` + interleaved `.md` | ported, reshaped |
| `tui.py` | Textual dashboard, hotkeys | ported, new headline row |
| `run.py` | `Session` — builds every stage, startup/shutdown, signals | ported, rewired |
| `adapters.py` | the real ports — every subprocess on the audio path | ported |
| `cli.py` / `__main__.py` | argparse, `doctor`/`devices`/`run` dispatch | ported, flags changed |

Deleted with no successor: `asr.py`, `segment.py`, `translate.py`, `tts.py`,
`rotation.py`.

`rotation.py` goes because its entire job was mapping a recognition offset back
onto the session timeline *through gated silence* — `AudioTimeline` existed
because audio time was not elapsed time. With no gate there are no offsets to
correct, and an event's arrival time is its timestamp. sidetap's invariant
"audio time is not elapsed time" therefore does not carry over, and its absence
should not be read as an oversight.

### The new port

```python
@runtime_checkable
class InterpreterSession(Protocol):
    """One live speech-to-speech translation session.

    The only thing in the package that talks to Gemini. A fake in
    tests/conftest.py emits scripted event sequences, which is what lets the
    rotation state machine, duck hysteresis, backlog accounting and transcript
    assembly all be tested with no network and no API key.
    """

    def send(self, pcm: bytes) -> None: ...

    def events(self) -> Iterator[SessionEvent]: ...

    def close(self) -> None: ...
```

`SessionEvent` is a closed union of what the design reacts to: `AudioOut(pcm)`,
`SourceText(text)`, `TargetText(text)`, `GoAway(time_left_s)`,
`ResumptionHandle(handle)`, `Closed(reason)`. Anything the API sends that is
not one of these is logged and dropped at the adapter boundary, so the state
machine above it has a finite input alphabet.

## Session configuration

Two sessions, never one: `targetLanguageCode` is fixed per session, so the
directions cannot share a WebSocket.

| | IN (them → you) | OUT (you → them) |
|---|---|---|
| `targetLanguageCode` | `--my-lang` | `--their-lang` |
| `echoTargetLanguage` | `false` | `true` |
| `inputAudioTranscription` | on | on |
| `outputAudioTranscription` | on | on |
| `contextWindowCompression` | sliding window | sliding window |
| `sessionResumption` | requested | requested |

**Neither language flag is a source hint.** In sidetap the channel fixes the
source language and nothing is language-detected; here the model auto-detects
the source, and `targetLanguageCode` is the only language input it accepts. So
`--my-lang` is used solely as IN's target and `--their-lang` solely as OUT's
target, and a misdetected source is unpreventable rather than merely unhandled.
That is what makes experiment 5 load-bearing: the model card flags non-native
accents specifically, and the author speaking accented English into the OUT
direction is the case most likely to hit it.

**The echo asymmetry is not a tuning choice, it is a consequence of the duck
policy.** With `echo: false` on IN, a remote party already speaking your
language produces no output; no audio flows; the duck opens; you hear them
raw. The degenerate case handles itself with no code on the path.

OUT cannot do that. Your real microphone is never linked to the messenger, so
no output means the remote party hears *nothing at all* — there is no raw
signal to fall through to. Hence `echo: true`. The model card warns that
*"background noise may introduce artifacts in the translated audio when input
audio is in the target language"*, which is exactly this setting, so it is
exposed as `--echo-out` / `--no-echo-out` rather than fixed.

`sessionResumption` is requested on both directions even though rotate-at-a-
pause does not normally use the handles. They are what the forced fallback
needs, and a handle cannot be requested after `GoAway` has already arrived.

## Session lifecycle

One state machine per direction, in `interpreter.py`:

```
  SUSPENDED ──speech onset──> OPENING ──ready──> RUNNING
      ^                          ^                 │
      │                          │            GoAway(time_left)
      └──idle > IDLE_SUSPEND_S───┼─────────────────┤
                                 │                 v
                                 └────────── OVERLAPPING
                                   (both sessions fed; switch when the
                                    replacement is warm AND the outgoing
                                    output is silent, then close outgoing)
```

- **RUNNING** — audio streams continuously to one session; its events pump out
  to playout, transcript and metrics.
- **OVERLAPPING** — `GoAway` has arrived. A replacement is opened at once and
  **every captured block is sent to both sessions.** The replacement's output
  is discarded. The switch happens when both hold: the replacement has emitted
  energy-bearing audio (it is warm, ~3 s), and the outgoing session's output is
  currently silent (the join lands in a gap). The outgoing session is then
  closed — mandatory, or it takes a 1008 abort at the deadline.
- **SUSPENDED** — no session at all, after `IDLE_SUSPEND_S` of silence. Capture
  keeps running and keeps filling the pre-roll ring; the first speech onset
  opens a session and replays the ring so the onset is not lost.

`OVERLAP_MAX_S` (15 s) bounds the overlap. If no output gap has appeared by
then, switch anyway — a join mid-word is worse than nothing, but far better
than overrunning the 50 s deadline and losing the connection outright. That
case is counted separately (see below), because a run where most rotations hit
the bound means the join-point rule needs revisiting.

**The pre-roll ring now has one consumer, not two.** Waking a suspended session
still replays it, because the speech that triggered the wake happened before
there was a session to send it to. Rotation replays nothing: the replacement
has been hearing the conversation for seconds before it takes over. That is the
whole point of overlapping.

## Playout and the duck

`playout.py` is rewritten rather than ported. sidetap queues whole utterances
with known durations and drops the oldest past a 20 s lag cap; there are no
utterances here. It becomes a chunk queue with a writer thread, and `pw-cat`'s
own buffer provides realtime pacing for free.

**AMENDED 2026-09-20 after experiments 2 and 3: the duck triggers on audio
ENERGY, not on bytes arriving.**

This section originally said the duck closes when translated audio starts
flowing and opens when it stops. That rests on an assumption the measurements
falsified: that the model goes quiet when it has nothing to translate. It does
not. `gemini-3.5-live-translate-preview` emits a **continuous 24 kHz output
stream regardless of whether it is translating anything** — experiment 3 fed
154 s of pure digital silence and got ~151 s of audio back, and experiment 2
fed 96.2 s of speech already in the target language (so `echo_target_language:
false` correctly suppressed translation) and still got 98.2 s of output.

That output is not silence, either. It is low-level non-zero: peak 1078 of
32767 across a 98 s run, with 0.4% of samples non-zero.

Keyed on byte presence, the duck would therefore close on the first chunk and
**never reopen for the rest of the call** — the user would hear nothing from
the remote party at all. That is exactly the "stuck closed" failure this
document elsewhere calls silently cruel, reached from a direction the original
design did not anticipate.

The fix is contained, because the machinery already exists: `playout.py`'s
`find_silence_boundary` computes peak amplitude over 20 ms frames for the lag
cap, and the duck simply has to use the same measure. Note the originally
proposed `SILENCE_PEAK = 600` is **too low** — the measured idle peak of 1078
would read as speech. Experiment 2's re-run reports the energy distribution
when idle versus when actively translating, and the threshold is set from that
gap rather than guessed.

This also revises the cost model. Output billing is not proportional to speech,
because output never stops: a session held open through a conversational pause
bills output continuously at $0.0315/min. Idle-suspend covers gaps beyond
`IDLE_SUSPEND_S`, but not ordinary within-call pauses, so the "ordinary
conversation" row in *Latency and cost* understates the real figure. It will be
restated once experiment 4 measures the duty cycle.

**The duck is driven from this queue, with hysteresis.** It closes on the first
chunk enqueued and opens only after `DUCK_HOLD_S` (0.4 s) with nothing queued
*and* nothing playing. Without the hold it flaps in the gaps between chunks and
chops the original into fragments — which would be heard as the duck failing,
not as hysteresis missing.

`DuckControl` keeps sidetap's two ported properties verbatim: `close()` flips
its internal flag only on a *successful* `wpctl` call, and the writer's
`finally` calls `open()` unconditionally. The reasoning is unchanged — a duck
stuck closed silences the person you are talking to and leaves them speaking to
nobody, which is worse than the program not working.

**Backlog is measured and barely capped.** If translated audio consistently
runs longer than its source, backlog grows, exactly as it does in sidetap. Here
that growth *is the experimental result*, so the cap is a safety valve at a
high default (`--lag-cap`, 30 s) and drops only at a silence boundary in the
output rather than mid-word. Boundary detection is a peak-amplitude threshold
over 20 ms frames of 24 kHz s16, computed with `array` — no numpy on the audio
path, in either direction, as in sidetap.

## Measuring the result

Four numbers, sampled into the lock-guarded `Metrics` snapshot the TUI polls at
10 Hz:

- **`backlog_s`**, per direction — translated audio queued but unplayed. Under
  sidetap this grows monotonically whenever someone talks continuously until
  the lag cap starts eating sentences. Whether it stays bounded here is the
  result. It replaces the per-stage `asr/mt/tts` row as the TUI's headline.
- **`offset_s`**, per direction — speech onset to the first output chunk of
  that stretch. Backlog says the queue is healthy; offset says whether you are
  conversing or narrating.
- **`overlap_pct`** — fraction of wall clock where both source tracks carry
  speech at once. This measures the humans, not the system. Interrupting each
  other is ordinary conversation; an interpreter that forbids it has dictated
  the cadence. A call where this stays near zero means people are still taking
  turns for the machine's benefit, whether or not they noticed.
- **`rotations`** — count, split clean versus forced. Under make-before-break
  "forced" means the overlap reached `OVERLAP_MAX_S` without the outgoing
  session's output ever falling silent, so the join landed mid-speech. A run
  where most rotations are forced means the join-point rule needs revisiting,
  which is why the split is recorded rather than just the total. `replayed_s`
  now counts only idle-suspend wakes: a rotation replays nothing, because the
  replacement has been listening for seconds before it takes over.

## Interface

### Commands

`doctor`, `devices` and `run` are ported. `devices` is unchanged — the
start-your-call-first confusion it exists to resolve is a PipeWire property,
not a pipeline one.

`doctor` keeps every PipeWire check (version, `pw-*`/`wpctl` binaries, the
virtual mic, a live `pw-link`), keeps `--install` and `--repair`, and replaces
the three Google Cloud API checks with one: a real Live session opened and
closed against the configured key. The `webrtcvad` check becomes a
`SpeechActivity` check and **stops being a hard failure** — a missing detector
no longer multiplies the bill twentyfold, it degrades rotation to timer-based
and disables idle-suspend, which `doctor` reports as a warning.

`run` flags that carry over: `--app`, `--their-lang`, `--my-lang`, `--no-tui`,
`--lag-cap`. Dropped: `--project`, `--region`, `--mt-region`, `--tts-region`,
`--mt-model`, `--voice-in`, `--voice-out`, `--phrase`, `--speaking-rate-*`.
New: `--duck-level`, `--echo-out` / `--no-echo-out`, `--no-idle-suspend`.

`--phrase` deserves a note: sidetap boosts recognition of names and jargon
through Speech-to-Text's phrase hints. This model exposes no equivalent, so
names are at the mercy of the model. That is a quality difference the
comparison will surface rather than a gap to work around.

### TUI

The layout is ported. The per-direction stage row (`asr` / `mt` / `tts` health
and latency) is replaced by `backlog_s` and `offset_s`, with `overlap_pct`,
rotation count and running cost in the session row. Hotkeys are unchanged in
binding and near-unchanged in meaning:

| Key | Action |
|---|---|
| `b` | Bypass — duck open, real mic linked, playout suppressed both ways |
| `m` | Mute out |
| `f` | Drop backlog |
| `q` | Quit |

`f` now drops queued *chunks* at the nearest output silence boundary rather
than queued utterances, and sidetap's caveat holds unchanged and for the same
measured reason: it cannot cut the audio already inside `pw-cat`'s buffer, so a
short tail plays regardless.

### Transcript

sidetap pairs source and target text into one record per unit, which it can do
because its segmenter defines the units. Here `inputAudioTranscription` and
`outputAudioTranscription` arrive as two independently-drifting streams, and
any pairing would be invented rather than observed.

So `.jsonl` carries timestamped **events** — `{t, direction, kind:
"source"|"target", text}` — appended and flushed as they arrive, so an unclean
exit leaves everything up to that moment on disk. The `.md` interleaves them
chronologically at close. A `meta` line at the head of the file records
`engine: "live"` with the model id.

**This requires a matching change in `sidetap`**: emitting each paired unit as
two events with `engine: "cascade"`. Without it the two systems' transcripts
are not comparable and the head-to-head has no textual evidence. It is small,
it is work in the other repository, and it is part of this project's scope.

## Failure handling

- **Session drops without `GoAway`.** Reconnect with the last resumption
  handle. Under duck policy the failure is already benign on IN: no audio out
  means the duck opens and you hear the unmediated call. `Metrics` marks the
  direction unhealthy.
- **Capture delivers zero bytes.** An unlinked capture node delivers nothing,
  not silence — the tapped application quit, a tab closed. With no gate in the
  path this now means we simply stop sending, which looks healthy at every
  downstream stage. sidetap's `NO_AUDIO_S = 15.0` watchdog on the capture queue
  is ported unchanged and is the successor to sidetap's keepalive invariant,
  which has none of its own: there is no stream to keep alive when the stream
  is continuous by construction.
- **Speech in, nothing out.** `DeadAirWatch` is ported and generalised to both
  directions. IN degrades gracefully now, so the alarm matters most on OUT,
  where silence means the remote party hears nothing and has no way to know.
- **Language detection failure.** The model card flags non-native accents
  specifically. When `SourceText` arrives in an unexpected language the TUI
  marks it; there is no automatic recovery.
- **Voice instability across rotation.** The model card warns voices *"may
  shift after long pauses"* — and this design rotates at pauses deliberately.
  If voice identity resets at every seam, the seam designed to be inaudible
  becomes the most audible thing in the call. Measured by experiment 3; if
  real, the remedy is make-before-break rotation mid-speech and the seam design
  changes.
- **Journal before you touch the graph.** Ported verbatim. `Router` writes
  every planned link/unlink atomically to
  `~/.local/state/sidetap_live/routing-journal.json` *before* calling the
  linker, so a `kill -9` in between is recoverable with `doctor --repair`.
- **Bypass's real-mic link is torn down by `Session.shutdown()` itself**,
  before anything else, because it is deliberately not journalled and
  `--repair` cannot find it. Ported unchanged.

## Testing

The suite runs with no audio hardware, no network and no credentials. Every
subprocess, socket and clock sits behind a `Protocol` in `ports.py` with a fake
in `tests/conftest.py`; the ported modules arrive with their ported tests.

`InterpreterSession`'s fake is the centre of the new testing. It emits scripted
`SessionEvent` sequences, which makes the following testable offline:

- the rotation state machine — clean rotate, forced rotate, `GoAway` arriving
  mid-speech versus mid-silence, idle suspend and wake, and a session that dies
  with no warning at all;
- duck hysteresis, including that a chunk gap shorter than `DUCK_HOLD_S` does
  not reopen it;
- backlog accounting and silence-boundary dropping;
- transcript event assembly and ordering across two drifting streams.

`tests/fixtures/pw_dump_real.json` is ported: a real, scrubbed `pw-dump`. Every
other graph fixture was hand-written from the same assumptions `graph.py` was
written from and cannot catch a wrong assumption, because it shares it. That
one can.

`docs/manual-smoke.md` is ported and extended, for what the suite structurally
cannot verify: that ducking actually silences the original, that a messenger
enumerates the virtual mic, that a rotation is inaudible, and that the remote
party hears anything intelligible.

## Experiments to run before implementing

Recorded in `docs/experiments/`, following sidetap's precedent of measuring
before deciding.

1. **Does this preview model honour `sessionResumption` and
   `contextWindowCompression`?** Both are documented for the Live API in
   general and unverified for `live-translate`. Rotate-at-a-pause needs
   resumption for its fallback; without it a forced seam is a cold start.
2. **Does it emit `GoAway` with `timeLeft`?** The whole rotation design depends
   on advance warning. Without it, rotation can only run on a timer, which is
   strictly worse and changes the state machine.
3. **Voice stability across a pause-rotation.** The interaction named under
   *Failure handling*. Cheap to measure, and it can invalidate the seam design.
4. **Output pacing.** Does generated audio track input at roughly realtime, or
   arrive in bursts and fall behind? This decides whether `backlog_s` measures
   anything or sits at zero for the whole call.
5. **Accented-English detection** on the OUT direction, which is the author's
   exact case and the one the model card flags.
6. **Cold-start latency.** Bounds both the idle-suspend wake cost and the size
   of a forced seam's hole.

## Latency and cost

Latency is not a deciding axis and there is no region control to tune, so there
is no budget to hit — `offset_s` is recorded to explain the result, not to meet
a target.

Cost is computed exactly rather than estimated. sidetap infers spend from
character counts against prices that render dynamically on Google's pages; here
we count the bytes we send and receive ourselves and multiply audio-seconds by
25 tokens/sec. Rates live in `cost.py` as configuration, at $3.50 per million
input tokens and $21.00 per million output tokens.

| call | sidetap_live | sidetap (STT only) |
|---|---|---|
| idle, sessions suspended | ~$0.00/hr | ~$0.10/hr |
| idle, `--no-idle-suspend` | ~$0.63/hr | ~$0.10/hr |
| ordinary conversation | ~$1.67/hr | ~$1.10/hr |
| both talking continuously | ~$2.52/hr | ~$1.92/hr |

Roughly 1.3–1.5x the cascade while people are talking. The idle row is why
idle-suspend is on by default rather than a flag to remember: ungated
continuous streaming costs about six times sidetap's idle rate, and a session
left running unattended is the one a user is most likely to forget.

These figures are a spend estimate to catch a runaway session, not an invoice.

## Non-goals for v1

- **Voice cloning or speaker-identity preservation.** The model attempts voice
  replication and its own card calls the result inconsistent. Nothing here
  tries to control or improve it.
- **More than two parties, or diarization.** One remote track, one local mic.
- **5.1 routing.** The ported `ports_of()` pairs ports by index after sorting
  by name, which alphabetizes 5.1 to FC, FL, FR, LFE, SL, SR — not positional
  order — and would cross channels. Stereo and mono only, as in sidetap.
- **Any offline or local path.**
- **Vertex AI support.** Not available for this model.
- **Any comparison against sidetap.** Dropped 2026-09-23. This project is
  evaluated on whether it is usable, not on whether it wins a contest, and a
  comparison would have meant carrying a matching transcript schema and a
  converter for no benefit to either side.
- **A replay harness over recorded audio.** `overlap_pct` is about whether two
  people interrupt each other, which cannot be measured from a recording. Live
  calls are the only instrument for that question.

## Done looks like

- `sidetap_live run --app zoom --their-lang ru-RU --my-lang en-US` interprets a
  real two-way call in both directions.
- A session survives an hour, crossing five or six connection boundaries, with
  the rotation count and clean/forced split recorded.
- `backlog_s`, `offset_s` and `overlap_pct` are recorded for at least one real
  call under each system, against a second human.
- A bilingual event transcript is written by both systems in the same schema.
- The suite runs green with no audio hardware, no network and no credentials.
- `docs/experiments/` holds the six measurements above, and any design decision
  they contradicted has been revisited in this document rather than silently
  left standing.
