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
