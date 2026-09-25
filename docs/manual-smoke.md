# Manual smoke checklist

The automated suite is 351 tests that run with no audio hardware, no network
and no credentials. That property is why it is worth having — and it is also
exactly why it cannot answer anything below.

Every fake in that suite agrees with the code by construction.
`FakeVolumeControl` records that `set_volume(42, 0.0)` was called; it cannot
tell you PipeWire accepted it, that node 42 was the duck, or that anything got
quieter. Everything here needs a human, headphones, and a real call.

**Run this before trusting a change to `capture`, `routing`, `playout`,
`interpreter` or the duck.** Record what you observe, not whether it "seemed
fine" — several of the checks below have a measured expectation to compare
against, and a vague note is how a regression survives.

---

## Before the call

**0. `doctor` passes.**

```bash
uv run sidetap-live doctor
```

Every check `OK`, or at worst `WARN` on `speech activity`. A `WARN` there means
`webrtcvad` is missing: idle-suspend never fires and the dead-air alarm stays
quiet. The program still works; it costs more and says less when OUT goes
silent.

**`FAIL live session` is the one that matters most** — it means the key, the
model or the network is wrong, and it fails here in a second rather than two
minutes into a conversation with the PipeWire graph already rewired.

---

## During the call

### 1. The virtual mic is enumerated, and still selected

Start a Zoom (or Viber, Telegram, Discord) call. Open its audio settings.
`sidetap Virtual Mic` should appear and be selectable.

**Then run sidetap as well, and come back.** The selection must still be
`sidetap Virtual Mic`.

*Why this specific check:* this program deliberately shares the virtual mic
with sidetap — same node name, same config file, byte-identical. A messenger's
saved input-device selection is tied to that name. If the two programs used
different devices, you would re-select your microphone in Zoom every time you
switched. If the selection is ever lost, that decision was wrong and the shared
config needs revisiting.

### 2. Ducking actually silences the original

With the default `--duck-level 0.0`, while the remote party is speaking and a
translation is playing: you should hear **no trace** of their own voice.

Then, when the translation stops, their voice should return **within about half
a second** (`DUCK_HOLD_S` is 0.4 s plus one chunk).

*This is the check the fakes cannot reach at all.* The suite proves `wpctl` was
called with `0.0`. Only your ears prove the remote party went quiet.

### 3. The duck does not flap

Listen for the remote party's original being chopped into fragments — a
stutter, a rapid open/shut as translated audio arrives in chunks.

If you hear it, `DUCK_HOLD_S` (0.4 s) is too short for the real chunk cadence.
It is deliberately hysteretic for exactly this reason; the measurement it was
set from is in `docs/experiments/02-voice-stability.md`.

### 4. The duck reopens when the model has nothing to say

Have the remote party go quiet for ten seconds mid-call, without ending the
session.

You should be able to hear them again — breathing, room tone, a cough.

*Why this is not obvious:* the model emits a **continuous** 24 kHz output
stream whether or not it is translating — measured at ~151 s of audio returned
for 154 s of pure silence in. The duck is keyed on audio *energy*, not on bytes
arriving, precisely so this works. If the duck stays shut here, the energy
threshold (`SPEECH_PEAK = 2000`) is wrong and you are inaudible to nobody's
knowledge but your own.

### 5. A rotation is inaudible

Run past ten minutes while talking normally. The session rotates at about
9 minutes, and again roughly every 9 after that.

Record: **could you tell?** And specifically, **did the voice change** at the
seam?

*Expectation from the lab:* experiment 2 confirmed by listening that the voice
survives a rotation, and the make-before-break design exists because
rotate-at-a-pause produced a measured 3.12 s hole. If you hear a gap, the
overlap is not warming the replacement in time. If you hear the *voice change*,
the spec's *Session continuity* decision has to be reopened.

Check the TUI's `rot` figure afterwards. `rot 6` is expected over an hour.
**`rot 6 (5 forced)` is a finding** — forced means the overlap hit its 15 s
bound without the outgoing output ever falling silent, so the join landed
mid-speech. A run that is mostly forced means the join rule needs revisiting.

### 6. Idle-suspend wakes fast enough

Stay silent for a full minute, then speak.

Record **how much of your first word was lost.** Expectation: none. Sessions
close after 45 s of silence and reopen on speech onset, and the 3-second
pre-roll ring replays the onset that triggered the wake. Cold start measured
~500 ms, well inside the ring.

If you lose the first word, the pre-roll is not being replayed.

### 7. A remote party speaking your language falls through

Have them say a full sentence in **your** language.

You should hear **their real voice** — not silence, not a synthetic echo of
what they just said.

