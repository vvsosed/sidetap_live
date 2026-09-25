"""Speech activity observation. Never removes a byte.

This is NOT a gate: it only reports whether speech is present, because a
model that reasons over continuous audio must receive continuous audio. It
drives idle-suspend, wake on speech onset, and the offset, dead-air and
overlap metrics.

Not called vad.py, because that name invites reinstating gating.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .ports import Clock
from .types import TARGET_RATE, Direction

log = logging.getLogger(__name__)

# webrtcvad accepts 10, 20 or 30 ms frames only.
FRAME_MS = 20
FRAME_BYTES = TARGET_RATE * 2 * FRAME_MS // 1000

SpeechDetector = Callable[[bytes], bool]


def webrtc_detector(aggressiveness: int = 2) -> SpeechDetector | None:
    """Real detector, or None when webrtcvad is unavailable.

    `aggressiveness` runs 0-3, higher filtering more non-speech. The value
    is not tuned, and need not be: a false positive keeps a session open a
    little longer, and a false negative delays a wake by one block. Rotation
    does not use it; it joins at a gap in the output, detected by energy.

    The returned closure is stateful (webrtcvad adapts to the noise floor),
    so give each track its own detector.
    """
    try:
        import webrtcvad
    except ImportError:
        log.warning(
            "webrtcvad not installed - session rotation falls back to a timer "
            "and idle-suspend is disabled. Run: uv sync"
        )
        return None

    vad = webrtcvad.Vad(aggressiveness)

    def detect(pcm: bytes) -> bool:
        return any(
            vad.is_speech(pcm[i : i + FRAME_BYTES], TARGET_RATE)
            for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)
        )

    return detect


class SpeechActivity:
    """Reports whether speech is happening. Returns no audio, ever."""

    def __init__(self, detector: SpeechDetector | None, clock: Clock):
        self._detect = detector
        self._clock = clock
        self._speaking = False
        # Seeded at construction, so that nobody having spoken yet still
        # counts as silence for idle-suspend.
        self._last_speech = clock.monotonic()

    @property
    def available(self) -> bool:
        return self._detect is not None

    @property
    def speaking(self) -> bool:
        return self._speaking

    def observe(self, pcm: bytes) -> bool:
        """Note whether this block carries speech. Returns that, nothing else."""
        if self._detect is None:
            return False
        self._speaking = self._detect(pcm)
        if self._speaking:
            self._last_speech = self._clock.monotonic()
        return self._speaking

    def silence_s(self) -> float:
        """Seconds since speech was last heard, or 0.0 with no detector."""
        if self._detect is None:
            return 0.0
        return self._clock.monotonic() - self._last_speech


class OverlapWatch:
    """Fraction of wall clock where BOTH tracks carry speech at once.

    Measures the people rather than the program: it rises when the parties
    talk over each other and the interpreter keeps up.

    Sampled by the session health poller, because it is a property of both
    tracks and neither direction's pump can see the other.
    """

    def __init__(self, tracks: dict[Direction, SpeechActivity], clock: Clock):
        self._tracks = tracks
        self._clock = clock
        self._last = clock.monotonic()
        self._overlap_s = 0.0
        self._total_s = 0.0

    @property
    def available(self) -> bool:
        """False if any track has no detector.

        A track with no detector never reports speech, so the figure is
        unmeasurable, not zero.
        """
        return bool(self._tracks) and all(a.available for a in self._tracks.values())

    def sample(self) -> float | None:
        now = self._clock.monotonic()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0:
            self._total_s += elapsed
            # Attributed to the interval that just ended, using the flags as
            # they stood through it, so this is a time integral rather than a
            # count of coincidences.
            if self._tracks and all(a.speaking for a in self._tracks.values()):
                self._overlap_s += elapsed
        return self.pct

    @property
    def pct(self) -> float | None:
        """None when it cannot be measured, NOT 0.0.

        Without a detector a zero would read like two people who never
        talked over each other.
        """
        if not self.available:
            return None
        if self._total_s <= 0:
            return 0.0
        return 100.0 * self._overlap_s / self._total_s
