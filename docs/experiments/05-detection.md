# Experiment 5: is accented English detected as English?

**Verdict: YES, cleanly. Across the full 64.0 s clip of Slavic-accented
English, `input_transcription` came back as coherent, grammatically correct
English throughout — no Cyrillic, no transliterated nonsense, no stretch
misdetected as another language. `output_transcription` produced matching
Russian for the entire clip and real speech-bearing audio flowed
continuously (73.9% of 20 ms output frames cleared the speech threshold,
51.70 s total). The specific risk this experiment was built to catch — the
model card's warning that "language detection can struggle with non-native
accents" causing the OUT direction to mistranslate or go silent — did not
materialize on this clip.** See *Caveat* below: this is not proof it never
will, because the clip is not this user's own voice.

## Setup

- SDK: `google-genai` 2.24.0, model `gemini-3.5-live-translate-preview`
  (unchanged from experiments 1–4)
- Config shape unchanged from `docs/experiments/01-connect.md`:
  `translation_config`, `input_audio_transcription`,
  `output_audio_transcription` all top-level on `LiveConnectConfig`,
  `echo_target_language=False` — the established shape, used as given rather
  than re-derived. (Real production OUT uses `echo_target_language=True` per
  `docs/superpowers/specs/2026-09-20-sidetap-live-design.md`; that setting
  governs what happens when the model judges source == target, not detection
  accuracy for a genuinely different source language, so it does not affect
  what this experiment measures.)
- **`target_language_code="ru"`** — not an illustrative default, but the real
  OUT-direction configuration for this user (English speaker, Russian-
  speaking counterpart)
- Clip: `tests/fixtures/accented_en_16k.raw`, 64.0 s, raw s16 16 kHz mono,
  peak 22074 (67% FS), 93% speech density, 2 pauses ≥0.4 s. English spoken
  with a Slavic (Russian/Ukrainian) accent, captured from a YouTube talk via
  sink-monitor capture. **Not** `tests/fixtures/speech_en_16k.raw`, which
  despite its name holds Russian speech (see experiment 2) and was not used
  here.
- Fed at wall-clock speed (100 ms blocks of 3200 bytes,
  `await asyncio.sleep(0.1)` per block), one continuous session, no
  rotation. Tail: session kept open 8.0 s past the last input block.
- Connected in 531 ms. No warnings, no exceptions, no dropped connection —
  a clean run.

## Question 1: is the source detected as English?

Yes, for the entire clip. `input_transcription` never produced anything but
English, from the first event at t=3.5 s to the last at t=64.6 s (62 delta
events, concatenated below). `finished=True` never fired on either stream —
the model treated the whole 64 s as one continuous, still-open turn, the
same behavior experiment 4 documented for dense continuous narration without
long enough pauses to close a turn boundary. The reconstructed input
transcript:

> "The video was performed by trained personnel in a controlled professional
> controlled laboratory equipped with specialized safety infrastructure. We
> are going to replicate a procedure from an Indian research paper, even
> though the paper contains a few glaring errors, the general methodology
> turned out to be completely reproducible in my hands. The plan is to
> nitrate ammonium sulfamate with a nitrating acid mixture at minus 40
> degrees Celsius and then isolate the resulting dinitramide as a sparingly
> soluble salt using guanidinium urea. This is the most modern approach.
> It's actually how it's produced industrially today. Since I didn't have
> any guanidinium urea on hand, we need to synthesize that first. It's a
> straightforward hydrolysis of dicyandiamide with sulfuric acid. We take
> dicyandiamide and dilute sulfuric acid in a two to one molar ratio, mix
> them with enough water and heat it up. For the best yield, you need to
> hold the temperature between 70 degrees Celsius and 80 degrees Celsius for
> three to four hours. I cooled the mixture and left it overnight. A large
> crop of guanidinium sulfate crystals precipitated out, which I filtered
> and dried. Now for the main event,"

