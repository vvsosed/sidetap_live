# Experiment 6: does this model label the remote speakers?

**Verdict: NO — and not for this model specifically: the Live API does not
support speaker diarization at all.** `gemini-3.5-live-translate-preview`
accepts `AudioTranscriptionConfig(diarization=True)` without complaint and
ignores it. Across a 48.0 s two-voice clip it returned 46 transcription fragments
and **zero** carrying a `speaker_label` — identical to the control run with
the flag off. A raw probe over *every* `Transcription`-shaped field on
`LiveServerContent` found no label anywhere. The flag is inert, so nothing in
this program offers it; `--diarize` was written, measured, and deleted in the
same change that added this file.

Run 2026-09-24, `google-genai` 2.24.0, `scripts/exp06_diarization.py`.

## Why it was worth asking

The IN direction taps the messenger's **output** port, where Zoom, Viber and
Discord have already mixed every remote participant into a single stream.
PipeWire offers no per-person separation at all, so on a call with more than
one person on the far side, the model telling the voices apart is the only way
a transcript could say who said what. `docs/manual-smoke.md` lists "more than
two parties" as untested, and `<session>.original.md` — the record of what was
actually said — attributes all of it to "Them".

`google-genai` 2.24.0 appeared to offer exactly that:

```python
types.AudioTranscriptionConfig(diarization=True)   # "Configures speaker diarization"
types.Transcription.speaker_label                  # 'e.g. "spk_1", "spk_2"'
```

Both fields are real and both type-check. Neither does anything here.
`diarization` lives on the **shared** `AudioTranscriptionConfig` used by every
Live model, and this one is translation-specialised.

## Method

- Clip built by the script: 8 turns × 6.0 s = 48.0 s, hard-cut, alternating
  `tests/fixtures/accented_en_16k.raw` (Slavic-accented English) and
  `tests/fixtures/speech_en_16k.raw` (Russian speech despite the name — see
  `02-voice-stability.md`). Two unmistakably different voices, **no overlap at
  all**, which is far easier than any real call.
- Fed at wall clock, 100 ms blocks of 3200 bytes, config shape otherwise
  identical to `01-connect.md` and to production `build_config()`.
- Phase A with the flag on, phase B the identical clip with it off, phase D a
  raw probe reading every transcription field and every non-`None` attribute.

## Results

| | diarize=True | diarize=False |
|---|---|---|
| input transcription fragments | 46 | 46 |
| **fragments carrying `speaker_label`** | **0** | **0** |
| connect | 521 ms | 396 ms |
| first output | 3.57 s | 3.62 s |
| output audio returned | 53.8 s | 53.8 s |

Phase D, every `Transcription`-shaped field on `LiveServerContent`:

| field | messages | attributes ever non-`None` |
|---|---|---|
| `input_transcription` | 46 | `text`, `language_code` |
| `interim_input_transcription` | **0** | — |
| `output_transcription` | 46 | `text`, `language_code` |

`speaker_label` found anywhere: **0**.

Phase C — the ~11 minute rotation-seam probe, which would have asked whether a
label survives a session change — **was not run.** With no labels there is
nothing for it to measure. The code is still in the script for the day the
model gains support.

## What this rules out, and what it does not

- **Ruled out:** that we were reading the wrong field. Phase D looked at all
  three, including `interim_input_transcription`, which this model never sends
  at all.
- **Ruled out:** that diarization is silently expensive. First output moved by
  −0.05 s, output audio was identical to the byte, and connect times sat inside
  the 477–549 ms range measured in experiment 1. It costs nothing because it
  does nothing.
- **Ruled out by Google's own documentation, found after the measurement:**
  that another Live model would do it. The Live API does not support
  diarization *at all* — "Speaker diarization is not supported in live
  streaming sessions. For speaker diarization, use the non-streaming Audio
  transcription endpoint."
  ([live transcription docs](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe))
  The field is on `AudioTranscriptionConfig` only because that same object
  serves the non-streaming endpoint.
- **Not addressed:** local diarization. Separating voices ourselves would mean
  a neural model on the audio path — torch, and per-frame work in Python — in
  a program whose audio path deliberately carries no numpy. Not attempted.

## There is nothing to switch to

Models visible to this project's key, checked with `client.models.list()` on
2026-09-24:

| model | streaming | diarization |
|---|---|---|
| `gemini-3.5-transcribe` | **no** — upload a recording | **yes**, up to 8 speakers (3+ experimental) |
| `gemini-3.5-transcribe-live` | yes | no |
| `gemini-3.5-live-translate-preview` ← this project | yes | no |
| `gemini-3.1-flash-live-preview` | yes | no |
| `gemini-3.8-live` | yes | no |
| `gemini-3.8-live-extended-thinking` | yes | no |

So this is not a case of having picked the wrong model. Every streaming model
lacks it, and the one model that has it cannot be streamed to. **Do not go
looking for a Live model that does diarization; there is not one.**

## The one route that would work, and what it would cost

`gemini-3.5-transcribe` takes a recording and returns a diarized transcript.
That makes a **post-call** diarized version of `<session>.original.md`
achievable — not a live one. It would need:

- the IN capture written to disk during the call, which this program does not
  currently do at all: the transcript is text only, and `transcripts/` holds
  no audio;
- a second pass over the whole call after it ends, billed separately;
- a length limit checked before relying on it — sources seen so far disagree
  between 30 minutes and 1 hour, and an hour-long call is the normal case
  here;
- and it inherits "attribution for 3 or more speakers is experimental", which
  is exactly the multi-party case that motivated this experiment.

Nothing about this is live, so it cannot help the TUI or the duck. It would
only improve the written record after the fact.

## Two incidental findings

**1. `language_code` is populated on both streams.** Every fragment carried a
non-`None` `language_code` on both `input_transcription` and
`output_transcription`. The probe recorded which attributes were present, not
their values, so *what* it reports is unverified — but the field arrives. That
is interesting because the source language "is not configured, and cannot be"
(README): the model auto-detects it, and experiment 5 established that
detection is the failure mode that would silently mistranslate the OUT
direction on an accented speaker. This field appears to be the model saying
what it decided, per fragment, live. Worth its own experiment.

**2. First output at ~3.6 s corroborates the warm-up figure.** Both runs took
about 3.6 s from connect to the first output byte on a cold session, against
cold-start-to-connect of ~0.5 s from experiment 1. That is the ~3 s of silence
a fresh session emits, which is precisely why rotation is make-before-break
rather than serial — measured independently here, as a side effect.

## The lesson, again

This is the fourth time in this project that a field existing in the SDK has
proved nothing about the model behind it. `translation_config` nested under
`generation_config` type-checks, connects, emits a `DeprecationWarning` and
silently produces a chatbot; `target_language_code="ru-RU"` is accepted and
then closes the socket with 1007 a block or two later; and now `diarization`
is accepted and ignored outright. **Ask the API, not the documentation** — and
when the answer is no, delete the flag rather than shipping a promise.
