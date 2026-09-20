"""Speech activity observation. Never removes a byte.

This is NOT a gate. sidetap's vad.py decided what to send; this only reports
whether speech is present, because a model that reasons over continuous audio
has to receive continuous audio. Two consumers need it: rotation has to find a
pause to rotate inside, and idle-suspend has to notice speech onset.

The module is deliberately not called vad.py. The old name would invite
someone to reinstate gating, and gating is the one thing it must not do.
"""

from __future__ import annotations

import logging
from typing import Callable

from .ports import Clock
from .types import TARGET_RATE

log = logging.getLogger(__name__)

# webrtcvad accepts 10, 20 or 30 ms frames only.
FRAME_MS = 20
FRAME_BYTES = TARGET_RATE * 2 * FRAME_MS // 1000

SpeechDetector = Callable[[bytes], bool]


def webrtc_detector(aggressiveness: int = 2) -> SpeechDetector | None:
    """Real detector, or None when webrtcvad is unavailable.

    `aggressiveness` runs 0-3, higher filtering more non-speech. It is
    inherited from sidetap, which inherited it from meetscribe, and it is NOT
    a settled value here - it was tuned to decide what to drop from a
    transcriber's stream, and the only question asked of it now is "has
    speech stopped for ROTATE_PAUSE_S". Being wrong costs something different:
    a false pause rotates the session mid-sentence. Tune it against
    docs/experiments/02-voice-stability.md rather than assuming it.

    The returned closure is stateful - webrtcvad adapts to the noise floor
    across calls - so give each track its own detector rather than sharing one.
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
        # Seeded at construction, not left None: starting the program before
        # the call means nobody has spoken yet, and that must still count as
        # silence or idle-suspend would never fire on exactly the session a
        # user is most likely to leave running.
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
