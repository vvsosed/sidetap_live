"""Speak translated audio, duck the original, bound the backlog.

One long-lived pw-cat per direction, fed raw PCM. Between chunks this writes
silence rather than stopping, which keeps pw-cat's buffer primed and tells
playout exactly when it is emitting speech.

The duck is driven by speech in the output, not by the remote party speaking,
so a dead pipeline cannot leave the duck closed: no speech out, no duck.
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

# Ticks without a full chunk before the tail is flushed padded. 200 ms: long
# enough not to pad (and stutter) a producer delivering in small pieces, short
# enough that a real tail does not sit in the buffer.
STARVE_LIMIT_TICKS = 10

# Silent ticks before the duck reopens; see DUCK_HOLD_S.
DUCK_HOLD_TICKS = max(1, int(DUCK_HOLD_S * 1000 / CHUNK_MS))

# Peak amplitude, out of 32767, above which a 20 ms OUTPUT frame counts as
# speech. The model streams output even with nothing to translate; measured
# (docs/experiments/02-voice-stability.md), the idle stream never peaked above
# 1078, while 75.5% of translating frames exceeded 1000. 2000 keeps the idle
# stream reading as silence, so the duck reopens.
SPEECH_PEAK = 2000


def find_silence_boundary(
    pcm: bytearray, frame_bytes: int = CHUNK_BYTES, threshold: int = SPEECH_PEAK
) -> int | None:
    """Byte offset of the first low-energy frame, or None if there is none.

    The lag cap drops backlog here, at a pause, rather than mid-word. Not a
    speech test; use has_speech() for that. A trailing partial frame is not
    examined, as it may yet be filled.
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

    For the lag cap when the buffer starts in a pause: there
    `find_silence_boundary` answers 0, and cutting nothing makes no progress.
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

    NOT `find_silence_boundary(...) is None`: speech often contains a quiet
    frame inside a word, so that would read speech as silence and switch
    sessions mid-word on rotation.

    A short buffer is judged whole rather than ignored, because the caller
    needs an answer about the bytes it has.
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

        The duck registers with the graph after Router.engage() returns, so
        its id is unknown when this is built. Resolving on each transition
        picks it up once it appears.

        `level` is what "closed" means: 0.0 replaces the original entirely,
        0.2 holds it under the translation like an interpreting booth.
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
        # Flip only on success, so a failed wpctl call (or a duck not yet
        # registered) is retried on the next transition.
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
            if self.suppressed:
                # Dropped, not queued: it translates speech from a stretch the
                # listener chose not to hear, and would play on unsuppressing.
                return
            self._pending.extend(pcm)
            self._trim_locked()

    def backlog_s(self) -> float:
        with self._lock:
            return len(self._pending) / TTS_BYTES_PER_S

    def flush(self) -> float:
        """Drop everything not yet handed to the sink. Returns seconds dropped.

        Audio already written to pw-cat cannot be recalled and still plays.
        """
        with self._lock:
            seconds = len(self._pending) / TTS_BYTES_PER_S
            self._pending.clear()
            self._starved = 0
            return seconds

    def set_suppressed(self, value: bool) -> None:
        """Throw the queue away on both edges.

        Nothing queued before suppressing is wanted afterwards, and anything
        that slipped in while suppressed translates speech meant for nobody,
        such as what you said while muted.
        """
        with self._lock:
            self.suppressed = value
        self.flush()

    def _trim_locked(self) -> None:
        """Drop the head of the buffer, but only at a pause in the output.

        Cutting mid-word is worse than running long, so a buffer with no
        quiet frame is left alone.
        """
        while len(self._pending) / TTS_BYTES_PER_S > self._lag_cap_s:
            cut = find_silence_boundary(self._pending)
            if cut is None:
                return
            if cut == 0:
                # The buffer opens on a pause, which is common because the
                # model streams near-silence when idle. Drop that pause so the
                # next iteration can reach the boundary after it. It advances
                # at least one frame, so the loop cannot spin.
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
            # The producer has stopped, not fallen behind, so padding the
            # tail once is safe; padding on every tick would stutter.
            chunk = bytes(self._pending) + b"\x00" * (CHUNK_BYTES - len(self._pending))
            self._pending.clear()
            self._starved = 0
            return chunk
        self._starved += 1
        return None

    def tick(self) -> bool:
        """Write exactly one chunk. True if it carried speech.

        The duck follows SPEECH in the output, not the presence of bytes: the
        model streams output continuously, so a byte trigger would close the
        duck for the whole call. Silent chunks are still written, to keep
        pw-cat primed and the loop paced.
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
        real time with no sleep. A failed PwCatSink returns at once, so the
        loop then waits on its own rather than pinning a core.

        The finally opens the duck unconditionally: a duck stuck closed
        silences the person you are talking to.
        """
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except Exception:
                    # One bad chunk must not end playout; on OUT there is no
                    # raw path to fall back to. The wait stops a persistently
                    # failing tick from spinning.
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

    During a call you are not watching the dashboard, so a silent OUT failure
    has to make a sound. Generated with math.sin; the audio path has no numpy.
    """
    import math
    import struct

    samples = int(TTS_BYTES_PER_S * duration_s) // 2
    amplitude = int(32767 * level)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * i / TTS_RATE)))
        for i in range(samples)
    )