*Why it should work with no code doing anything:* IN is configured
`echo_target_language=False`, so input already in the target language produces
no output; no audio flows; the duck opens; you hear them raw. Measured
directly — 96 s of same-language input produced 99.6% digital silence. If you
hear silence instead of their voice, the duck is not reopening.

### 8. Dead air on OUT is noticed

While you are speaking, kill the network (unplug, or disable WiFi).

Within about six seconds (`DEAD_AIR_S`) the OUT pane should show **DEAD AIR**
and an earcon should sound.

*Why OUT specifically:* IN degrades gracefully now — no audio out means the
duck opens and you hear the unmediated call. OUT has no such fallback. Your
real microphone is never linked to the messenger, so a dead OUT direction means
the remote party hears **nothing at all**, and has no way to know. The earcon
exists because during a call you are looking at a person, not a dashboard.

Then restore the network. The direction should recover on its own within a few
seconds — `REOPEN_BACKOFF_S` is 2.0, so it retries at a paced interval rather
than hammering the API.

### 9. The remote party hears something intelligible

The only check that needs a second human.

Ask them, unprompted: **what is the quality like?** Record their own words, not
your interpretation of them. Ask specifically whether names came through — this
program has no equivalent of sidetap's `--phrase` hints, so names are entirely
at the model's mercy. Experiment 5 found eight technical terms survived intact
and consistent, but **personal names are untested.**

### 10. Overlap actually rises — this is the experiment

Deliberately talk over each other for a minute. Interrupt. Finish each other's
sentences. Do not take polite turns.

Then record:

- what `overlap` reads in the TUI subtitle
- what `backlog` and `offset` read in both panes
- **whether the conversation remained followable**

*This is the question the whole project exists to answer.* An interpreter that
forbids interruption has dictated the cadence, whether or not anyone noticed.

Expectation from the lab: `backlog` near zero, `offset` near 0.25 s, both flat
rather than climbing — experiment 4 measured lag holding at 0.24 s → 0.25 s
across 96 s of dense speech, with output running 0.894x input.

**If `backlog` climbs while you overlap, that is the headline finding**, and it
is not a display bug. It means the single-box approach inherits the problem
this project was built to escape.

---

## After the call

**11. The transcript is readable and complete.**

`Ctrl-C`, then open `transcripts/<session>.md`. Both directions, chronological,
fragments joined into paragraphs. Check the tail is there: the `.jsonl` is
flushed per event, so an unclean exit should still leave everything up to that
moment.

Then open `<session>.original.md` and `<session>.translated.md` side by side.
Every block should carry the **same timestamp and the same speaker** in both —
they are one paragraph grouping filtered two ways, so a block present in one
and missing from the other, or shifted against it, is a real finding. Read the
original against your memory of the call: that is the only check that says
whether the transcription itself was right, separately from the translation.

**12. The graph is restored.**

```bash
wpctl status
```

No `sidetap_live_duck` node should remain. The messenger's stream should be
back on your speakers directly.

If a duck survived, something went wrong in `Router.restore()` — recover with:

```bash
uv run sidetap-live doctor --repair
```

which replays the routing journal at `~/.local/state/sidetap_live/`.

---

## Changes this checklist has not yet been run against

Everything below landed from a code review and is verified only against the
fakes in `tests/conftest.py`. The suite cannot reach any of it, which is what
this checklist is for — treat these as unconfirmed until it has been run:

- **Routing defers while the duck has a node but no ports yet.** Check 2 is
  the one that would catch a regression: the call should be audible through
  the duck, not silent.
- **SIGTERM ends the call and restores the graph.** Nothing here covers it.
  Start a call, `kill <pid>` from another terminal, and confirm `pw-link -l`
  shows your messenger back on the speakers and no `sidetap_live_duck`.
- **`b` (bypass) no longer runs `pw-dump`/`pw-link` on the UI thread.** Press
  it mid-call and confirm the dashboard keeps repainting while it takes effect.
- **A replacement that dies mid-rotation no longer wedges the direction**, and
  a replacement that fails to open is retried inside `time_left`. Check 5
  covers the happy path only; both failure paths need a real nine-minute call.
- **The lag cap now fires when the output buffer opens on a pause.** Not
  reachable in an ordinary call — `LAG_CAP_S` is 30 s — so this stays
  unverified in practice.

## Things that are known-untested

Not in scope for this checklist, recorded so nobody assumes otherwise:

- **Personal names.** Experiment 5 covered technical vocabulary only.
- **Your own voice on OUT.** Experiment 5 used a YouTube speaker with a
  comparable accent, not yours. A rerun with your own microphone is a
  five-minute job and the only way to settle it.
- **More than two parties**, and any call with 5.1 audio — port pairing is
  stereo/mono only.
- **A call longer than an hour.** Rotation is measured to work; six consecutive
  rotations are not.
