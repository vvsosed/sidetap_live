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
from .types import Direction, TARGET_RATE

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
    transcriber's stream, and the questions asked of it now are different:
    "has speech stopped for IDLE_SUSPEND_S" (close the session and stop
    billing) and "has someone started speaking" (reopen it).

    Being wrong is cheap in both directions, which is why this is no longer
    the load-bearing constant it was in sidetap. A false positive keeps a
    session open that could have been suspended, costing a little money; a
    false negative delays a wake by one block. It does NOT affect session
    rotation - that used to wait for a pause in the INPUT, but since the
    switch to make-before-break the join point is a gap in the OUTPUT stream,
    which playout detects by energy and this module never sees.

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


class OverlapWatch:
    """Fraction of wall clock where BOTH tracks carry speech at once.

    The only metric in this package that measures the people rather than the
    program. sidetap's full-replacement routing plus finals-only commit
    forbids overlap by construction, so under it this sits near zero; if the
    two parties naturally begin talking over each other here and it keeps
    working, this rises. That is the project's chosen axis in its most direct
    form.

    Sampled by the session health poller rather than computed per block,
    because it is a property of the two tracks together and neither
    direction's pump can see the other.
    """

    def __init__(self, tracks: dict[Direction, SpeechActivity], clock: Clock):
        self._tracks = tracks
        self._clock = clock
        self._last = clock.monotonic()
        self._overlap_s = 0.0
        self._total_s = 0.0

    def sample(self) -> float:
        now = self._clock.monotonic()
        elapsed = now - self._last
        self._last = now
        if elapsed > 0:
            self._total_s += elapsed
            # Attributed to the interval that just ENDED, using the speaking
            # flags as they stood through it. Sampling the flags and the clock
            # at the same instant is what keeps this a time integral rather
            # than a count of coincidences.
            if self._tracks and all(a.speaking for a in self._tracks.values()):
                self._overlap_s += elapsed
        return self.pct

    @property
    def pct(self) -> float:
        if self._total_s <= 0:
            return 0.0
        return 100.0 * self._overlap_s / self._total_s
