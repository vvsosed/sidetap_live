"""Value types shared by every layer. Imports nothing but the standard library."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Capture side. pw-record resamples to this, so nothing here does conversion.
TARGET_RATE = 16_000
BLOCK_MS = 100
BLOCK_BYTES = TARGET_RATE * 2 * BLOCK_MS // 1000

# Playout side. Chirp 3 HD streaming synthesis returns LINEAR16 at this rate;
# pw-cat resamples it to whatever the sink wants.
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

# Silence long enough to rotate a session inside. Short enough to occur in
# ordinary conversation within a GoAway window, long enough that a breath
# between clauses does not trigger it. A false pause rotates mid-sentence.
ROTATE_PAUSE_S = 0.7

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

    SUSPENDED and RUNNING are steady states; OPENING and DRAINING are
    transitions that must terminate. DRAINING is bounded by GoAway's
    time_left, OPENING by the connect call itself.
    """

    SUSPENDED = "suspended"
    OPENING = "opening"
    RUNNING = "running"
    DRAINING = "draining"


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
