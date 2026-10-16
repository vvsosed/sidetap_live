# Manual smoke checklist

The automated suite is 457 tests that run with no audio hardware, no network
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
uv run sidetap-live doctor --their-lang ru-RU --my-lang en-US
```

Name both languages. Without them the live-session probe opens `en` only,
which proves the key and not the codes you are about to call with.

Every check `OK`, or at worst `WARN` on `speech activity`. A `WARN` there means
`webrtcvad` is missing: session rotation can no longer wait for a gap in the
output and idle-suspend never fires. The program still works; it costs more and
rotates worse.

**`live session` is the weakest check here, not the strongest** — do not read
an `OK` as "the key works". It opens a session per language and closes it, but
`sessions.open()` returns before the socket has connected, so a server-side
refusal — revoked key, exhausted spending cap, a language the model will not
take — arrives afterwards as a `Closed` that nothing in `doctor` reads, and
the check still passes. A `FAIL` here is a local error: no SDK, or a client
that would not construct. The refusal shows up in the call instead, which is
what check 12 stages deliberately.

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
stream whether or not it is translating — a session fed nothing but digital
silence returned ~587 s of audio over 591 s, 99.36% of its lifetime
(experiment 3). The duck is keyed on audio *energy*, not on bytes arriving,
precisely so this works. If the duck stays shut here, the energy threshold
(`SPEECH_PEAK = 2000`) is wrong and you are inaudible to nobody's knowledge
but your own.

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
directly — a same-language run went over 99.5% digital silence by 20 ms frame
(experiment 2). If you hear silence instead of their voice, the duck is not
reopening.

### 8. Dead air on OUT is noticed

While you are speaking, kill the network (unplug, or disable WiFi).

Within about six seconds (`DEAD_AIR_S`) the OUT pane should show **DEAD AIR**.

**Under the TUI that is all it does — the earcon is on the `--no-tui` path
only.** `Session.alarm_dead_air()` is called from `_run_headless` and nowhere
else, so on the dashboard this alarm is silent and you only get it if you are
looking. Worth staging once with `--no-tui` as well, where the beep plays into
your headphones and `DEAD AIR`, the session's own reason, `OUTPUT STALLING`
and `NO AUDIO` all reach the log instead of a pane.

*Why OUT specifically:* IN degrades gracefully now — no audio out means the
duck opens and you hear the unmediated call. OUT has no such fallback. Your
real microphone is never linked to the messenger, so a dead OUT direction means
the remote party hears **nothing at all**, and has no way to know. The earcon
exists because during a call you are looking at a person, not a dashboard —
which is the argument for its reaching the TUI too, and it does not yet.

Then restore the network, and do it promptly. A short outage recovers on its
own — `REOPEN_BACKOFF_S` is 2.0, so it retries at a paced interval rather than
hammering the API — but five consecutive failed opens is `FATAL_OPEN_FAILURES`,
about ten seconds of them at that spacing, and then the direction declares
itself dead, **stops its own pump, and stays down until you restart the run.**
Nothing restores it when the network comes back. That is check 12's path,
reached from here by waiting too long.

Note also what the client is doing while you wait: `websockets` is on its
default 20 s ping interval and 20 s timeout, unconfigured here, so it can take
up to 40 s to notice a dead socket at all. `DEAD AIR` is what you see in the
meantime, and the `rate` figure in check 11 exists because of that gap.

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
across 96 s of dense speech, with *speech-bearing* output running 0.894x
input. That last figure says the translation is not verbose; it is not the one
`backlog` responds to. The queue is fed raw bytes, and that ratio was 1.012 on
the same clip and 1.089x (IN) / 1.123x (OUT) on a real call.

**If `backlog` climbs while you overlap, that is the headline finding**, and it
is not a display bug. It means the single-box approach inherits the problem
this project was built to escape.

One cause of a climbing backlog is now known and fixed, so rule it out before
reading a climb as the headline finding: the model's keep-alive padding used to
be queued as if it were speech, which ratcheted latency to `LAG_CAP_S` within a
minute on any call. Check `pad` in the pane — it should be climbing steadily,
which is the drain working — and `dropped`, which should stay at `0.0s`. A
climb with `dropped` still at zero and `pad` rising is about speech, and is the
real finding. See check 11.

---

### 11. Latency stays flat over a call longer than ten minutes

**This is the check that the automated suite structurally cannot make, and the
one place the worst bug found so far was ever going to show.** Everything else
here is observable in two minutes; this is not.

Let the call run **at least 15 minutes** of ordinary back-and-forth, then
compare the panes against what you noted at the five-minute mark:

- `backlog` should be **the same as it was**, oscillating around 1–3 s, not
  higher. A figure that is larger than it was ten minutes ago is a ratchet, and
  a ratchet is the bug.
- `dropped` should still read `0.0s/0`. The seconds are the lag cap cutting
  translated speech — sentences the other party will never hear, with nothing
  on screen to say which — and the count after the slash is capture blocks
  lost on the way in. Both belong at zero.
- `pad` should be climbing steadily, into the tens or hundreds of seconds by
  this point. That is the drain disposing of the model's keep-alive stream,
  and it is healthy. Do not hold it against a figure, though: the ~1,690 s
  once quoted here came from an offline replay driven at 1.89x inflow, which
  experiment 6 then corrected to 1.089x on IN and 1.123x on OUT — so a real
  call has roughly a tenth of its session length to shed, not twice it. The
  direction of travel is the signal; the size is uncalibrated.
- `rate` should read **1.00x**, and stay there. It is output audio per second
  of wall clock, and a healthy session is remarkably steady — 0.97–1.05x
  across 133 consecutive windows of a 22-minute call. Anything under 0.5x
  lights `OUTPUT STALLING`, which means the session is up and delivering a
  fraction of a stream that is meant to be continuous. Two sessions on
  2026-10-13 did that; both looked healthy on every other indicator, and one
  of them read 0.909x on the whole-call average that hid it. If you see it,
  quit and start again — nothing recovers from it automatically yet, by
  choice, because the threshold has only four probes behind it. An em dash
  rather than a figure is not the alarm: it means there is nothing to measure
  yet, a suspended session or one still inside its first window.
- If the translation goes quiet while `rate` still reads 1.00x, look for
  `SINK STALLED`: the model is delivering and the playout thread is stuck in
  one write to `pw-cat`. `DUCK FAILING` is the other name: the original is
  audible at full volume under the translation. Neither has fired on a real
  call; the 2026-10-15 report that prompted them was bypass's own 30 s
  suspend, now labelled `suspended (bypass)` on the pane. Both are reported
  only. Run with `--probe-audio` and the probe records the playout side once
  a second — backlog, chunks written, duck state, seconds blocked — beside
  the model's chunks.

Then the part only you can judge: **does the translation still sound
continuous?** The drain removes non-speech from the head of the queue, so the
failure mode to listen for is a pause between sentences being clipped short, or
a word losing its opening consonant. If the speech sounds hurried or
run-together rather than merely prompt, say so — `--target-latency` is the
knob, and the trade is latency against breathing room. A pause the drain
touches keeps 100 ms, and anything shorter than 200 ms is never eligible, so
clipped consonants would be a bug rather than a tuning problem.

If `backlog` sits high while one person talks for minutes at a stretch, that is
expected and not this bug: the model generates faster than realtime, so during
an uninterrupted stretch the queue holds real speech and the delay tracks that
lead. `dropped` staying at `0.0s` is what says the difference.

**Then press `b` for a minute, release it, and watch `backlog`.** This is what
broke on the 2026-10-08 call: playout consumed nothing while suppressed but
kept queueing, so bypass filled the buffer to the cap and released ~28 s of
the conversation you had just had *unmediated* — about to be spoken at the
remote party. After releasing bypass, `backlog` should be back near zero
within a second or two and `dropped` should not have moved. Do the same with
`m`, which suppresses OUT only.

**Then hold mute for a full minute and listen to what happens when you release
it.** Past `SUPPRESSED_SUSPEND_S` the OUT session closes, because holding it
open costs about $2.21 an hour for audio discarded on arrival. Two things have
to be true on release, and only an ear can check them:

- **You are audible again promptly.** The session reopens the moment you
  release, not when you next speak, so its ~3 s warm-up should hide inside the
  time you take to start talking. If the remote party loses your first
  sentence, the eager reopen is not working.
- **Nothing you said while muted is transmitted.** Say something distinctive
  while muted, release, then stay quiet and ask the remote party what they
  heard. The pre-roll spans the release boundary and is cleared for exactly
  this reason; if they heard it, that is a serious bug, not a tuning problem.

Watch `session` in the pane go `suspended (mute)` after 30 s muted, and
`suspended (bypass)` under `b`, then back to `running` on release. `rot` must
not climb — a mute is not a rotation.

Afterwards, confirm it in the log rather than trusting memory:

```bash
grep -c "behind" transcripts/<session>.log     # expect 0
grep -c "drained"  transcripts/<session>.log   # expect many, at -v
grep -E "bypass|mute out" transcripts/<session>.log   # when you pressed them
grep -c "closed (output suppressed)" transcripts/<session>.log   # sessions closed
```

A non-zero first count is the regression. The 2026-10-05 call scored 11,000.

### 12. A direction that cannot get a session says why, and gives up

**Stage it deliberately, in its own throwaway run** — do not wait for it to
happen. Start a run with a deliberately wrong key in the environment for that
one command, which overrides the real one without touching
`~/.config/sidetap-live.env`:

```bash
GEMINI_API_KEY=not-a-real-key uv run sidetap-live run \
    --app zoom --their-lang ru-RU --my-lang en-US
