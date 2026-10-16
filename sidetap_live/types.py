"""Value types shared by every layer. Imports nothing but the standard library."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Capture side. pw-record resamples to this, so nothing here does conversion.
TARGET_RATE = 16_000
BLOCK_MS = 100
BLOCK_BYTES = TARGET_RATE * 2 * BLOCK_MS // 1000

# Playout side. The model returns s16 at this rate; pw-cat resamples it for
# the sink.
TTS_RATE = 24_000
TTS_BYTES_PER_S = TTS_RATE * 2

# Capture track names. These are what the recorders and queues are keyed on.
REMOTE = "remote"
MIC = "mic"

# Seconds of un-spoken audio past which playout drops at an output silence
# boundary. A safety valve for a runaway backlog, set high enough that an
# ordinary call never reaches it. It may cut speech, so reaching it is a
# quality loss; TARGET_LATENCY_S exists to keep it out of reach.
LAG_CAP_S = 30.0

# Queue depth above which playout discards NON-SPEECH from the head. Depth is
# not a statistic, it is the delay you hear: the model holds its audio channel
# open continuously, so inflow is padding plus speech and runs over realtime -
# 1.089x on IN, 1.123x on OUT, bursts at 2x, measured in
# docs/experiments/06-padding-separability.md (the 1.89x once claimed here was
# inferred from lag-cap discards the suppression bug caused). Undrained, depth
# ratchets to LAG_CAP_S inside the first minute and stays pinned, the cap
# discarding real audio to hold the line - 89% of the IN stream on the
# 31.7 min call in docs/experiments/04-pacing.md. Low enough to stay near its
# 0.2-0.3 s event lag, high enough to leave short pauses alone: one the drain
# does touch keeps KEEP_PAUSE_MS, and one shorter than MIN_DRAIN_RUN_MS is
# never eligible, being indistinguishable from a dip inside a word.
#
# This bounds the padding, not the model's generation lead: through one long
# uninterrupted stretch the queue holds real speech and latency tracks that
# lead instead. Draining cannot help - see docs/experiments/04-pacing.md.
TARGET_LATENCY_S = 1.0

# Seconds of continuous speech into a direction with nothing coming out.
DEAD_AIR_S = 6.0

# No audio at all reaching a capture queue for this long. An unlinked capture
# node delivers zero bytes rather than silence, which looks healthy at every
# stage downstream; this watchdog is the only thing that sees it.
NO_AUDIO_S = 15.0

# How long two sessions may overlap before the switch is forced without a
# clean join. The switch normally waits for the replacement to warm up (~3 s)
# and for the outgoing output to fall silent, which is quick because output
# silences are frequent. The cap sits well inside GoAway's 50 s: joining
# mid-word beats overrunning the deadline and losing the connection.
OVERLAP_MAX_S = 15.0

# Minimum gap between open attempts after a failure. Without it every speech
# block retries, ten a second against an API that just refused us. Long enough
# to respect a rate limiter, short enough that a network blip costs one phrase.
REOPEN_BACKOFF_S = 2.0

# Consecutive failed opens after which a direction is reported dead. Retrying
# suits a network blip, but a rejected language code or revoked key fails the
# same way every time and the user must be told. At REOPEN_BACKOFF_S apart this
# gives a transient failure about ten seconds to clear.
FATAL_OPEN_FAILURES = 5

# How long a session must survive before its open counts as having worked. The
# socket coming up proves nothing: on 2026-10-12 a spending cap made the server
# accept every connection and close it inside a second, and _open()'s success
# path reset the give-up accounting every time - so neither REOPEN_BACKOFF_S nor
# FATAL_OPEN_FAILURES could engage, measured at one reconnect every 0.51 s for
# minutes. A healthy session lives ~540 s, to GoAway.
SESSION_PROBATION_S = 10.0

# Window over which output delivery is judged, and the fraction of realtime
# below which it is reported. The model's output stream is CONTINUOUS - it
# emits padding whether or not it is translating - so a healthy session is a
# rock: 1.001x over 22.3 min, inside 0.97-1.05x in all 133 of its windows,
# plus 1.021x on a short probe. A thin stream means half-dead rather than
# quiet: the degraded session of 2026-10-13 ran 0.282x with 0.4% exact digital
# silence where the healthy probes carry 11.9-31.3%, i.e. the keep-alive
# stream had stopped. 0.5 is clear of both sides. Judged per window, never
# over a whole call: that morning's other bad session reads 0.909x overall and
# swings 0.20x-2.08x, because averaging over a stall hides the stall. The
# window tumbles rather than slides, so a degradation surfaces within 2x it;
# that is cheap and this only reports.
STARVED_WINDOW_S = 10.0
STARVED_RATIO = 0.5

# Seconds the playout thread may sit inside one sink write before it is
# reported stalled. pw-cat blocks that write ~20 ms a chunk once its 16 KiB
# pipe is full, so a full second means nothing is draining it. Never seen on
# a real call: the longest on 2026-10-16 was 0.14 s.
SINK_STALL_S = 1.0

# Silence after which a session is closed entirely. Reopening costs a cold
# start, paid only after most of a minute of nothing.
IDLE_SUSPEND_S = 45.0

# Seconds a direction's output must go unwanted - bypass or mute - before its
# session is closed. Holding one open costs $0.0006 a second ($3.50/M in,
# $21.00/M out, 25 tokens a second): $2.21 an hour per direction for audio
# discarded on arrival. The 30 min mute on the 2026-10-09 call bought 2,129 s
# of it, about $1.12; IDLE_SUSPEND_S cannot catch that, keying on input
# silence while you are still talking. The grace period is what stops a short
# bypass churning the session: a fresh one emits nothing for ~3 s, so closing
# around a 3 s bypass trades dead air on return for a fifth of a cent. Four
# bypasses on that call, two of them under 10 s.
SUPPRESSED_SUSPEND_S = 30.0

# Playout must be idle this long before the duck reopens. Without the hold it
# flaps in the gaps between output chunks and chops the original into
# fragments.
DUCK_HOLD_S = 0.4

# Seconds of recent capture replayed into a freshly opened session, on wake
# from suspend or after a session died.
PREROLL_S = 3.0


class Direction(StrEnum):
    """Which way a translation flows.

    IN is them -> you, landing on your headphones.
    OUT is you -> them, landing in the virtual mic's sink.
    """

    IN = "in"
    OUT = "out"

    @property
    def track(self) -> str:
        """The capture track this direction consumes."""
        return REMOTE if self is Direction.IN else MIC

    @property
    def opposite(self) -> Direction:
        return Direction.OUT if self is Direction.IN else Direction.IN


@dataclass(frozen=True)
class AudioChunk:
    track: str
    pcm: bytes
    t_start: float


class SessionState(StrEnum):
    """Where one direction's Live session is in its lifecycle.

    SUSPENDED and RUNNING are steady states; OPENING and OVERLAPPING are
    transitions that must terminate - OVERLAPPING bounded by OVERLAP_MAX_S and
    behind that by GoAway's time_left, OPENING by the connect call.

    OVERLAPPING means two sessions are live and fed the same audio, the
    replacement's output discarded until it takes over.
    """

    SUSPENDED = "suspended"
    OPENING = "opening"
    RUNNING = "running"
    OVERLAPPING = "overlapping"


@dataclass(frozen=True)
class TranscriptEvent:
    """One line of transcript. `kind` is "source" or "target".

    Not a source/target pair: the two transcriptions arrive as independently
    drifting streams, so any pairing would be invented.
    """

    t: float
    direction: Direction
    kind: str
    text: str


@dataclass(frozen=True)
class AudioOut:
    """Translated audio, 24 kHz s16 mono, straight from the model."""

    pcm: bytes


@dataclass(frozen=True)
class SourceText:
    """A fragment of inputAudioTranscription - what the speaker said."""

    text: str


@dataclass(frozen=True)
class TargetText:
    """A fragment of outputAudioTranscription - what was spoken back."""

    text: str


@dataclass(frozen=True)
class GoAway:
    """The connection will end in `time_left_s`. The window to rotate in."""

    time_left_s: float


@dataclass(frozen=True)
class ResumptionHandle:
    """A handle to resume from if the live session dies.

    Kept continuously, because one cannot be requested after GoAway.
    """

    handle: str


@dataclass(frozen=True)
class Closed:
    """The session ended. `reason` is for the log and the health flag."""

    reason: str


SessionEvent = (
    AudioOut | SourceText | TargetText | GoAway | ResumptionHandle | Closed
)
