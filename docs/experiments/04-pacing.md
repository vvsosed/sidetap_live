# Experiment 4: does the model keep up with realtime, or fall progressively behind?

**Verdict: FLAT. Across the full 96.2 s of dense (88.5%-density), continuous
Russian narration, the translation shows no measurable growth in lag —
event-by-event lag holds in a 0.19–0.31 s band from the first bucket to the
last, and the cumulative ratio of speech-bearing output seconds to
speech-bearing input seconds stabilizes at ~0.87–0.89 within the first 20 s
and stays there. The system also drained its output within ~2 s of the last
input arriving, not the tens of seconds a real backlog would take to flush.**
This is direct evidence that, for continuous dense speech at this pace and in
this language pair, the turn-free single-model approach does dissolve the
cascade's headline limitation (progressive backlog under continuous speech) —
at least under the one condition tested here. See *Caveats* before treating
this as a universal answer.

## Why the plan's original measurement was not used

The plan specified measuring bytes-out/bytes-in as a proxy for backlog. That
measurement is invalid here: experiments 2 and 3 established that this model
emits a continuous 24 kHz output byte stream almost the entire time a session
is open, whether or not it has anything to say (exp03: 587.5 s of
audio-shaped bytes out of 591.3 s of silence-fed connection time; exp02: 0.04%
of 20 ms frames peaked above 1000 with nothing to translate, vs 75.5% while
actively translating). A raw byte ratio sits near 1.0 regardless of whether
the model is keeping pace or falling behind, so it would have produced a
falsely reassuring (or simply meaningless) number. This experiment measures
two things that are actually informative instead, both described below.

## Setup

- SDK: `google-genai` 2.24.0, model `gemini-3.5-live-translate-preview`
- Config shape unchanged from `docs/experiments/01-connect.md`:
  `translation_config`, `input_audio_transcription`, `output_audio_transcription`
  all top-level on `LiveConnectConfig`
- `target_language_code="en"` (the clip's source is Russian; a `ru` target is
  a same-language no-op that suppresses output, per experiment 2)
- Clip: `tests/fixtures/speech_en_16k.raw`, 96.17 s (padded to 96.20 s for
  block alignment), raw s16 16 kHz mono, fed at wall-clock speed (100 ms
  blocks of 3200 bytes, `await asyncio.sleep(0.1)` per block) — one
  continuous session, no rotation
- Tail: kept the session open past the last input block until output speech
  (not raw bytes — see above) had been quiet for 3.0 s, with a 4.0 s floor
  and a 45 s hard cap (above `LAG_CAP_S`, to see if it would be threatened)

## Measurement 1: transcription lag over time

`input_transcription` and `output_transcription` arrive as separate
timestamped streams. Two proxies were computed:

- **Turn lag** (pair the Nth *finished* input utterance with the Nth
  finished output utterance): **not usable this run.** `finished=True` never
  fired once on either stream across the whole 96 s — the model treated this
  continuous narration as one ongoing turn from start to end. Only the
  trailing, still-open turn was available (1 pair, lag 0.23 s), too coarse
  to say anything about a trend. This is itself worth recording for future
  experiments: turn-boundary lag as a metric requires the clip to contain
  enough pauses to close a turn, and dense continuous narration may not.
- **Event lag** (pair the Nth input_transcription event with the Nth
  output_transcription event, in arrival order — 95 pairs, no turn concept
  needed): this is the metric that answers the question. Per-10s bucket:

| bucket (s) | event lag avg (s) | n pairs |
|---|---|---|
| 0–10 | 0.24 | 6 |
| 10–20 | 0.31 | 10 |
| 20–30 | 0.22 | 10 |
| 30–40 | 0.19 | 10 |
| 40–50 | 0.25 | 10 |
| 50–60 | 0.23 | 10 |
| 60–70 | 0.26 | 10 |
| 70–80 | 0.25 | 9 |
| 80–90 | 0.24 | 10 |
| 90–100 | 0.28 | 10 |

