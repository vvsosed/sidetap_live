"""Value types shared by every layer. Imports nothing but the standard library."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Capture side. pw-record resamples to this, so nothing here does conversion.
TARGET_RATE = 16_000
BLOCK_MS = 100
BLOCK_BYTES = TARGET_RATE * 2 * BLOCK_MS // 1000

# Playout side. gemini-3.5-live-translate-preview returns s16 at this rate;
# pw-cat resamples it to whatever the sink wants. (The rate is right but the
# reason used to name Chirp 3 HD, which is sidetap's TTS and has no successor
# in this program - the model synthesises the speech itself.)
TTS_RATE = 24_000
TTS_BYTES_PER_S = TTS_RATE * 2

# Capture track names. These are what the recorders and queues are keyed on.
REMOTE = "remote"
MIC = "mic"

# Seconds of un-spoken audio past which playout starts dropping at an output
# silence boundary.
#
# A safety valve, not a working limit. Backlog growth is the experimental
# result this whole project exists to measure - see docs/experiments/04 - so
# the cap sits high enough that an ordinary call never reaches it and only a
# runaway does.
LAG_CAP_S = 30.0

# Seconds of continuous speech into a direction with nothing coming out.
DEAD_AIR_S = 6.0

# No audio at all reaching a capture queue for this long. An unlinked capture
# node delivers ZERO BYTES rather than silence, and with no gate in the path
# that now means we simply stop sending - which looks healthy at every stage
# downstream. This watchdog is the only thing that sees it.
NO_AUDIO_S = 15.0

# How long the outgoing and incoming sessions may overlap before the switch is
# forced through without a clean join.
#
# On GoAway the replacement is opened immediately and fed the same audio; the
# switch waits for it to warm up (~3s, measured) AND for the outgoing output
# to fall silent. Output silences are plentiful - 154 in a 99s run - so the
# wait is normally short. This bounds the pathological case, well inside the
# 50s GoAway deadline, because joining mid-word is far better than overrunning
# the deadline and losing the connection outright.
OVERLAP_MAX_S = 15.0

# Minimum gap between attempts to open a session after one failed.
#
# A failed open leaves the direction with nothing to send to, and the next
# speech block would otherwise retry immediately - ten attempts a second
# against an API that just refused us. Long enough to be polite to a rate
# limiter or a revoked key, short enough that a transient network blip costs
# one missed phrase rather than the rest of the call.
REOPEN_BACKOFF_S = 2.0

# Consecutive failed opens after which a direction is called dead.
#
# _open() falls back to SUSPENDED and retries on the next speech onset, which
# is right for a network blip and wrong for a misconfiguration: a rejected
# language code or a revoked key fails identically every time, and without a
# ceiling the direction retries for the whole call while the user is told
# nothing beyond a health marker. At REOPEN_BACKOFF_S apart, this gives a
# transient failure about ten seconds to clear before the call is declared
# one-way.
FATAL_OPEN_FAILURES = 5

# Silence after which a session is closed entirely. Reopening costs the
# cold-start latency measured in docs/experiments/01-connect.md, paid only
# when someone starts talking again after most of a minute of nothing.
IDLE_SUSPEND_S = 45.0

# Playout must be idle this long before the duck reopens. Without the hold it
# flaps in the gaps between output chunks and chops the original into
# fragments, which is heard as the duck failing rather than as hysteresis
# missing.
DUCK_HOLD_S = 0.4

# Seconds of recent capture kept for replay into a freshly opened session.
# Feeds the two non-ideal paths only: waking from suspend, and crossing a
# seam where no pause arrived. The happy path replays nothing.
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
    transitions that must terminate. OVERLAPPING is bounded by OVERLAP_MAX_S
    and, behind that, by GoAway's time_left; OPENING by the connect call.

    OVERLAPPING means two sessions are live and being fed the same audio,
    with the replacement's output discarded until it takes over.
    """

    SUSPENDED = "suspended"
    OPENING = "opening"
    RUNNING = "running"
    OVERLAPPING = "overlapping"


@dataclass(frozen=True)
class TranscriptEvent:
    """One line of transcript.

    Deliberately NOT a source/target pair. inputAudioTranscription and
    outputAudioTranscription arrive as two independently-drifting streams, so
    any pairing would be invented rather than observed - see the spec's
    Transcript section. `kind` is "source" or "target".
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
    """A handle the forced-seam fallback can reconnect with.

    Kept even though rotate-at-a-pause does not normally use it: a handle
    cannot be requested once GoAway has already arrived.
    """

    handle: str


@dataclass(frozen=True)
class Closed:
    """The session ended. `reason` is for the log and the health flag."""

    reason: str


SessionEvent = (
    AudioOut | SourceText | TargetText | GoAway | ResumptionHandle | Closed
)
