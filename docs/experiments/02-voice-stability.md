# Experiment 2: does the output voice survive a session rotation?

**RESOLVED by listening, 2026-09-20: the voice survives the rotation — the
speaker sounds like the same person on both sides of the seam — but the
rotated audio carries audible defects.**

The seam-strategy question this experiment was built to answer is therefore
settled in the design's favour: rotate-at-a-pause does NOT make the voice
shift, so the spec's *Session continuity* decision stands and does not become
make-before-break.

(Superseded verdict, kept for provenance: the decision *was* reversed to
make-before-break, the same day, on the size of the hole rather than the voice
— see the 2026-09-20 addendum at the end of this file. The voice finding
stands; the strategy it was taken to vindicate does not.)

The defects are a separate, smaller matter. Measured over the full runs:

| | continuous | rotated |
|---|---|---|
| mid-speech dropouts (40 ms silence between loud frames) | 3 | 9 |
| discontinuities (sample-to-sample jump > 12000) | 15 | 26 |
| clipping | 0% | 0% |

Seven of the rotated run's 26 discontinuities fall within 1.5 s of the seam at
t=34.25 s. Spread evenly across 99 s, a 3 s window would hold about one — so
that cluster is roughly 9x baseline and genuinely seam-local.

Two conclusions, and they are different in kind:

1. **The model drops audio mid-speech on its own.** The continuous run — no
   rotation anywhere — dropped at 2.5 s, 48.9 s and 57.7 s. This is a baseline
   synthesis-quality property, not a rotation artifact, and it belongs in the
   head-to-head against the cascade rather than in the seam design.
2. **Rotation roughly triples the defect rate and adds a local burst.** At one
   rotation per ~9 minutes that is a brief sub-second glitch six times an
   hour. Accepted for v1 and recorded here rather than engineered away; if it
   proves intrusive in a real call, make-before-break (spec option B) is the
   remedy, and it is now a quality trade rather than a correctness one.

Caveat on strength of evidence: one run each, and a generative model has
run-to-run variance, so the 3-vs-9 ratio should not be read as precise. The
seam-local clustering is the more robust signal.

(Superseded first line, kept for provenance: inconclusive from automated
analysis alone — a human must listen to confirm.) No objective signal (seam-window energy, discontinuity/"click" magnitude, a coarse pitch proxy) shows anything abnormal at the seam, and the seam itself lands in genuine silence on both sides with no audible-looking artifact. But none of those signals can detect a timbre or gender change — that requires an ear. Listen with the `pw-cat` commands at the bottom of this doc before relying on this experiment to settle the seam-strategy question.