First-half mean 0.24 s vs second-half mean 0.25 s (delta +0.01 s) — noise,
not drift. The lag stays inside a 0.19–0.31 s band for the entire clip with
no directional trend. This lines up with, and sharpens, experiment 2's
informal "roughly 0.2–0.4 s" eyeballed estimate — now confirmed flat across
the full clip rather than a single spot-check.

## Measurement 2: speech-bearing seconds, output vs input

Both streams sliced into 20 ms frames; a frame counts as speech if its peak
sample exceeds 1000 (exp02: worst-case "nothing to say" peak was 1078 over
~200 s combined; actively-translating frames cleared 1000 in 75%+ of cases —
two-plus orders of magnitude of separation).

| bucket (s) | in speech (s) | out speech (s) | ratio | cum in (s) | cum out (s) | cum ratio |
|---|---|---|---|---|---|---|
| 0–10 | 7.70 | 4.64 | 0.60 | 7.70 | 4.64 | 0.60 |
| 10–20 | 7.28 | 8.12 | 1.12 | 14.98 | 12.76 | 0.85 |
| 20–30 | 8.08 | 7.92 | 0.98 | 23.06 | 20.68 | 0.90 |
| 30–40 | 7.78 | 6.48 | 0.83 | 30.84 | 27.16 | 0.88 |
| 40–50 | 8.76 | 7.08 | 0.81 | 39.60 | 34.24 | 0.86 |
| 50–60 | 8.32 | 8.04 | 0.97 | 47.92 | 42.28 | 0.88 |
| 60–70 | 8.48 | 7.36 | 0.87 | 56.40 | 49.64 | 0.88 |
| 70–80 | 7.76 | 6.60 | 0.85 | 64.16 | 56.24 | 0.88 |
| 80–90 | 7.98 | 6.20 | 0.78 | 72.14 | 62.44 | 0.87 |
| 90–100 | 4.94 | 6.24 | 1.26 | 77.08 | 68.68 | 0.89 |
| 100–110 (tail) | 0.00 | 0.24 | inf | 77.08 | 68.92 | 0.89 |