```

Talk into the microphone and watch the OUT pane. Three things have to happen,
and only the first is visible to the automated suite:

- **The reason appears in the pane, on its own line, in red.** Not just
  `session ○ suspended` and `DEAD AIR`. On 2026-10-12 a spending cap produced
  exactly this situation and the only line that explained it — the server's
  own message, with the URL that clears it — was in the log file, which under
  the TUI nobody is watching. Check it is legible and that the end of a long
  message wraps rather than vanishing off the right edge.
- **It stops trying.** After five attempts the direction reports itself dead
  and **its pump stops**, so the reconnects end. Watch `session` settle on
  `suspended` and stay there. The call does **not** stop on its own here: with
  no call running, only OUT ever wakes, so the "both directions are dead"
  check never becomes true and you quit with `q` yourself. If the attempt
  counter is still climbing a minute later, the give-up is not arming —
  before this fix it kept going at about 24 a minute for the rest of the run,
  because the flag the fatal set was read by nothing.
- **It does not hammer the API.** Afterwards, count the attempts:

```bash
grep -c "died:" transcripts/<session>.log                      # expect 5
grep -c "could not open a session" transcripts/<session>.log   # expect 0
```

Five attempts at two seconds apart, not hundreds. The failure this guards is
measured: before the fix, a server that accepted every connection and then
dropped it was retried **once every 0.51 s** for as long as the run lasted.

The counts are that way round, and not by accident. `GeminiLiveSession`
starts its thread and returns before it has connected, so **a refusal never
raises out of `sessions.open()`** — it arrives later as a `Closed` on the
receive thread, which is why a bad key is a `died:` line and not a "could not
open a session" one. That second grep has returned zero on every log this
project has ever produced.

---

## After the call

**13. The transcript is readable and complete.**

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

**14. The graph is restored.**

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

- **A stalled playout sink is named within a second.** `SINK STALLED` and
  `DUCK FAILING` have never fired on a real call; the 2026-10-16 call ran
  38 minutes on them without a false alarm. The next time the translation
  goes quiet with `rate` at 1.00x, note what the pane says and what you
  hear — silence, or the original — before toggling anything.
- **Routing defers while the duck has a node but no ports yet.** Check 2 is
  the one that would catch a regression: the call should be audible through
  the duck, not silent.
- **SIGTERM ends the call and restores the graph.** Nothing here covers it.
  Start a call, `kill <pid>` from another terminal, and confirm `pw-link -l`
  shows your messenger back on the speakers and no `sidetap_live_duck`.
- **Neither `b` nor `m` runs session work on the UI thread.** Press `b` and,
  while it is still taking effect, press `m` and then `q`. The dashboard must
  keep repainting and `q` must still quit: both hotkeys contend for
  `Session._lifecycle_lock`, which bypass holds across `pw-dump` and `pw-link`.
- **A hotkey that fails does not end the call.** Hard to stage deliberately;
  if `pw-dump` ever does fail under `b`, the dashboard should stay up with
  `bypass FAILED` in the subtitle rather than exiting to a traceback.
- **A replacement that dies mid-rotation no longer wedges the direction**, and
  a replacement that fails to open is retried inside `time_left`. Check 5
  covers the happy path only; both failure paths need a real nine-minute call.
- **The lag cap now fires when the output buffer opens on a pause.** Not
  reachable in an ordinary call — `LAG_CAP_S` is 30 s — so this stays
  unverified in practice.
- **A lit footer key is legible as lit.** The suite asserts the `-engaged`
  class; only your eyes confirm the CSS renders. Press `m` and look: the key
  must read as clearly on against the unlit keys beside it. `$warning` is the
  footer's own amber, which is why both component-class rules override the
  foreground - if either regresses, the class is still set and every test
  still passes while nothing on screen changes.

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