A second, unplanned finding turned out to matter more for the playout design: **the model emits a continuous 24 kHz output byte stream almost the entire time a session is open, whether or not it has anything to say.** See [Continuous output stream](#continuous-output-stream) below — this affects the ducking design regardless of how the voice-stability question resolves. (Qualified by experiment 6's 2026-10-13 addendum: continuous is what a *healthy* session does, not a guarantee.)

## Environment

- SDK: `google-genai` 2.24.0, model `gemini-3.5-live-translate-preview` (unchanged from experiments 1 and 3)
- Config shape: unchanged from `docs/experiments/01-connect.md` — `translation_config`, `input_audio_transcription`, `output_audio_transcription` all top-level on `LiveConnectConfig`
- Clip: `tests/fixtures/speech_en_16k.raw`, fed at wall-clock speed (100 ms blocks, `await asyncio.sleep(0.1)`)
- `SPLIT_S = 32.7` (unchanged — midpoint of the clip's longest silent gap, 32.3–33.1 s)
- `TAIL_WAIT_S = 4.0` s after the last block of each segment, before closing that session

## The fixture is not English, and that changed the experiment

Independently confirmed (own analysis, not just an assumption): `tests/fixtures/speech_en_16k.raw` is 96.17 s, peak 24936, 88.5% speech density (20 ms frames, RMS > 200) — matching its documented acoustic profile exactly. But its **actual spoken content, per the model's own `input_transcription`, is Russian**, not English: a documentary-style narration about the historical geography of Yemen ("...близость к морю... воинствующие горные племена... приняли шиитский зейдитский ислам..." — coastal exposure to foreign powers, isolated mountain tribes, the Zaydi Shia tribes of northern Yemen). The `_en_` in the filename is misleading; this is source material recorded from a Russian-language video, not an English reading.

This mattered directly. The first run used `target_language_code="ru"` (the illustrative default carried over from experiments 1 and 3). With the clip's real source language *also* Russian and `echo_target_language=False`, the model treated the input as already being in the target language and suppressed spoken output almost entirely — see [Continuous output stream](#continuous-output-stream) for exactly how little. `output_transcription` never populated once in that run. A 15 s throwaway diagnostic with `target_language_code="en"` on the same audio immediately produced real audio (70% non-zero bytes, peak within 6% of the input's own peak) and populated `output_transcription` text, confirming the cause. `scripts/exp02_voice_stability.py` was corrected to default to `target="en"`; **`SPLIT_S` and everything else about the experiment is unchanged.** This is a parameter-value fix, not a change to the config shape, the model, or playback speed.

All results below are from the corrected (`target="en"`) run: Russian source, English output.

## Results: duration and content

| | bytes | duration |
|---|---|---|
| `continuous.raw` | 4,716,000 | 98.25 s |
| `rotated.raw` | 4,752,000 | 99.00 s |
| — segment A (0–32.7 s of input) | 1,644,000 | 34.25 s |
| — segment B (32.7 s–end of input) | 3,108,000 | 64.75 s |

Both runs produced real, dense speech: peak 27235 (continuous) / 27505 (rotated) out of 32767 full scale (83–84%), comparable to the 16 kHz input's own peak of 24936 (76%). Overall RMS 4610 (continuous) vs 4143 (rotated) — the same order of magnitude, not a 10x quieter/louder split.

**Content continuity across the seam** (from `input_transcription` / `output_transcription`, both logged with timestamps): rotated-A's last transcript pair is `'горные,' → 'mountainous,'`; rotated-B's first pair is `'горные вершины, которые' → 'mountain peaks'`. Comparing against the continuous run's transcript over the same stretch (`'горные,' → 'mountainous,'`, `'э' → 'uh'`, `'горные' → 'mountainous'`, `'вершины, которые' → 'peaks that'`), the rotated run picks up cleanly with **no dropped or duplicated substantive clause**. The one difference: the filler word "э"/"uh" — a hesitation in the original speech landing right at the split point — doesn't appear as its own token in the rotated run; it's silently absorbed across the A/B boundary. `SPLIT_S = 32.7` was chosen as the midpoint of a genuine pause, and this is consistent with that pause containing a brief hesitation rather than true dead air — a barely-there content edge, not a lost clause.

## Seam analysis (rotated.raw, byte offset 1,644,000 = t=34.25s)

| window | peak | RMS |
|---|---|---|
| 1 s immediately before the seam | 0 | 0.0 |
| 1 s immediately after the seam | 24894 | 3530.4 |

The full second before the seam is exact digital silence. Scanning further back: segment A's last audio above a peak-50 noise floor ends at **t=31.35s**, meaning the model had already finished speaking and gone silent **2.8 s before its session closed** — `TAIL_WAIT_S=4.0s` was generous enough that nothing was truncated. Segment B's real speech begins about 0.2 s after the seam (a small onset ramp, then normal speech levels by +0.2s: RMS 3880, peak 7809, climbing to RMS 7000+/peak 20000+ within a second).

**Discontinuity check:** the largest single-sample jump anywhere in `rotated.raw` is 13386, occurring at t=34.87s — during segment B's speech onset, 0.6s after the seam. `continuous.raw`'s own largest jump, with no rotation at all, is **larger** (16375, at t=29.06s, in the middle of ordinary speech). A rotation-caused "click" would be expected to stand out as an outlier; instead the seam's biggest jump is unremarkable next to jumps that occur naturally elsewhere during normal speech.

**Rough pitch proxy (zero-crossing rate, not a real pitch/gender detector):** A's last 500 ms of speech: RMS 4216, ZCR 902/s. B's first 500 ms of speech: RMS 4500, ZCR 824/s. Comparable magnitude, ~9% apart — nothing suggesting an octave-scale register or gender flip, but this is a coarse proxy only. Confirming an actual timbre change requires listening.

**Bottom line on the seam:** every automated signal available without listening — RMS level, click/discontinuity magnitude, rough pitch proxy, transcript continuity — is consistent with a clean handoff. None of them can rule out a perceived voice/gender change, which is exactly the risk the model card warns about. The verdict stays **inconclusive pending a human listen.**

(Superseded verdict, kept for provenance: the listen happened — see the
headline and the 2026-09-20 addendum. It is also the paragraph that shows why
the automated signals were not enough: none of them measured the length of the
silence the rotation produced, which is what killed the strategy.)

## Continuous output stream

This is the more important finding, and it turned up by accident: **the model appears to hold its output audio channel open with a continuous byte stream for as long as a session is connected, regardless of whether it has anything to say.** (Qualified by experiment 6's 2026-10-13 addendum: continuous is what a *healthy* session does, not a guarantee — two sessions in one morning stopped sending the keep-alive stream mid-call and delivered only speech, in bursts, which nothing in the program could see. See `06-padding-separability.md`.) Two independent experiments show this from two different angles:

**exp03 (`03-session-limits.md`), input = literal silence for the whole session:** Phase 1 ran 591.3 s before the connection was aborted; `unexpected_data_bytes` totaled 28,200,000 bytes = 587.5 s of audio-shaped data (**99.36%** of the connection's lifetime) despite there being nothing to translate. Phase 2 (a 60 s resumed session, also silence-fed): 2,676,000 bytes = 55.75 s (**92.9%**).

**exp02's first (miscorrected) run, input = real speech already in the target language ("nothing worth translating," not literal silence):** the `continuous` and `rotated` sessions ran their full ~98–100 s and produced output *byte counts* at essentially the same rate as a normal translating session — but a 20 ms-frame energy analysis shows those bytes carry almost no signal:

| condition | file | total frames (20ms) | frames w/ any non-zero sample | frames w/ peak > 100 | frames w/ peak > 1000 | overall peak (of 32767) |
|---|---|---|---|---|---|---|
| nothing to say (target=source=ru) | continuous | 4912 | 0.49% | 0.16% | 0.04% | 1078 |
| nothing to say (target=source=ru) | rotated | 4987 | 0.24% | 0.10% | 0.00% | 305 |
| actively translating (target=en) | continuous | 4912 | 96.42% | 89.25% | 75.55% | 27235 |
| actively translating (target=en) | rotated | 4950 | 94.26% | 86.36% | 73.72% | 27505 |

So: the "nothing to say" output is **not exactly zero**, but it is overwhelmingly zero — over 99.5% of 20 ms frames are pure digital silence in both files, and the handful of non-zero frames are concentrated in a roughly half-second window right after each connection opens (one blip per session, not scattered noise throughout), consistent with a brief connection/codec-priming artifact rather than the model attempting and failing to speak. Its **worst-case peak across ~200 combined seconds of "nothing to say" audio was 1078** — 3.3% of full scale. The "actively translating" distributions are cleanly separated by two-plus orders of magnitude: even at a threshold as high as 3000 (nearly 3x the worst "nothing to say" peak ever observed), 66–68% of translating frames still exceed it.

**Answered by experiment 6**, which measured a live call rather than a fed
clip: what the model emits between utterances there is *exact digital silence*
— 21.2% of the IN stream and 48.0% of OUT as exact zeros — not a 1078-peak
floor. The 1078 figure is a property of this condition ("nothing worth
translating", prerecorded, already in the target language) and did not transfer.
See `06-padding-separability.md`.

**Why this matters for the design:** the plan's playout ducking closes when output audio is present and reopens when it stops, so the user hears the remote party in the gaps. If ducking triggers on *byte presence*, it will never reopen — bytes arrive whether or not the model is speaking, both when fed silence (exp03) and when fed content it declines to translate (this experiment). That would mute the remote party for the entire call. **Ducking must trigger on energy, not byte presence.** Given the measured distributions, a per-frame (20 ms) peak threshold anywhere in roughly the 300–3000 range (on the int16 scale) cleanly separates the two conditions with wide margin in this data; a real implementation should also add hangover/hold time, as any VAD would, so a single quiet consonant inside active speech doesn't reopen the duck. The exact threshold should be re-validated against more clips, but the two-orders-of-magnitude gap here means it is not a fragile choice.

**Superseded in part by experiment 6**, which did re-validate it, on a real
call. The duck's `SPEECH_PEAK = 2000` sits inside the range suggested here and
holds. A *drain* keyed on 2000 does not: it cut most genuine OUT speech, and
OUT has no raw path to fall back to, so what it cut the remote party never
heard. One threshold cannot serve both questions — "is the model translating"
for the duck, "is this silence" for the drain, which keys on `DRAIN_PEAK = 64`.
See `06-padding-separability.md`.

## Listen for yourself

```
pw-cat --playback --raw --rate 24000 --channels 1 --format s16 docs/experiments/audio/continuous.raw
pw-cat --playback --raw --rate 24000 --channels 1 --format s16 docs/experiments/audio/rotated.raw
```

Focus on the seam at **t≈34.25s** in `rotated.raw` (a ~2.8s pause going in, a brief onset, then segment B's speech). Compare the voice heard just before that pause against the voice heard just after, and against the same stretch of `continuous.raw`, for gender, pitch register, and speaking style — none of which this doc's numbers can confirm.

## Surprises / notes for later tasks

1. **Test fixture provenance should be double-checked before further experiments reuse it.** `speech_en_16k.raw`'s acoustic stats (duration, peak, density) are exactly as documented, but its language is not what the filename implies. Any future experiment assuming "English input" from this file will be wrong in the same way this one initially was.
2. **`echo_target_language=False` appears to suppress speech, not just skip a redundant re-statement, when it judges source == target.** That's a real product behavior worth knowing regardless of this experiment: if a real call has moments where the remote party briefly speaks the *listener's* language, the interpreter may go effectively silent for that stretch. Out of scope to chase further here, but worth flagging for the spec.
3. **The continuous-output-stream finding (see above) should feed directly into whichever task builds `interpreter.py`'s playout/ducking logic** — it changes the trigger condition from "bytes arriving" to "energy above threshold."

---

## Addendum, 2026-09-20: the seam decision was REVERSED to make-before-break

The headline above answers the question this experiment asked, and gets the
decision wrong. The voice does survive a rotation — that part stands. But the
same run killed rotate-at-a-pause on a fault the question never anticipated:
not *what* the listener hears across the seam, but **how long they hear
nothing**.

These figures were recorded in the spec rather than here — see *Session
continuity: make before break* in
`docs/superpowers/specs/2026-09-20-sidetap-live-design.md` (lines 141-205 as
that file stands today) — which is why this file reads as though the strategy
survived. They belong in the experiment record:

| | |
|---|---|
| silence the rotation produced | **3.12 s** |
| pause it was deliberately placed in | 0.80 s |
| longest silence anywhere in the unrotated run | 0.60 s |

The hole is nearly four times the pause meant to hide it, because **a freshly
opened session needs ~3 s before it emits any audio at all** — 3.06 s measured
in `live.py`'s smoke check, 3.54 s in a raw SDK probe. That is not connection
latency (~500 ms, experiment 1); it is the model's own lead-in, and no pause in
ordinary speech is long enough to conceal it. A listener confirmed the seam is
audible in the rotated run and not in the continuous one.

So the seam analysis above is not wrong, it is incomplete: every signal it
measured was about the *join*, and none of them measured the length of the gap
in front of it. "The full second before the seam is exact digital silence" is
recorded above as evidence that nothing was truncated. It is also, read the
other way, the first second of the hole.

**Why the output stream, not the input, supplies the join point.** The original
design had to wait for the speaker to stop, and this same run says how often
that happens: only **11 pauses of ≥0.4 s in 96 s** of dense speech, and just
**2 reaching the 0.7 s** the design wanted. Make-before-break waits for a gap
in the *output* stream instead, and the same run contained **154 internal
output silences** — a join point is always a second or two away. The property
that made the original strategy marginal is the one that makes its replacement
easy.

**What the reversal removed:** `ROTATE_PAUSE_S` and the clean/forced rotation
distinction, because every rotation is now overlapped and there is no degraded
path to fall back to. `03-session-limits.md`'s rotation-window reasoning is
written in those terms and carries a note saying so.

**What survives untouched:** the continuous-output-stream finding above. It was
unplanned, it decided the duck's trigger condition, and the reversal does not
touch it — the duck still keys on energy, and make-before-break keys on the
same measure to find its join point.