Per-bucket ratio bounces (0.60–1.26, as expected — sentence-level phrasing
doesn't map 1:1 second-by-second), but the **cumulative ratio settles by
bucket 2 (t=30s) at ~0.86–0.90 and stays there for the rest of the clip.** It
never climbs toward or past 1.0. Total: 77.08 s of speech-bearing input vs
68.92 s of speech-bearing output, ratio **0.894** — the translated speech
occupies *less* wall-clock time than its source, on average, not more. (Raw
output bytes, for reference only and known to be uninformative: 99.50 s —
this is the number the plan's original metric would have reported, and it
would have obscured everything above.)

## Tail drain

Sending finished at t=98.29 s (96.20 s of clip plus connect delay). The last
speech-bearing output frame arrived at t=100.23 s — **only 1.93 s later.**
Output speech was confirmed quiet (idle ≥3.0 s) by t=103.31 s, a 5.01 s tail
overall. This is consistent with the model finishing the one sentence it was
mid-way through when input stopped, not with draining an accumulated
backlog — a real backlog of the kind the cascade suffers from would show a
tail measured in many seconds to tens of seconds, not two.

## What this implies

**For `LAG_CAP_S` (currently 30 s):** nothing observed here threatens it, or
even approaches it. Steady-state event lag (0.2–0.3 s) is two orders of
magnitude below the cap, and the full-clip tail-drain (2–5 s) is a small
fraction of it. This run supports treating `LAG_CAP_S` exactly as the plan
now assumes — **a safety valve for pathological cases, not a working limit
this system is expected to approach in normal operation.**

**For the project's headline question:** this is direct, positive evidence
that a turn-free speech-to-speech model does dissolve the cascade's
documented worst limitation (progressive backlog under continuous dense
speech forcing a "speak, pause, let it interpret" cadence). Over 96 s of
88.5%-density continuous narration — deliberately chosen to be a hard case,
with almost no natural pauses — lag did not grow and the speech-time ratio
did not climb. **No active backlog management (dropping audio, skipping
ahead, forcing pauses) appears necessary for this condition.**

**Caveats, so this isn't over-read:**
- One run, one clip, one language pair (ru→en), one speaker, ~96 s. Model
  output is generative and exp02 already documented run-to-run variance in
  unrelated respects (dropout counts); a single run is directional evidence,
  not a guarantee.
- No `finished=True` ever fired, so the cleaner turn-boundary lag metric
  could not be cross-checked against the event-lag numbers here — only the
  event-lag proxy was available. It matches experiment 2's independent
  eyeballed estimate closely, which raises confidence, but a future
  experiment with a clip that contains genuine pauses (to force turn
  boundaries) would let both methods be checked against each other directly.
- This is single-direction, single-speaker audio with no overlapping speech,
  interruptions, or the two-way nature of a real call. A real call's harder
  cases (crosstalk, a much longer session, faster speech, a language pair
  with a larger length mismatch) are untested here.
- 96 s is short next to a real call. Nothing here rules out a *slow* drift
  that would only become visible over many minutes — this run does establish
  that whatever mechanism would cause such a drift is not visible at the
  timescale and speech density tested.

## Files

- `scripts/exp04_pacing.py` — the experiment
- Raw run log (not committed): `/tmp/exp04.log`

## Addendum, 2026-10-05: the raw-byte ratio is the one that governs backlog

This experiment's headline number, the 0.894 speech-time ratio, is correct and
still the right answer to "does the model translate faster than it is spoken".
It is the wrong number for "does the playout queue grow", and that distinction
cost a real call.

The figure set aside above as "for reference only and known to be
uninformative" — **raw output 99.50 s against 98.29 s of sending, a ratio of
1.012** — is the only one the playout queue responds to.
`DirectionInterpreter._dispatch` enqueues every `AudioOut` byte, speech or not,
and `Playout` drains exactly one 20 ms chunk per tick paced by `pw-cat`, i.e.
at precisely realtime. So inflow is *raw bytes* and outflow is realtime: any
raw ratio above 1.0 accumulates as queue depth, and **queue depth is latency**,
not a statistic. The speech-bearing ratio never enters into it.

Every translating run on record is above 1.0 on that measure: this one 1.012,
exp02's `continuous.raw` 98.25/96.17 = 1.022, exp02's `rotated.raw`
99.00/96.17 = 1.029. At those rates it takes 9–22 minutes to build 16 s of
delay. This run lasted 96 s and accumulated 1.2 s, which is invisible. The last
caveat above called it: "96 s is short next to a real call. Nothing here rules
out a *slow* drift that would only become visible over many minutes."

It was not a slow drift.

### What a real call did

`transcripts/20261005-100120-593938.log`, a 31.7 min Teams call, ru↔en:

| | IN | OUT |
|---|---|---|
| lag-cap drop warnings | 7,931 | 3,069 |
| audio discarded by the cap | **1,688.0 s** | 828.7 s |
| share of the stream discarded | **89%** | 44% |
| inflow (played + discarded ÷ elapsed) | **1.89x** | 1.44x |

First drop at **t=+49 s, already 30.2 s behind**: the queue saturated
`LAG_CAP_S` inside the first minute and stayed pinned there for the remaining
31 minutes, with the cap discarding real audio continuously to hold the line.
Every one of the 11,000 drops reported "at a pause", median 0.1 s — the queue
was riddled with silence. Reproduced on the 2026-09-30 call: 30 s by t=+3.5 min,
34.7 s discarded.

The surplus is not speech, and the transcript proves it: IN carried 22,024
source characters against 22,789 target (**1.035**), OUT 7,778 against 7,850
(**1.009**), with 6 duplicate consecutive fragments in 1,683. The model is
neither verbose nor repeating. Attributing the 22,024 characters to speech at
conversational pace and applying this experiment's own 0.894 ratio gives
~1,313 s of actual speech inside 3,588 s delivered — so roughly **37% speech,
63% keep-alive padding**, and speech alone arrives at **0.69x realtime**.

That last figure is the important one: **speech fits.** Only the padding pushes
inflow over 1.0, which is why discarding padding is always sufficient and no
translated audio ever has to be cut.

### Confirmed by replay

Driving `Playout` offline with that profile (1.89x inflow, 37/63 split)
reproduces the failure and the fix:

| | before | after `TARGET_LATENCY_S` |
|---|---|---|
| first lag-cap drop | t=+34 s (real: +49 s) | never |
| backlog | pinned 30 s (real: 30 s) | peak 1.6 s |
| real audio cut over 31.7 min | 65% (real: 89%) | **0.0 s** |
| padding discarded | — | **~1,690 s** (real IN: 1,688.0 s) |

The padding figure matching the real log to within a second or two, from an
independently derived split, is what confirms the diagnosis.

### What the drain does not fix

Three limits, all measured, none of them the bug above:

- **Submit granularity used to matter and must not.** A first version removed
  only the one quiet run at the head of the queue, once per `submit()`, which
  rate-limits disposal to submits per second. `live.py` emits one `AudioOut`
  per server message, so the chunk size is the model's choice and nothing here
  measures it: at 100 ms chunks that version fell 30 s behind and the cap cut
  89 s of speech, while the identical stream fed one 20 ms frame at a time
  held 1.0 s. Compacting from anywhere in a 2 s window makes all three
  granularities behave identically (peak 1.6 s, nothing cut).
- **A long uninterrupted speaking stretch costs latency that cannot be
  drained.** The model generates faster than realtime, so during one
  continuous stretch it runs ahead and the queue holds *real speech*. Measured
  at 1.89x inflow: 2 s speech runs give a 1.6 s peak, 10 s runs 8.4 s, 30 s
  runs 26.2 s — all with nothing cut — and 60 s runs saturate the cap and lose
  53.9 s. The 0.69x figure above is a call average, not an instantaneous
  bound. Bounding this needs time-stretching the output, not discarding it.
- **The drain did not work on a real call, and this is why.** On 2026-10-06
  (`transcripts/20261006-100025-886964.log`, 11.4 min) the drain removed
  **nothing** and the queue pinned at the cap again, 1,965 drops. The OUT
  direction is the proof it was not a speech-lead problem: 667 source
  characters, about **40 s of speech in a 683 s call — 0.06x realtime** —
  inside ~732 s delivered, so that queue was **95% padding by volume**. IN was
  66% padding, speech 0.48x realtime. Both are fully removable in principle.
  The classifier is what failed: the log's drop sizes are the leading
  non-quiet run, **median 0.10 s**, so on a live call the keep-alive stream
  crosses `SPEECH_PEAK` every ~100 ms and no quiet run reaches
  `MIN_DRAIN_RUN_MS`. Experiment 2's idle-peak figure of 1078 came from a
  session fed prerecorded audio already in the target language — "nothing
  worth translating" — and did not transfer to a live call. Reproduced
  offline: a stream that is 95% padding but crosses 2000 every 100 ms gives
  peak backlog 30.0 s, drained 0.0 s, cap cut 58.0 s.
  **Whether padding is separable from speech at all is now experiment 6**;
  run `sidetap-live run --probe-audio` on a real call and analyse it with
  `scripts/analyse_audio_probe.py`.
- **Padding finer than ~200 ms is indistinguishable from speech.** A quiet run
  of one or two 20 ms frames is exactly what occurs inside a word, by the same
  `SPEECH_PEAK` test, so the drain declines and leaves it to the cap.
  Experiment 2 found the model's idle stretches to be long runs of digital
  silence, which the drain does resolve, but nothing measures how finely it
  interleaves padding *with* speech.

### What this changes

`LAG_CAP_S` was never the problem — treating it as the *only* backpressure
was. It is a valve against runaway speech and may cut speech to act, so
reaching it is a quality loss. `TARGET_LATENCY_S` keeps it out of reach by
discarding non-speech from the head of the queue at 1 s, counted separately as
`squelched_s` because it is not a loss.

**For future experiments: report the raw ratio alongside the speech ratio, and
run long enough to see minutes of accumulation.** A 96 s run cannot see this
class of bug, and a speech-bearing metric cannot see it at any length.
