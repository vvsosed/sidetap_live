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
from collections.abc import Callable

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


def leading_silence_bytes(
    pcm, frame_bytes: int = CHUNK_BYTES, threshold: int = SPEECH_PEAK
) -> int:
    """Length of the run of quiet frames at the head, in whole frames.

    The lag cap needs this for the case where the buffer already STARTS in a
    pause. `find_silence_boundary` correctly answers 0 there, which is not a
    byte offset the cap can cut at - dropping nothing makes no progress - so
    it needs to know where that opening pause ends instead.
    """
    offset = 0
    while offset + frame_bytes <= len(pcm):
        samples = array("h")
        samples.frombytes(bytes(pcm[offset : offset + frame_bytes]))
        if max(abs(s) for s in samples) >= threshold:
            break
        offset += frame_bytes
    return offset


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


class Playout:
    """One direction's output: a pending buffer drained one chunk per tick."""

    def __init__(
        self,
        direction: Direction,
        sink: AudioSink,
        *,
        duck: DuckControl | None = None,
        lag_cap_s: float = LAG_CAP_S,
    ):
        self.direction = direction
        self.dropped_s = 0.0
        self.spoken_s = 0.0
        self.suppressed = False
        self._sink = sink
        self._duck = duck
        self._lag_cap_s = lag_cap_s
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._starved = 0
        self._idle_ticks = DUCK_HOLD_TICKS

    @property
    def duck(self) -> DuckControl | None:
        return self._duck

    def submit(self, pcm: bytes) -> None:
        with self._lock:
            self._pending.extend(pcm)
            self._trim_locked()

    def backlog_s(self) -> float:
        with self._lock:
            return len(self._pending) / TTS_BYTES_PER_S

    def flush(self) -> float:
        """Drop everything not yet handed to the sink. Returns seconds dropped.

        The chunk already passed to sink.write() cannot be recalled - pw-cat
        has it. sidetap measured 441 ms still sitting in pw-cat's own buffer
        at the moment the hotkey fires (docs/experiments/02-pwcat-playback.md
        in that repository); that audio is past this process's control and
        plays regardless.
        """
        with self._lock:
            seconds = len(self._pending) / TTS_BYTES_PER_S
            self._pending.clear()
            self._starved = 0
            return seconds

    def set_suppressed(self, value: bool) -> None:
        """Entering bypass throws the queue away.

        The conversation during bypass happens unmediated, so a translation of
        it is worth nothing by the time it plays - it would arrive as a voice
        recapping a minute the user has already had. And the cap lives below
        the suppressed branch in tick(), so a backlog built while suppressed
        is never trimmed.
        """
        self.suppressed = value
        if value:
            self.flush()

    def _trim_locked(self) -> None:
        """Drop the head of the buffer, but only at a pause in the output.

        Backlog growth is this project's experimental result, not a nuisance,
        so the cap sits high and is expected never to fire in an ordinary
        call. When it does, cutting mid-word would be worse than running long,
        so a buffer with no quiet frame in it is left alone.
        """
        while len(self._pending) / TTS_BYTES_PER_S > self._lag_cap_s:
            cut = find_silence_boundary(self._pending)
            if cut is None:
                return
            if cut == 0:
                # The buffer already opens on a pause. `if not cut` used to
                # treat this exactly like "no pause anywhere" and return - and
                # since the model streams a near-silent output whenever it has
                # nothing to translate, a quiet head is the common case. The
                # cap therefore almost never fired: measured at 40 s of
                # backlog held against a 30 s cap with nothing dropped.
                #
                # Dropping the opening pause is inaudible and is what lets the
                # next iteration reach the boundary after it. It always
                # advances by at least one frame, so the loop cannot spin.
                cut = leading_silence_bytes(self._pending)
            del self._pending[:cut]
            self.dropped_s += cut / TTS_BYTES_PER_S
            log.warning(
                "%s playout %.1fs behind; dropped %.1fs at a pause (%.1fs total)",
                self.direction.value,
                len(self._pending) / TTS_BYTES_PER_S,
                cut / TTS_BYTES_PER_S,
                self.dropped_s,
            )

    def _take_locked(self) -> bytes | None:
        if len(self._pending) >= CHUNK_BYTES:
            chunk = bytes(self._pending[:CHUNK_BYTES])
            del self._pending[:CHUNK_BYTES]
            self._starved = 0
            return chunk
        if self._pending and self._starved >= STARVE_LIMIT_TICKS:
            # The producer has stopped rather than merely fallen behind.
            # Padding here splices at most one chunk of silence onto a tail
            # that was ending anyway; padding on every tick, which is what
            # doing this unconditionally would mean, would stutter.
            chunk = bytes(self._pending) + b"\x00" * (CHUNK_BYTES - len(self._pending))
            self._pending.clear()
            self._starved = 0
            return chunk
        self._starved += 1
        return None

    def tick(self) -> bool:
        """Write exactly one chunk. True if it carried speech.

        The duck follows SPEECH in the output, not the presence of bytes.
        That distinction is the whole ballgame: the model emits a continuous
        24 kHz stream whether or not it is translating (measured - 151 s of
        audio for 154 s of pure silence in), so a byte-presence trigger would
        close the duck on the first chunk and never reopen it, muting the
        remote party for the entire call. Silent chunks are still WRITTEN, to
        keep pw-cat's buffer primed and the loop paced; they just do not
        count as speech.
        """
        if self.suppressed:
            if self._duck is not None:
                self._duck.open()
            self._sink.write(SILENCE_CHUNK)
            return False

        with self._lock:
            chunk = self._take_locked()

        if chunk is None:
            chunk = SILENCE_CHUNK
            speech = False
        else:
            speech = has_speech(chunk)

        if speech:
            self._idle_ticks = 0
            self.spoken_s += CHUNK_MS / 1000
            if self._duck is not None:
                self._duck.close()
        else:
            self._idle_ticks += 1
            if self._duck is not None and self._idle_ticks >= DUCK_HOLD_TICKS:
                self._duck.open()

        self._sink.write(chunk)
        return speech

    def run(self, stop: threading.Event) -> None:
        """Pace comes from the sink.

        pw-cat blocks on write once its buffer is full, so this loop runs at
        real time with no sleep. But PwCatSink.write() swallows a dead pipe and
        becomes a no-op, and a no-op never blocks - so a sink that dies
        mid-call removes the only thing pacing this loop and it would pin a
        core until hangup. The fallback wait is not belt-and-braces; it is the
        whole reason the `failed` flag is readable from here.

        The finally is the module's one fail-safe. Leaving the duck closed
        silences the person you are talking to and leaves them speaking to
        nobody, which is worse than this program not working at all.
        """
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except Exception:
                    # The same reasoning as the interpreter's pump(): one bad
                    # chunk must not take the direction down for the rest of
                    # the call. Without this, a single exception ended playout
                    # permanently - the finally below opened the duck, so IN
                    # degraded to the unmediated call, but OUT has no raw path
                    # and the remote party simply heard nothing from then on.
                    # The wait keeps a persistently failing tick from spinning.
                    log.exception("%s playout tick failed", self.direction.value)
                    stop.wait(CHUNK_MS / 1000)
                    continue
                if getattr(self._sink, "failed", False):
                    stop.wait(CHUNK_MS / 1000)
        finally:
            if self._duck is not None:
                self._duck.open()
            self._sink.close()


def earcon(duration_s: float = 0.25, frequency: float = 880.0, level: float = 0.25) -> bytes:
    """A short tone for the dead-air alarm.

    During a call you are looking at the other person, not at a dashboard, so
    the OUT direction failing silently has to make a sound. Generated rather
    than shipped as an asset, and with math.sin rather than numpy, because the
    audio path deliberately has no numpy in it.
    """
    import math
    import struct

    samples = int(TTS_BYTES_PER_S * duration_s) // 2
    amplitude = int(32767 * level)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * i / TTS_RATE)))
        for i in range(samples)
    )
