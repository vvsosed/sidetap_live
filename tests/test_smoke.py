"""End to end through the fakes. No audio hardware, no network, no API key."""

import threading

import pytest

from sidetap_live.activity import SpeechActivity
from sidetap_live.capture import DroppingQueue
from sidetap_live.cost import Rates
from sidetap_live.interpreter import DirectionInterpreter, InterpreterConfig
from sidetap_live.metrics import Metrics
from sidetap_live.playout import CHUNK_BYTES, DuckControl, Playout
from sidetap_live.preroll import PreRoll
from sidetap_live.transcript import EventTranscript
from sidetap_live.types import (
    BLOCK_BYTES,
    TTS_BYTES_PER_S,
    AudioChunk,
    AudioOut,
    Direction,
    SourceText,
    TargetText,
)
from tests.conftest import (
    FakeAudioSink,
    FakeClock,
    FakeSessionFactory,
    FakeVolumeControl,
)

SPEECH = b"\x00\x40" * (BLOCK_BYTES // 2)


def test_speech_in_becomes_ducked_audio_out_and_a_transcript(tmp_path):
    clock = FakeClock()
    metrics = Metrics()
    sessions = FakeSessionFactory()
    volume = FakeVolumeControl()
    sink = FakeAudioSink()
    transcript = EventTranscript(tmp_path, session="smoke")

    playout = Playout(
        Direction.IN, sink, duck=DuckControl(volume, object_id=42)
    )
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.IN, target_lang="en", echo=False),
        sessions=sessions,
        playout=playout,
        activity=SpeechActivity(lambda pcm: True, clock),
        metrics=metrics,
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(),
        on_event=transcript.write,
    )

    # 1. Speech arrives, a session opens, audio is sent.
    interpreter.feed(AudioChunk(track="remote", pcm=SPEECH, t_start=0.0))
    assert len(sessions.sessions) == 1
    assert sessions.sessions[0].sent

    # 2. The model answers with audio and both transcription streams.
    interpreter.note_event(SourceText(text="privet"))
    interpreter.note_event(AudioOut(pcm=b"\x00\x40" * (TTS_BYTES_PER_S // 2)))
    interpreter.note_event(TargetText(text="hello"))

    # 3. Playout speaks it and the duck closes while it does.
    assert playout.tick() is True
    assert volume.calls == [(42, 0.0)]
    assert len(sink.chunks[0]) == CHUNK_BYTES

    # 4. The duck reopens once the audio runs out.
    while playout.backlog_s() > 0:
        playout.tick()
    for _ in range(50):
        playout.tick()
    assert volume.calls[-1] == (42, 1.0)

    # 5. Both streams reached the transcript, unpaired and in order.
    path = transcript.close()
    text = path.read_text()
    assert text.index("privet") < text.index("hello")


def test_the_pump_thread_drains_a_capture_queue_and_stops(tmp_path):
    """The threaded path, not just feed()."""
    clock = FakeClock()
    sessions = FakeSessionFactory()
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.OUT, target_lang="ru", echo=True),
        sessions=sessions,
        playout=Playout(Direction.OUT, FakeAudioSink()),
        activity=SpeechActivity(lambda pcm: True, clock),
        metrics=Metrics(),
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(),
    )
    queue = DroppingQueue()
    for _ in range(3):
        queue.put(AudioChunk(track="mic", pcm=SPEECH, t_start=0.0))

    stop = threading.Event()
    thread = threading.Thread(target=interpreter.pump, args=(queue, stop))
    thread.start()
    try:
        for _ in range(100):
            if sessions.sessions and len(sessions.sessions[0].sent) >= 3 * BLOCK_BYTES:
                break
            threading.Event().wait(0.01)
        else:
            pytest.fail("pump never drained the queue")
    finally:
        stop.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert sessions.sessions[0].closed is True
