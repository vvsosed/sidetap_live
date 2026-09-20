# Experiment 3: does the model send GoAway, and can a session be resumed?

**Verdict: yes to both, and `GoAway` is an instruction rather than a warning.**
A connection lasts ~9 minutes, then `go_away` arrives carrying a 50-second
window. **The client must close within that window; overrunning it is an error,
not a graceful timeout.** Session resumption works, with handles arriving
roughly every two seconds.

This covers spec experiments 1 and 2. Every number below decides part of the
rotation state machine, so they are quoted exactly.

## Environment

- SDK `google-genai` 2.24.0, model `gemini-3.5-live-translate-preview`
- Config shape as established in `01-connect.md`, plus `session_resumption` and
  `context_window_compression` — **both accepted at connect time.**
- Input: pure digital silence, 100 ms blocks of 3200 zero bytes, at wall-clock
  speed, for a target of 720 s.

## Results

| Question | Answer | Evidence |
|---|---|---|
| Does `go_away` arrive? | **Yes** | `[t= 540.5s] go_away: time_left='50s'` |
| How long into the connection? | **540.49 s** (~9 min) | `go_away_elapsed_s: 540.494…` |
| Does it carry a usable window? | **Yes, 50 s** | `go_away_time_left: '50s'` |
| What TYPE is `time_left`? | **`str`**, not a number or duration | `(type=str)` |
| How did the connection end? | **Server abort, code 1008** at t=591.3 s | see below |
| Do resumption handles arrive? | **Yes**, 320 across the run, first at t=4.8 s | ~one every 2 s, all `resumable=True` |
| Does reconnecting with a handle work? | **Yes** | `Phase 2 done: resumption reconnect succeeded` |
| Is `context_window_compression` accepted? | **Yes** | connected without error |

## The 1008 abort is the important part

The connection did not lapse quietly. At t=591.3 s the server closed it:

```
APIError('1008 None. Connection aborted because the client failed to close
the connection after receiving a GoAway signal once the session durat…')
```

540.5 s + 50 s = 590.5 s, and the abort landed at 591.3 s. The window was
honoured to the second.

So `go_away` is not advisory. **The client is required to close and reconnect
within `time_left`, and failing to do so is an error the far end raises.** This
experiment deliberately did not close, in order to find out what happens — the
real implementation must.

## What this fixes in the design

1. **`seconds_of()` must coerce a string.** `time_left` came back as `'50s'`,
   not a float, an int or a duration object. The helper in the plan's Task 14
   already strips a trailing `s` and parses — that was a guess, and it happens
   to be right. Keep it.
2. **The rotation window is 50 seconds.** `DRAINING` may watch for a pause for
   at most that long before it must force a rotation. Against the clip
   analysed for experiment 2 — roughly one pause of ≥ `ROTATE_PAUSE_S` (0.7 s)
   per 48 s of dense monologue — a 50 s window gives about even odds of a clean
   rotation for a relentless speaker, and considerably better odds in real
   two-way conversation where turn-taking gaps are far longer. Forced rotations
   are therefore a real minority case: worth building well, not the norm.
3. **Closing is mandatory, not optional.** `DRAINING` must terminate in a
   close. A state machine that simply opened a replacement and let the old
   connection lapse would take a 1008 on the old socket every single rotation.
4. **Resumption handles are plentiful.** Storing only the most recent one, as
   the design does, is sufficient — a new one arrives about every two seconds,
   so the stored handle is never more than a couple of seconds stale.

## Unplanned finding: output never stops

The receiver logged `unexpected_data_count=2350, unexpected_data_bytes=28200000`
over Phase 1 — **28.2 MB of 24 kHz audio returned in response to 591 s of pure
digital silence.** That is ~587 s of audio for ~591 s of silence: the model
emits a continuous output stream whether or not it has anything to translate.

Experiment 2 quantified the content: when idle, 0.04% of 20 ms frames exceed a
peak of 1000, against 75.5% when actively translating.

This is recorded here because it was found first here, but its consequence
belongs to the playout design and is written up in `02-voice-stability.md` and
amended into the spec: **the duck must trigger on audio energy, not on bytes
arriving**, or it closes on the first chunk and never reopens.

## Caveats

- One run. The 540 s figure is a single observation, not a distribution; treat
  "~9 minutes" as approximate and `time_left` as the authoritative number to
  act on at runtime rather than a timer built from 540.
- Silence input only. Whether `go_away` timing shifts under real speech load is
  unmeasured.
- Phase 2 ran 60 s, long enough to prove a resumed connection works but not to
  prove a resumed connection gets its own full ~9 minutes.
