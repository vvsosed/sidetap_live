# Experiment 6: can the model's padding be told from its speech?

**Date:** 2026-10-08. **Source:** a real 30.5 min Teams call, ru↔en, recorded
with `--probe-audio`. `transcripts/20261008-100047-141709.*`.

## Why this was needed

Two previous answers to "how do we stop queue depth becoming latency" were
both calibrated on something that did not describe a live call.

`SPEECH_PEAK = 2000` was set above experiment 2's measured idle peak of 1078.
That measurement came from a session fed prerecorded audio **already in the
target language** — "nothing worth translating" — so it described a model with
nothing to do, not a model translating a conversation.

Experiment 4 measured the speech-time ratio at 0.894 and set the raw-byte
ratio aside as "uninformative". The playout queue is fed raw bytes, so the raw
ratio is the only one it responds to.

Each time, the lab proxy stood in for the thing the code actually keys on. So
this measures the real stream: every output chunk's per-frame peak, beside the
model's own signal for when it is speaking (seconds since the last output
transcription), which does not depend on amplitude at all.

## Results

### Delivery rate — the earlier figures were wrong

| | audio delivered | over a 1,829 s call | rate |
|---|---|---|---|
| IN | 1,877 s | | **1.026x realtime** |
| OUT | 213 s | | **0.116x realtime** |

**There is no 1.89x structural surplus.** IN runs 2.6% over realtime; OUT is
nowhere near it, because the session closes on idle (5 times on this call).

The earlier 1.89x / 1.63x / 1.44x figures were inferred from logs as
`(call + cap_discarded) / call`, which attributes every lag-cap discard to
excess inflow. Much of that discard was the suppression bug below refilling
the queue, not the model overdelivering.

### Chunk size — previously unmeasured

**12,000 bytes = 250 ms**, every chunk, both directions. That is **12.5**
playout frames of 960 bytes, so no chunk is frame-aligned and every one ends
mid-frame.

### Frame energy, split by the model's own speaking signal

Frame peaks, out of 32767:

| | p1 | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---|---|---|---|---|---|---|
| IN speaking | 0 | 0 | 179 | **5480** | 14478 | 19761 | 24166 |
| IN idle | 0 | 0 | 0 | **0** | 0 | 37 | 18492 |
| OUT speaking | 0 | 24 | 116 | **591** | 1395 | 2136 | 4053 |
| OUT idle | 0 | 0 | 0 | **0** | 29 | 79 | 584 |

Two findings, both consequential.

**What the model emits between utterances is exact digital silence**, not a
1078-peak hiss. 21.2% of the IN stream and 48.0% of OUT are exact zeros — on
OUT in just **8 runs with a median length of 847 frames, about 17 s each**.

**OUT's translated speech looked an order of magnitude quieter than IN's** —
median peak 591 against 5480, p90 2136 — but see the addendum: that was a
small-sample artefact, not a property of OUT.

### What a drain can remove, with no risk

Share of the whole stream sitting in a run long enough to act on, measured
across chunk boundaries:

| threshold | runs ≥200 ms | IN | OUT |
|---|---|---|---|
| exact zero | ✓ | **20.1%** | **47.9%** |
| < 50 | ✓ | 25.5% | 59.0% |
| < 300 | ✓ | 32.0% | 77.3% |

Against a surplus of **2.6% on IN and none on OUT**, dropping only
near-silence has roughly an order of magnitude of headroom — and carries no
risk, because there is nothing in it to lose.

## Conclusions

1. **`DRAIN_PEAK = 64`, separate from `SPEECH_PEAK`.** The drain needs "is
   this silence", the duck needs "is the model translating", and OUT's speech
   median of 591 means one threshold cannot serve both. A drain keyed on 2000
   cut most genuine OUT speech — and OUT has no raw path to fall back to, so
   what it cut the remote party never heard.

2. **The queue-depth problem is small and solvable.** 2.6% to shed, 20%
   available at zero risk.

3. **The real cause of the 30 s backlogs was suppression, not inflow.** On
   this call every one of the 325 lag-cap drops fell inside two windows —
   14 drops in 2 s on OUT only, and 311 in 81 s on both directions — with
   17 minutes of zero drops between them. `tick()` consumes nothing while
   suppressed, `submit()` had no suppression guard, and leaving suppression
   never flushed, so bypass filled the queue to the cap and handed it back.
   Reproduced: 80 s of bypass left 27.92 s queued and 41.7 s cap-dropped.
   The OUT-only window matches mute, which suppresses only OUT.

## Addendum, 2026-10-09: the fix confirmed, and the OUT level corrected

A 40 min call on the fixed build, with **four bypasses (one of 3.5 minutes)
and a mute held for ~30 minutes**: the lag cap fired **zero times**. The log
is 2 KB with 25 lines, against 746 KB and 6,246 drops on 2026-10-07.

The sharpest part of that test is OUT. Muted from 10:08 to the end, it still
received **2,129 s of audio at 1.123x realtime** — a suppressed direction
taking delivery for half an hour, which before the fix pinned the queue at the
cap within a minute. Nothing was dropped.

**Delivery rate, measured properly for the first time** now the probe records
wall clock beside output-audio time:

| | audio | wall | rate | worst 10 s window |
|---|---|---|---|---|
| IN | 2,844.2 s | 2,611.4 s | **1.089x** | 2.08x |
| OUT | 2,129.5 s | 1,896.9 s | **1.123x** | 2.07x |

So the model does deliver faster than realtime — about 9–12% over, with short
bursts at **2x**. Higher than the 1.026x computed above against call length
rather than the probe's own span, and still comfortably inside the ~20% the
drain has available.

**Correcting this experiment's own OUT finding.** OUT's speech here has a
median peak of **3475** (p90 15875), not 591. The earlier figure came from
3,068 "speaking" frames on a call where OUT carried ~40 s of speech, and the
1.5 s transcription window labelled the surrounding silence as speech, pulling
the percentiles down. Today's sample is 11,323 frames with 59% of them
speaking, and it is the one to trust.

`DRAIN_PEAK = 64` is unaffected — it sits an order of magnitude below either
figure, which is why a conservative threshold was the right choice while the
level was uncertain.

## Still open

**`SPEECH_PEAK` and the duck on OUT — weaker than first thought, not
resolved.** The alarm above rested on the 591 median, which was an artefact.
At 3475 the duck closes on OUT normally. What remains unmeasured is whether
OUT's level is *stable* across calls: one call at 591 and one at 3475 is not a
distribution. Until that is known, `has_speech` on OUT is a thing to watch
rather than a thing to change.

**Bypass and mute were not logged**, which is why the attribution above took
a code-level reproduction to confirm. They are logged at INFO now.

**The probe's `t` was output-audio time only.** It now records wall clock
beside it, so a future file can show whether a burst arrives faster than
realtime — the thing that actually makes queue depth grow.

## Files

- `scripts/analyse_audio_probe.py` — the analysis
- `sidetap_live/probe.py` — the recorder, behind `--probe-audio`
