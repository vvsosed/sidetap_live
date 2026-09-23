# Experiment 1: connect + cold-start latency

Answers spec experiment 6: how long does opening a Live session take? This
bounds both the idle-suspend wake cost and the size of a forced seam's hole.

## Environment

- SDK: `google-genai` **2.24.0** (pinned in `uv.lock`; `pyproject.toml` only
  requires `>=1.0.0`, so pin behaviour matters — see Surprises below)
- Model id that actually worked: **`gemini-3.5-live-translate-preview`**
  - Confirmed present in `client.models.list()` for this API key, alongside
    `gemini-3.5-transcribe-live`, `gemini-3.1-flash-live-preview`,
    `gemini-3.8-live`, `gemini-3.8-live-extended-thinking`. No name change
    was needed.
- Connected with the bare model id (no `models/` prefix) — worked as-is.

## The plan's hypothesis was wrong in one specific way

The plan (written from REST docs) nested `translation_config` inside
`generation_config`:

```python
types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    generation_config=types.GenerationConfig(
        translation_config=types.TranslationConfig(...),
    ),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
)
```

In `google-genai` 2.24.0, `LiveConnectConfig` has **`translation_config`,
`input_audio_transcription`, and `output_audio_transcription` as its own
top-level fields** (siblings of `generation_config`, not children of it).
`GenerationConfig` *also* happens to expose a `translation_config` field
(and an `audio_transcription_config` field) as a legacy/back-compat
pass-through, and setting `LiveConnectConfig.generation_config` at all now
raises:

```
DeprecationWarning: Setting `LiveConnectConfig.generation_config` is
deprecated, please set the fields on `LiveConnectConfig` directly. It will
be removed in the next major version (not before 7/31/2026).
```

The nested form still connects today (verified directly, see below) but is
explicitly deprecated and scheduled for removal, so it must not be used
going forward. This experiment uses the corrected, non-deprecated top-level
form, which produces no warnings.

### Field-name comparison (REST doc name vs. SDK 2.24.0 name)

| REST doc name              | SDK 2.24.0 name                            | Where it lives                                                |
|-----------------------------|---------------------------------------------|----------------------------------------------------------------|
| `translationConfig`         | `translation_config`                        | **Top-level field of `LiveConnectConfig`** (alias `translationConfig`), not under `generationConfig` |
| `targetLanguageCode`        | `target_language_code`                      | Field of `types.TranslationConfig` — unchanged shape |
| `echoTargetLanguage`        | `echo_target_language`                      | Field of `types.TranslationConfig` — unchanged shape |
| `inputAudioTranscription`   | `input_audio_transcription`                 | Top-level field of `LiveConnectConfig` (alias `inputAudioTranscription`) — matches plan already |
| (implied) `outputAudioTranscription` | `output_audio_transcription`       | Top-level field of `LiveConnectConfig` — matches plan already |

Every *leaf* field name and casing convention (snake_case Python attr with a
camelCase REST alias) matched the REST docs exactly. The only structural
error was the **nesting level** of `translation_config`: the plan put it
under `generation_config`; the SDK wants it as a sibling of
`generation_config` directly on `LiveConnectConfig`.

## The exact working config (copy this for Task 14)

```python
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"

def config(target: str = "ru") -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target,
            echo_target_language=False,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
```

Connect with:

```python
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
async with client.aio.live.connect(model=MODEL, config=config()) as session:
    ...
```

This produced zero warnings and zero errors across all 10 rounds.

## Cold-start results

10 rounds, sequential connect → immediately close (`async with` exit) →
reconnect. Full output:

```
round 0: connected in 538 ms
round 1: connected in 479 ms
round 2: connected in 549 ms
round 3: connected in 500 ms
round 4: connected in 477 ms
round 5: connected in 492 ms
round 6: connected in 500 ms
round 7: connected in 506 ms
round 8: connected in 495 ms
round 9: connected in 483 ms

n=10  min=477  median=500  max=549 ms
```

**min = 477 ms, median = 500 ms, max = 549 ms.** Tight spread (~72 ms range)
across all 10 rounds — no long-tail outlier, no evidence of a distinct
"first ever connection" penalty beyond the general ~0.5 s cost. This is a
meaningful cost: at ~500 ms per cold start, both an idle-suspend wake and a
forced mid-speech session rotation would each cost roughly half a second of
either lost audio or added latency unless something is done to hide it
(e.g. pre-warming a replacement session before rotating).

## Surprises

1. **`pyproject.toml` says `google-genai>=1.0.0`, but 2.24.0 is what's
   actually installed and locked.** The plan's hypothesis was written
   against much older REST/SDK documentation than what `uv.lock` resolved
   to. Any future re-lock could, in principle, move further and re-break
   this shape again; the config above should be treated as validated only
   for 2.24.0 unless re-verified.
2. **The deprecated nested shape still silently connects** (with a
   `DeprecationWarning`, not an error). This means a shape mismatch here
   would *not* have failed loudly — it could easily have produced a
   session that quietly ignored `translation_config` while looking
   successful. This was checked directly (in an ad hoc, non-committed
   probe): `LiveConnectConfig(generation_config=GenerationConfig(translation_config=...))`
   connects with a warning, not an exception. It was not carried into the
   committed script because it is the deprecated/wrong shape, but it is
   why field placement was verified against `model_fields`/aliases rather
   than trusting "it connected" as proof of correctness.
3. **`GenerationConfig` itself also has a `translation_config` field**,
   which is presumably there for non-Live (`generateContent`) translation
   use and/or the deprecated pass-through — a second, easy way to
   misplace the field that isn't obviously wrong from the type system
   alone (it type-checks fine either way).
4. Cold start (~500 ms median) is dominated by network/handshake, not by
   anything translation-specific — the spread across 10 rounds was small
   enough that no separate "first connection ever" warm-up cost was
   visible in this run.

---

## Addendum, 2026-09-23: region-qualified language codes are rejected

Found by a real call failing, not by this experiment — and this experiment is
why it was missed.

`target_language_code` accepts only a bare or script-qualified BCP-47 code.
A region subtag is rejected, **but not at connect time**:

| target | 20 blocks | 1 block |
|---|---|---|
| `ru` | OK | — |
| `ru-RU` | **FAIL 1007** | OK |
| `en` | OK | — |
| `en-US` | **FAIL 1007** | OK |

Setup succeeds, the first block or two succeed, and then the server closes the
socket with `1007 Request contains an invalid argument` once it actually uses
the code. A probe that connects and sends a single chunk passes; a real call
dies about a second in.

**Why every experiment missed it.** All five used `"ru"` and `"en"`, copied
from Google's own examples. The CLI takes full BCP-47 (`--their-lang ru-RU`),
because that is what sidetap took and what a user naturally types. So the
experiments validated a config the program never actually sends.

That gap — between what was measured and what ships — is where this class of
bug lives. An experiment that exercises the real code path, rather than a
hand-written config that resembles it, would have caught this before the first
call.

The fix is `live.normalise_language`, which strips a region subtag while
keeping a script one (`zh-Hans` must survive; cutting at the first hyphen would
silently pick the wrong script).