This is fluent, grammatical English with correct sentence structure and
punctuation throughout — not a marginal or partial detection. The one
transcription blemish visible is a duplicated word ("a controlled
professional **controlled** laboratory"), which reads like an ASR repeat
glitch rather than a language-detection problem (the surrounding English is
otherwise clean and the duplication doesn't change language).

Indirect corroboration: `echo_target_language=False` suppresses spoken
output when the model judges the input is already in the target language
(established in experiment 2 — a same-language run went 99.5%+ silent with
`output_transcription` never populating at all). Here, output speech ran
essentially continuously for the full 64 s (see Question 3) — if detection
had ever flipped to "this is already Russian," that would show up as a gap
in spoken output, and none appeared.

## Question 2: how badly are proper nouns and technical terms mangled?

This clip has no personal or place names to test — it's chemistry-heavy
narration (apparently synthesis notes) rather than a talk built around named
entities, so the "proper noun" question ends up tested through unusual
technical vocabulary instead, which is arguably a harder case than an
ordinary name. Every technical term checked came through cleanly, on both
sides of the pipe, and stayed **consistent** across repeated mentions:

| term (as spoken) | input_transcription | output_transcription | verdict |
|---|---|---|---|
| ammonium sulfamate | "nitrate ammonium sulfamate" | "нитровании сульфамата аммония" | correct |
| dinitramide | "the resulting dinitramide" | "полученный динитрамид" | correct |
| guanidinium urea (3 mentions) | "guanidinium urea" (x3, consistent spelling) | "гуанидиния мочевины" (x3, consistent) | correct, consistent across repeats |
| dicyandiamide (2 mentions) | "dicyandiamide" (x2) | "дициандиамида" (x2) | correct, consistent across repeats |
| guanidinium sulfate | "guanidinium sulfate crystals" | "сульфата гуанидиния" | correct |
| Celsius (3 mentions, with numbers) | "minus 40 degrees Celsius", "70 degrees Celsius and 80 degrees Celsius" | "минус 40 градусах Цельсия", "70 и 80 градусами Цельсия" | correct, numbers preserved exactly |
| Indian (research paper) | "an Indian research paper" | "индийской исследовательской работы" | correct |
| two to one molar ratio | "a two to one molar ratio" | "молярном соотношении два к одному" | correct |

No mangling was observed on this run — no term was dropped, phonetically
garbled, mistranslated, or rendered inconsistently between its repeated
occurrences. This is a real, if narrow, data point *against* the concern
motivating this question: even without an equivalent of sidetap's
`--phrase` hints, uncommon multisyllabic technical vocabulary survived
intact here. It does not generalize to personal names (untested by this
clip) or to a broader vocabulary sample, and one run of one speaker's
vocabulary is not strong evidence either way — but it is evidence, and it
points the opposite direction from what the question was framed to expect.

## Question 3: does the translation actually happen?

Yes, clearly, on both signals:

- **`output_transcription` produced fluent Russian for the entire clip**,
  sentence-for-sentence matching the English input turn described above (62
  delta events, t=3.7 s to t=64.9 s), reconstructed:

  > "Видео было выполнено обученным персоналом в контролируемой
  > профессиональной лаборатории, оснащенной специализированной
  > инфраструктурой безопасности. Мы собираемся воспроизвести процедуру из
  > индийской исследовательской работы, хотя в ней есть несколько вопиющих
  > ошибок, общая методология оказалась полностью воспроизводимой в моих
  > руках. План состоит в нитровании сульфамата аммония нитрующей кислотной
  > смесью при минус 40 градусах Цельсия, а затем выделить полученный
  > динитрамид в виде труднорастворимой соли с использованием гуанидиния
  > мочевины. Это самый современный подход. Именно так его производят в
  > промышленности сегодня. Поскольку у меня не было гуанидиния мочевины,
  > нам нужно сначала синтезировать его. Это простая гидролиз дициандиамида
  > с серной кислотой. Мы берем дициандиамид и разбавленную серную кислоту в
  > молярном соотношении два к одному, смешиваем их с достаточным
  > количеством воды и нагреваем. Для лучшего выхода нужно поддерживать
  > температуру между 70 и 80 градусами Цельсия в течение трех-четырех
  > часов. Я охладил смесь и оставил ее на ночь. Большой урожай сульфата
  > гуанидиния выпал в осадок, который я отфильтровал и высушил. Теперь к
  > главному событию, нитрамид"

- **Real speech-bearing audio flowed**, not just the near-silent
  connection-priming blip experiment 2 documented for "nothing to say"
  output. Using that experiment's peak>1000-per-20ms-frame speech test:
  2585 of 3500 output frames (73.9%) were speech-bearing, totaling **51.70 s**
  of actual spoken audio. First speech-bearing frame at t=3.76 s (matching
  `output_transcription`'s first event at t=3.7 s almost exactly), last at
  t=65.66 s (just after the last input block was sent at t=65.0 s, i.e. the
  model finished its trailing sentence and stopped, not a silent channel).
  Total raw output bytes were 3,360,000 (70.00 s of 24 kHz audio) — the
  usual near-continuous byte stream from experiment 2 — but the frame-energy
  measurement is the one that actually shows speech, and 73.9% is far above
  experiment 2's ~0.0–0.5% "nothing to say" baseline and in the same range
  as its ~74–89% "actively translating" baseline. Output audio was saved to
  `docs/experiments/audio/exp05_output_ru.raw` (gitignored, not committed)
  for anyone who wants to listen.

No silent stretch, no dropout to zero output, no sign the model ever judged
the input already-in-target and suppressed output.

## Caveat: this is not the user's own voice

`accented_en_16k.raw` is **not a recording of this project's actual user**.
It is a YouTube speaker with a comparably Slavic-accented English delivery,
captured via sink-monitor recording because a microphone was unavailable
when this fixture was made. This experiment therefore tests the
*mechanism* — does Gemini's auto-detector correctly identify Slavic-accented
English as English, under real OUT-direction config — but not the *specific
case* that matters most: this particular user's voice, microphone chain,
and speaking rate. The result above should be read as "the mechanism did not
fail on one representative accented-English sample," not "this will never
fail for this user." A follow-up with the user's own voice, once a
microphone is available, is a five-minute job: swap the clip path in
`scripts/exp05_detection.py` for a fresh recording and rerun.

## Files

- `scripts/exp05_detection.py` — the experiment
- Raw run log (not committed): `/tmp/exp05.log`
- Output audio (not committed, gitignored):
  `docs/experiments/audio/exp05_output_ru.raw`
