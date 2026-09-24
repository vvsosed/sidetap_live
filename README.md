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
uv run sidetap-live doctor
```

Every check should pass, or at worst `WARN` on `speech activity`. The
`live session` check opens and closes one real session against your key, so a
bad key or a withdrawn model fails in a second here rather than two minutes
into a conversation.

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

=== INPUT DEVICES (sources / microphones) ===
  serial=62     USB Microphone Mono

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
| `--mic` | microphone node name or substring; defaults to your default source |
| `--duck-level` | how loud their original stays under the translation. `0.0` (default) replaces it; `0.2` is interpreter-booth mode |
| `--no-echo-out` | when you already speak their language, send nothing rather than synthesised audio |
| `--no-idle-suspend` | keep the session open through long silences (bills continuously) |
| `--lag-cap` | seconds of un-spoken translation before dropping the oldest at a pause. Must be greater than 0 — at or below, every submitted chunk is over the cap and the translation is dropped to the first pause continuously |
| `--out` | transcript directory |
| `--no-tui` | plain console logging, useful over SSH. Reports DEAD AIR and NO AUDIO to the log, so a deaf capture node is visible without the dashboard |

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
  emits a continuous stream whether or not it is translating — measured at
  ~151 s of audio returned for 154 s of pure silence in. Keyed on bytes
  arriving, the duck would close once and never reopen.
- **If they speak your language, you hear their real voice.** No output is
  produced, so no audio flows, so the duck opens. The degenerate case handles
  itself.
- Your voice is translated and *only* the synthesised result reaches the
  messenger, through the virtual microphone.

Unlike a cascade, this does not wait for your sentence to finish. Measured
translation lag held **flat at 0.24 s → 0.25 s** across 96 seconds of
88.5%-density continuous speech, with speech-bearing output running 0.894x
input — so talking without pausing does not push it progressively behind.
Whether that holds with two humans interrupting each other is the thing
`docs/manual-smoke.md` exists to find out.

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
3. Playout on both directions is suppressed.

`m` (mute) stops sending your translated voice without leaving the call.
Pressed while bypassed it changes what you come back to, not what bypass is
doing now.

`f` clears queued audio at a pause in the output. **It cannot cut the audio
already inside `pw-cat`'s buffer**, so expect a short tail.

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
output for most of its length rather than in proportion to speech. Idle-suspend
closes the session after 45 seconds of silence, which removes the cost of a
forgotten session but not of ordinary gaps. A measured figure for a real
two-way call has not been taken.

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
- **This has been built and reviewed by one person, against fakes, experiments
  and short local checks.** It has not been run through a full live call with a
  second human on the other end. See `docs/manual-smoke.md` for what that
  leaves unverified — it is a twelve-item checklist, and every item on it is
  something the 351 automated tests structurally cannot reach.

## More detail

- `docs/experiments/` — the six measurements the design rests on, in five files
  (the sixth is an addendum to `01-connect.md`). Four of them
  contradicted either Google's documentation or the original design.
- `docs/manual-smoke.md` — what only a human with a real call can check.
- `docs/superpowers/specs/` — the design, and what each decision rejected.
- `CLAUDE.md` — architecture and the invariants worth knowing before changing
  `playout.py`, `interpreter.py` or `routing.py`.
