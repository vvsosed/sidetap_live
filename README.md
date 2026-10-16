# sidetap_live

Real-time two-way voice interpretation for any call on Linux — Zoom, Viber,
Telegram, Discord, Slack huddles — captured straight from **PipeWire** instead
of integrating with each platform's API. The remote party speaks their
language and you hear yours; you speak yours and they hear theirs. Neither
side installs anything or changes platform.

It runs **one WebSocket per direction** against
`gemini-3.5-live-translate-preview`, a model purpose-built for continuous
speech translation: audio in, translated audio out, no turn-taking.

## Why PipeWire

A PipeWire output port can feed several input ports at once. That means this
can tap the messenger's audio *in addition to* its existing link to your
speakers, and inject translated speech through a virtual microphone the
messenger selects as an ordinary input device. Neither tap needs the
messenger's cooperation or an API integration — the same mechanism works for
every application PipeWire can see.

## Requirements

- Linux with PipeWire >= 0.3.60 (needed for `target.object` and
  `stream.capture.sink`).
- A **`GEMINI_API_KEY`** from [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

  `gemini-3.5-live-translate-preview` is **Gemini Developer API only** — it is
  not on Vertex AI. So there is no `--project`, no ADC, and **no region
  pinning**: whatever latency Google's edge gives you is the latency you get.
- `webrtcvad-wheels`, installed by `uv sync`. Without it the program still
  runs, but every session rotation is forced and idle-suspend never fires — a
  warning from `doctor`, not a failure.

Install PipeWire's CLI utilities if they aren't already present:

```bash
# Arch
sudo pacman -S pipewire pipewire-audio wireplumber

# Debian/Ubuntu
sudo apt install pipewire-bin pipewire-audio wireplumber
```

Then, from the repository root:

```bash
uv sync
```

`uv` is the only supported way to install and run this project — never `pip
install` or activate `.venv` by hand; `uv run` does both, from the lockfile.

## Setup (one time)

This needs a permanent virtual microphone: a runtime-created device gets a
different identity every session, so Zoom and Viber quietly lose the saved
selection and fall back to your real microphone — the remote party then hears
your untranslated voice with nothing on screen saying so. So the device lives
in a config file, installed once:

```bash
uv run sidetap-live doctor --install
systemctl --user restart pipewire pipewire-pulse
```

**The virtual mic is shared with `sidetap` deliberately** — same node name,
same config file, byte-identical. If sidetap already installed it, the command
above correctly reports that the config already exists and leaves it alone.
That is the point: a messenger's saved input-device selection is tied to the
node name, so two separate devices would mean re-selecting your microphone in
Zoom every time you switched programs.

Confirm with:

```bash
uv run sidetap-live doctor --their-lang ru-RU --my-lang en-US
```

Every check should pass, or at worst `WARN` on `speech activity`. Name both
languages: the `live session` check opens and closes one real session per
language named, and `en` alone is what it falls back to.

**That check is weaker than it looks, and an `OK` is not proof the key
works.** `sessions.open()` returns before the socket has connected, so a
refusal from the server — revoked key, exhausted spending cap, a language the
model will not take — arrives afterwards as a close event that `doctor` is no
longer listening for. It fails here only on a local error. The rest of the
checks are exact: the tools, the version, the virtual mic, the key being set
at all.

## Start your call first, then run `sidetap-live devices`

**This is the single most common first-run confusion.** An application does
not appear anywhere in the PipeWire graph until it actually starts an audio
stream — Zoom, for instance, creates one only once the meeting itself starts,
not when the app launches. Running `devices` before that shows no application
at all.

```bash
$ uv run sidetap-live devices

=== OUTPUT DEVICES (sinks) ===
  serial=64     ... Analog Stereo [default]
      node.name = alsa_output.pci-0000_00_1f.3.analog-stereo

=== INPUT DEVICES (sources / microphones) ===
  serial=62     USB Microphone Mono
      node.name = alsa_input.usb-Generic_USB_Microphone-00.mono-fallback

=== APPLICATIONS CURRENTLY PLAYING AUDIO ===
  (none - start your Zoom/Viber call, then run this again)
```

Start the call, then run it again:

```
=== APPLICATIONS CURRENTLY PLAYING AUDIO ===
  serial=2328   zoom.real  binary=zoom  pid=3553
      --app 'zoom'
```

That last line is the flag value to pass to `run`.

## Running a call

```bash
uv run sidetap-live run \
  --app zoom \
  --their-lang ru-RU \
  --my-lang en-US
```

| flag | |
|---|---|
| `--app` | substring of the name/binary shown by `devices` |
| `--their-lang` / `--my-lang` | BCP-47 codes (`ru-RU`, `en-US`, `uk-UA`, …) |
| `--mic` | an exact `node.name`, or a case-insensitive substring of its name or description; defaults to your default source |
| `--duck-level` | how loud their original stays under the translation. `0.0` (default) replaces it; `0.2` is interpreter-booth mode |
| `--no-echo-out` | when you already speak their language, send nothing rather than synthesised audio |
| `--no-idle-suspend` | keep the session open through long silences, and through a bypass or mute (bills continuously — about $2.21 an hour per direction) |
| `--lag-cap` | seconds of un-spoken translation before dropping the oldest at a pause. A safety valve that may cut speech, so reaching it is a loss; playout drains non-speech at 1 s to keep it out of reach. Must be greater than 0 — at or below, every submitted chunk is over the cap and the translation is dropped to the first pause continuously |
| `--target-latency` | queue depth above which the model's keep-alive padding is discarded (default 1.0). Queue depth is the delay you hear. Lower it for a shorter delay; raise it if pauses between sentences sound clipped. Must be below `--lag-cap` |
| `--probe-audio` | diagnostic: record every output chunk's per-frame energy to `<session>.audio-probe.jsonl`, with the model's own signal for when it is speaking. Analyse with `scripts/analyse_audio_probe.py`. Energies and timings only, no audio or text |
| `--out` | transcript directory (default `transcripts/`) |
| `--latency` | PipeWire stream latency, default `100ms`. The capture block size is fixed at 100 ms regardless |
| `--no-tui` | plain console logging, useful over SSH. Reports DEAD AIR, NO AUDIO, OUTPUT STALLING, SINK STALLED, DUCK FAILING and the server's own reason for refusing a session to the log, so none of them needs the dashboard — and it is the only path that sounds the dead-air earcon |
| `-v` | debug logging, including every drain of keep-alive padding |

**The source language is not configured, and cannot be.** The model
auto-detects it; only the target is settable. That is why the flags are
crossed: IN translates into `--my-lang`, OUT into `--their-lang`.

### What you hear, what they hear

Routing is **full replacement**, not an overlay: you never hear the remote
party's raw voice at the same time as the translation, and they never hear
your raw voice at all.

- While a translation is playing, their original is ducked (via a loopback
  node this program owns, `wpctl set-volume`d to `--duck-level`). When the
  translation stops, their voice returns within about half a second.
- **The duck follows audio *energy*, not the presence of data.** The model
  emits a continuous stream whether or not it is translating — a session fed
  nothing but digital silence returned ~587 s of audio over 591 s, 99.36% of
  its lifetime (`docs/experiments/03-session-limits.md`). Keyed on bytes
  arriving, the duck would close once and never reopen.
- **If they speak your language, you hear their real voice.** No output is
  produced, so no audio flows, so the duck opens. The degenerate case handles
  itself.
- Your voice is translated and *only* the synthesised result reaches the
  messenger, through the virtual microphone.

Unlike a cascade, this does not wait for your sentence to finish. Measured
translation lag held **flat at 0.24 s → 0.25 s** across 96 seconds of
88.5%-density continuous speech, with speech-bearing output running 0.894x
input — the translation takes less time to say than the original did, so
talking without pausing does not force the "speak, pause, let it interpret"
cadence a cascade imposes.

That is not the same as a constant delay, and the distinction cost a real
call. What you hear is the depth of the playout queue, and the queue is fed
raw bytes rather than speech: the model holds its audio channel open
continuously and delivers 1.089x realtime on IN and 1.123x on OUT, with
bursts at 2x. Discarding the padding is what holds the delay down
(`--target-latency`), and it bounds the padding only — through one long
uninterrupted stretch the model runs ahead, the queue holds real speech, and
the delay tracks that lead instead. Whether all of this holds with two humans
interrupting each other is the thing `docs/manual-smoke.md` exists to find
out.

### Sessions rotate about every nine minutes

A Live API connection lives ~10 minutes. `GoAway` arrives around 540 s with a
50-second window, and **closing inside it is mandatory** — the server aborts
otherwise.

Rotation is **make-before-break**: the replacement opens immediately, is fed
the same audio while its output is discarded, and takes over once it is warm
and the outgoing session's output has fallen silent. Serial rotation was
measured to produce a 3.12 s hole, because a fresh session emits nothing for
about three seconds.

The TUI's `rot` figure shows the count and how many were forced. `rot 6` over
an hour is expected; `rot 6 (5 forced)` means the join rule needs revisiting.

**`backlog`, `pad` and `dropped` are three different things.** `backlog` is how
far behind the translation is, and it is the number that should stay flat —
1–3 s, not climbing over the call. `pad` is the model's keep-alive padding
being discarded to hold that: the model never closes its audio channel, so
inflow runs over realtime — 1.089x on IN and 1.123x on OUT, measured on a real
call — and what it sends between utterances is exact digital silence, 21.2% of
the IN stream and 48.0% of OUT. So `pad` in the hundreds of seconds is the
drain working, not a fault. `dropped` is the lag cap cutting **translated
speech**, and it should read `0.0s` for the whole call — anything else means
sentences the other party never heard. The count after the slash is capture
blocks lost on the way in, and belongs at zero too. See
`docs/experiments/06-padding-separability.md`, which corrects the 1.89x figure
in `04-pacing.md`.

**`rate` is the one alarm with no other symptom.** It is output audio per
second of wall clock, and because the model's stream is continuous a healthy
session is a rock — 0.97–1.05x across 133 consecutive windows of a 22-minute
call. Under 0.5x the pane lights `OUTPUT STALLING`: the session is up, the
transcription is still arriving, and a fraction of the audio is. Nothing
recovers from that automatically; quit and start again. `SINK STALLED` is the
opposite case — the model is delivering and the playout thread has been stuck
inside one write to `pw-cat` for over a second. `DUCK FAILING` means `wpctl`
cannot set the duck's volume, so the original is playing at full volume under
the translation. Neither has fired on a real call. And when a direction
cannot keep a session at all, the server's own words — which name the cause
and sometimes carry the URL that clears it — are printed in red in that
direction's pane, because under the TUI the log is a file nobody is watching.

### Hotkeys

| Key | Action |
|---|---|
| `b` | Bypass |
| `m` | Mute out |
| `f` | Drop backlog |
| `q` | Quit |

**Bypass (`b`) has three effects, and all three matter** — describing only one
is how someone ends up with translated speech talking over the unmediated
conversation it was meant to replace:

1. The duck opens and stays open: you hear the remote party's own voice.
2. Your real microphone is linked straight into the virtual mic: they hear
   your own voice, untranslated.
3. Playout on both directions is suppressed, and after 30 s the sessions
   close: the pane reads `suspended (bypass)`, or `suspended (mute)` under
   `m`, until you release it.

`m` (mute) stops sending your translated voice without leaving the call.
Pressed while bypassed it changes what you come back to, not what bypass is
doing now.

**A lit key means you pressed it, not that its effect happens to be in force.**
Bypass suppresses OUT too, but it does not light `Mute out` - otherwise `m`
under bypass would change nothing on screen and you could not tell which way
you had just toggled it.

`b` and `m` both hand their work to a thread, because both take the same
lock and bypass holds it across `pw-dump` and `pw-link`. A hotkey that fails
leaves the dashboard up and says so in the subtitle.

`f` throws away everything queued but not yet handed to the sink, immediately
and on both directions. **It cannot cut the audio already inside `pw-cat`'s
buffer**, so expect a short tail.

## Output

Ctrl-C writes four files into `transcripts/`, all sharing one session stem:

| file | |
|---|---|
| `<session>.jsonl` | every fragment exactly as it arrived |
| `<session>.md` | both directions and both streams, interleaved chronologically |
| `<session>.original.md` | only what was actually said |
| `<session>.translated.md` | only what each side heard |

**The two split files are not monolingual, and cannot be.** There are two
directions, so the originals are their language *and* yours, and the
translations likewise — the `**Them**` / `**You**` labels are what carries
that. Paragraphs are grouped once and then filtered by stream, so all three
Markdown files share block boundaries and timestamps: `.original.md` and
`.translated.md` line up block for block when read side by side.

A fifth file, `<session>.log`, shares the stem and is written as the call
runs — that is the one to grep afterwards for a rotation, a bypass, or a
session the server refused. `--probe-audio` adds a sixth,
`<session>.audio-probe.jsonl`.

The `.jsonl` is flushed per event, so an unclean exit still leaves everything
up to that moment on disk. The Markdown is rendered at close, and nothing
regenerates it from the `.jsonl` — so a `kill -9` leaves the events but not
the readable files.

The two transcription streams are recorded as **independent timestamped
events, not source/target pairs.** The model emits no turn boundary at all —
`finished=True` never fired once across a full measured run — so the streams
drift relative to each other and any pairing would be invented rather than
observed. The Markdown joins consecutive fragments into paragraphs for
readability; the JSONL keeps every fragment exactly as it arrived.

## Cost

Audio in is $3.50 per million tokens, out $21.00, at 25 tokens per second. The
figure in the TUI is a **spend estimate to catch a runaway session, not an
invoice** — though the quantity is exact here, since this process counts every
byte it sends and receives, so only the published rate can drift.

**Output billing does not stop during conversational pauses.** The model
streams output continuously while a session is open, so an ordinary call bills
output for most of its length rather than in proportion to speech — about
**$2.21 an hour per direction** at these rates. Idle-suspend closes the
session after 45 seconds of silence, which removes the cost of a forgotten
session but not of ordinary gaps.

It keys on **input** silence, so it never fires while you are muted and still
talking: a ~30 minute mute on 2026-10-09 bought 2,129 s of OUT audio that was
discarded on arrival, about **$1.12**. So `--idle-suspend` governs two timers
rather than one — 45 seconds of input silence, and 30 seconds of bypass or
mute, the second with a grace period so a short bypass does not churn a
session that emits nothing for its first ~3 s. A measured figure for a whole
real two-way call has not been taken.

## Known limitations

Real, current limitations — not aspirational TODOs.

- **No region pinning.** The model is Developer API only, so there is no
  equivalent of pinning to a nearby region. Cold start measured 477–549 ms
  from central Europe.
- **Sessions rotate about every nine minutes.** The voice was confirmed by
  listening to survive a rotation, but the rotated audio carried audible
  defects that the unrotated run did not — roughly triple the mid-speech
  dropout rate, with a cluster of discontinuities local to the seam.
- **No `--phrase` equivalent.** sidetap boosts recognition of names and jargon
  through Speech-to-Text phrase hints; this model exposes nothing comparable.
  Eight technical terms were measured to survive intact and consistently, but
  **personal names are untested** and entirely at the model's mercy.
- **The source language cannot be specified.** Detection was measured clean on
  Slavic-accented English, but using a YouTube speaker rather than the author's
  own voice — so the mechanism is confirmed and the specific case is not.
- **It is a preview model** and may change or be withdrawn.
- **The model drops audio mid-speech occasionally**, independently of
  rotation — three times in 98 seconds in a measured run with no seam in it.
- **This has been built and reviewed by one person**, against fakes,
  experiments and the author's own calls. Real two-way calls are on record —
  the padding and delivery-rate measurements in
  `docs/experiments/06-padding-separability.md` come from 30- and 40-minute
  ru↔en Teams calls, and three of this program's worst bugs passed every
  offline test and showed up only on one. What those calls did not cover is
  `docs/manual-smoke.md`, worked through as a checklist: fifteen items, every
  one of them something the 457 automated tests structurally cannot reach, and
  a list at its end of changes no run has exercised yet.

## More detail

- `docs/experiments/` — the seven measurements the design rests on, in six
  files (the seventh is an addendum to `01-connect.md`, because a real call
  found it rather than an experiment). Five of them contradicted either
  Google's documentation, the original design, or an earlier experiment:
  experiment 6 corrects 2 and 4, both of which were calibrated on sessions
  with nothing to translate. Prefer a number taken from a real call over one
  taken from a fed clip.
- `docs/manual-smoke.md` — what only a human with a real call can check.
- `docs/superpowers/specs/` — the design, and what each decision rejected.
- `CLAUDE.md` — architecture and the invariants worth knowing before changing
  `playout.py`, `interpreter.py` or `routing.py`.

## License

MIT — see [`LICENSE`](LICENSE).
