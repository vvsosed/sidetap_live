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
# ordinary call never reaches it.
LAG_CAP_S = 30.0

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

# Failed opens, with no answering session between them, after which a
# direction is reported dead. A rejected language code or revoked key fails the
# same way every time and the user must be told. Only a refusal, or an open()
# that raises, counts: network and server trouble can clear at any time, and a
# direction reported dead is stopped for the rest of the call.
FATAL_OPEN_FAILURES = 5

# Silence after which a session is closed entirely. Reopening costs a cold
# start, paid only after most of a minute of nothing.
IDLE_SUSPEND_S = 45.0

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
    """The session ended. `reason` is for the log and the health flag.

    `refused` means the server rejected this configuration or credential, so
    an identical retry fails the same way. False covers network and server
    trouble that can clear, and a clean end.
    """

    reason: str
    refused: bool = False


SessionEvent = (
    AudioOut | SourceText | TargetText | GoAway | ResumptionHandle | Closed
)
