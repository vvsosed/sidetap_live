"""Speak translated audio, duck the original, bound the backlog.

One long-lived pw-cat per direction, fed raw PCM. Between chunks this writes
silence rather than stopping, which keeps pw-cat's buffer primed and gives
playout exact knowledge of when it is emitting speech - and THAT is what
drives the duck.

The duck is driven from the output queue, not from the remote party speaking.
sidetap does it the other way round, which means a dead pipeline leaves the
duck closed over a live call - the failure its "fails open" invariant exists
to prevent, patched with a finally clause. Here, no audio out means no duck,
by construction.
"""

from __future__ import annotations

import logging
import threading
from array import array
from typing import Callable

from .ports import AudioSink, VolumeControl
from .types import DUCK_HOLD_S, LAG_CAP_S, TTS_BYTES_PER_S, TTS_RATE, Direction

log = logging.getLogger(__name__)

CHUNK_MS = 20
CHUNK_BYTES = TTS_BYTES_PER_S * CHUNK_MS // 1000
SILENCE_CHUNK = b"\x00" * CHUNK_BYTES

# Ticks of not receiving a full chunk before the tail is flushed padded.
# 10 x 20 ms = 200 ms: long enough that a producer delivering at realtime in
# small pieces is never padded (which would stutter), short enough that a
# genuine tail is not left sitting in the buffer holding the duck closed.
STARVE_LIMIT_TICKS = 10

# Silent ticks before the duck reopens. Without the hold it flaps in the gaps
# between output chunks and chops the original into fragments, which is heard
# as the duck failing rather than as hysteresis missing.
DUCK_HOLD_TICKS = max(1, int(DUCK_HOLD_S * 1000 / CHUNK_MS))

# Peak amplitude, out of 32767, above which a frame counts as SPEECH in the
# OUTPUT audio.
#
# Measured, not guessed - docs/experiments/02-voice-stability.md. This model
# emits a continuous 24 kHz output stream whether or not it has anything to
# translate: over ~200 combined seconds of "nothing to say" audio, 20 ms
# frames peaked above 1000 only 0.04% of the time and the worst frame ever
# observed peaked at 1078. Actively translating audio peaked above 1000 in
# 75.5% of frames, overall peak 27235. 2000 sits clear of the measured idle
# ceiling (1078) while still well inside the mass of real speech, so the idle
# stream reads as silence and the duck reopens rather than staying shut for
# the whole call - the "silently cruel" failure mode this project cares most
# about avoiding.
SPEECH_PEAK = 2000


def find_silence_boundary(
    pcm: bytearray, frame_bytes: int = CHUNK_BYTES, threshold: int = SPEECH_PEAK
) -> int | None:
    """Byte offset of the first low-energy frame, or None if there is none.

    Two callers, one predicate. The lag cap uses this to find where it may
    drop backlog: dropping at an arbitrary offset cuts a word in half,
    dropping at a pause in the output is inaudible beyond the missing
    sentence. The duck uses it directly on the current chunk to ask "is this
    frame speech?" - offset 0 means no, keep the duck open; any other result
    (including None) means yes, close it. Uses `array` rather than numpy
    because the audio path in this package carries no numpy, in either
    direction, exactly as in sidetap. A trailing partial frame is not
    examined - it may yet be filled.
    """
    for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        samples = array("h")
        samples.frombytes(bytes(pcm[start : start + frame_bytes]))
        if max(abs(s) for s in samples) < threshold:
            return start
    return None


def has_speech(pcm: bytes, frame_bytes: int = CHUNK_BYTES,
               threshold: int = SPEECH_PEAK) -> bool:
    """Does this buffer carry speech anywhere in it?

    Deliberately NOT `find_silence_boundary(...) is None`. That asks where the
    FIRST quiet frame is, which is the right question for the lag cap and the
    wrong one here: a 250 ms chunk of clear speech routinely contains a quiet
    20 ms frame inside a word, and would read as silence. The interpreter uses
    this to decide when the outgoing session has stopped talking, so getting it
    backwards means every rotation switches mid-word - exactly the audible seam
    make-before-break exists to remove.

    A short buffer is judged whole rather than ignored: unlike the lag cap,
    which can afford to wait for a full frame, a caller asking "is this
    speech?" needs an answer about the bytes it actually has.
    """
    if not pcm:
        return False
    for start in range(0, max(len(pcm) - frame_bytes + 1, 1), frame_bytes):
        frame = pcm[start : start + frame_bytes]
        if len(frame) < 2:
            continue
        samples = array("h")
        samples.frombytes(bytes(frame[: len(frame) // 2 * 2]))
        if samples and max(abs(s) for s in samples) >= threshold:
            return True
    return False


class DuckControl:
    """Silences (or lowers) the remote party's original while we speak."""

    def __init__(
        self,
        volume: VolumeControl,
        object_id: int | Callable[[], int | None],
        level: float = 0.0,
    ):
        """`object_id` may be a callable, and for a live session it must be.

        Router.engage() finishes before pw-loopback has registered the duck
        with the graph - deliberately, because the alternative is journalling
        links to ports that do not exist yet - so the duck's object id is
        still None when Session.setup() builds this. Reading it once there
        meant the duck was never created at all, ducking never happened, and
        the user heard the original underneath every translation for the whole
        call, with nothing logged. Resolving it on each transition lets the id
        arrive a poll later, which is exactly when it does arrive.

        `level` is what "closed" means: 0.0 replaces the original entirely,
        0.2 holds it under the translation the way an interpreting booth does.
        """
        self._volume = volume
        self._object_id = object_id
        self._level = level
        self._closed = False

    def _resolve(self) -> int | None:
        if callable(self._object_id):
            return self._object_id()
        return self._object_id

    def close(self) -> None:
        # Only flip on a successful call. set_volume returns False rather than
        # raising when wpctl fails; flipping anyway would desync the flag from
        # the real volume, and the next transition would think it is already
        # in the target state and skip retrying. A duck that has not appeared
        # yet is the same case: not an error, just not yet.
        if self._closed:
            return
        object_id = self._resolve()
        if object_id is not None and self._volume.set_volume(object_id, self._level):
            self._closed = True

    def open(self) -> None:
        if not self._closed:
            return
        object_id = self._resolve()
        if object_id is not None and self._volume.set_volume(object_id, 1.0):
            self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed
