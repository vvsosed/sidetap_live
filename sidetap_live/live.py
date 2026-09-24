"""The only thing in this package that talks to Gemini.

google-genai's Live API is asyncio-native and everything else here is
threaded, because the ported capture and playout code is subprocess-and-thread
shaped and converting it would risk the tested foundation to serve the part
being measured. So the asyncio island is confined to this module: each
GeminiLiveSession owns one thread running one event loop, and presents the
synchronous InterpreterSession Protocol outward.

`parse_message` is deliberately pure and outside the class. Translating an SDK
message into this package's own event types is the part most likely to be
wrong and most worth testing, and it needs no network to test.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

from .types import (
    TARGET_RATE,
    AudioOut,
    Closed,
    GoAway,
    ResumptionHandle,
    SessionEvent,
    SourceText,
    TargetText,
)

log = logging.getLogger(__name__)

MODEL = "gemini-3.5-live-translate-preview"

# 10 s of 100 ms blocks. Past this the socket is not draining and blocking
# would stall the capture pump and then pw-record itself.
OUTBOUND_BLOCKS = 100
CLOSE_TIMEOUT_S = 3.0

# How long the send loop parks on the outbound queue before looking at
# _closing again. It must be a bounded wait, not a blocking get: an
# indefinite one cannot be interrupted from outside, which is what made
# shutdown depend on pushing a sentinel through a queue that is full exactly
# when shutdown matters most.
QUEUE_POLL_S = 0.1

_SENTINEL = object()


def seconds_of(value) -> float:
    """Coerce whatever `time_left` turns out to be into seconds.

    MEASURED: docs/experiments/03-session-limits.md recorded
    `go_away: time_left='50s' (type=str)` - a STRING, not a number and not a
    duration object. This accepts the other shapes too rather than narrowing
    to the one observation, because being wrong here means the rotation
    window is silently zero and every rotation becomes forced.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.rstrip("s") or 0.0)
    for attr in ("total_seconds", "seconds"):
        got = getattr(value, attr, None)
        if callable(got):
            return float(got())
        if got is not None:
            return float(got)
    log.warning("unrecognised duration %r; treating as 0", value)
    return 0.0


def parse_message(message) -> list[SessionEvent]:
    """Pure: one SDK message in, zero or more SessionEvents out.

    Everything unrecognised is dropped HERE, which is what gives the state
    machine above a finite input alphabet. Written with getattr throughout
    because the preview SDK's message shape is not stable enough to unpack.
    """
    events: list[SessionEvent] = []

    data = getattr(message, "data", None)
    if data:
        events.append(AudioOut(pcm=bytes(data)))

    content = getattr(message, "server_content", None)
    if content is not None:
        source = getattr(content, "input_transcription", None)
        if source is not None and getattr(source, "text", ""):
            events.append(SourceText(text=source.text))
        target = getattr(content, "output_transcription", None)
        if target is not None and getattr(target, "text", ""):
            events.append(TargetText(text=target.text))

    go_away = getattr(message, "go_away", None)
    if go_away is not None:
        events.append(GoAway(time_left_s=seconds_of(getattr(go_away, "time_left", None))))

    update = getattr(message, "session_resumption_update", None)
    if update is not None:
        handle = getattr(update, "new_handle", None)
        if handle and getattr(update, "resumable", False):
            events.append(ResumptionHandle(handle=handle))

    return events


def normalise_language(code: str) -> str:
    """Strip a region subtag, which this model rejects.

    MEASURED, and the failure mode is nasty: `target_language_code="ru-RU"`
    is accepted at connect time and survives the first block or two, then the
    server closes the socket with 1007 "Request contains an invalid argument"
    once it actually tries to use the code. A probe that connects and sends a
    single chunk passes; a real call dies a second in.

        ru     20 blocks  OK        ru-RU  20 blocks  FAIL
        en     20 blocks  OK        en-US  20 blocks  FAIL
        ru-RU   1 block   OK        en-US   1 block   OK

    Google's own examples only ever show bare or script-qualified codes -
    "pl", "en", "es", "zh-Hans" - never a region.

    A SCRIPT subtag is kept: BCP-47 is language[-script][-region], script is
    four letters ("Hans", "Cyrl") and region is two letters or three digits.
    Blindly cutting at the first hyphen would turn "zh-Hans" into "zh" and
    quietly pick the wrong script.

    The CLI still takes full BCP-47 (`--their-lang ru-RU`), because that is
    what sidetap took and what a user naturally types. Normalising here keeps
    the accommodation in the one module that talks to Gemini.
    """
    parts = code.split("-")
    kept = [parts[0]]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            kept.append(part.title())
    return "-".join(kept)


def build_config(*, target_lang: str, echo: bool, handle: str | None):
    """The session config, as the SDK's own typed objects.

    MEASURED, not assumed - see docs/experiments/01-connect.md. The REST
    documentation nests translationConfig under generationConfig; in
    google-genai 2.24.0 it is a TOP-LEVEL field of LiveConnectConfig,
    alongside input_audio_transcription and output_audio_transcription.

    That distinction is load-bearing and silent. `GenerationConfig` ALSO
    exposes a `translation_config` field, so the nested form type-checks AND
    connects, emitting only a DeprecationWarning. A session built the wrong
    way does not fail - it comes up as a conversational agent with its own
    turn-taking instead of an interpreter, which is the exact behaviour this
    project exists to avoid, with nothing in the logs to say so. Do not
    "simplify" this by moving the field under generation_config.

    `session_resumption` and `context_window_compression` are likewise
    top-level fields, confirmed in docs/experiments/03-session-limits.md
    (there via `types.LiveConnectConfig.model_fields`, not guessed).

    Imported inside the function so the package imports with no SDK present,
    as with every other third-party dependency here.
    """
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=normalise_language(target_lang),
            echo_target_language=echo,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


class GeminiLiveSession:
    """Real InterpreterSession. Owns one thread running one event loop."""

    def __init__(self, *, connect, config, model: str = MODEL):
        """`connect(model=..., config=...)` returns an async context manager.

        Injected rather than imported so a test can drive this class without
        the SDK. run.py passes `genai.Client(...).aio.live.connect`.
        """
        self._connect = connect
        self._config = config
        self._model = model
        self._outbound: queue.Queue = queue.Queue(maxsize=OUTBOUND_BLOCKS)
        self._inbound: queue.Queue = queue.Queue()
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="live-session"
        )
        self._thread.start()

    def send(self, pcm: bytes) -> None:
        try:
            self._outbound.put_nowait(pcm)
        except queue.Full:
            # Audio is lost either way once the socket stops draining; this
            # way the process survives and the NO_AUDIO watchdog upstream
            # stays meaningful instead of being masked by a stalled pump.
            log.warning("live outbound queue full; dropped a block")

    def events(self):
        while True:
            item = self._inbound.get()
            if item is _SENTINEL:
                return
            yield item

    def close(self) -> None:
        """Must never block. Every caller is on a pump thread.

        This used to unblock the send loop by putting a sentinel on the
        outbound queue - a BLOCKING put on a bounded queue that send() already
        documents as expected to fill whenever the socket stops draining. With
        nothing consuming it, the put never returned, and since _close,
        _suspend and _switch all run on the pump thread, a wedged socket at
        rotation time stopped that direction feeding audio for the rest of the
        call. The send loop now polls _closing instead, so setting it is
        enough.
        """
        self._closing.set()
        self._thread.join(timeout=CLOSE_TIMEOUT_S)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            log.exception("live session ended abnormally")
            self._inbound.put(Closed(reason=str(exc)))
        else:
            self._inbound.put(Closed(reason="ended"))
        finally:
            # Always, even if Closed could not be queued: events() must
            # return or the receive thread above leaks for the whole call.
            self._inbound.put(_SENTINEL)

    async def _main(self) -> None:
        """Run the send and receive loops until one of them finishes.

        Whichever finishes first is retrieved and re-raised, and that is the
        whole point. `asyncio.wait` RETURNS the completed task rather than
        raising its exception, so without this a hard API rejection - a 1007
        invalid-argument, a revoked key - was stored in the task and never
        read: _thread_main took its `else` branch and queued
        Closed(reason="ended"), the interpreter treated a fatal error as a
        normal close and quietly reopened, and the real diagnosis surfaced
        only as Python's "Task exception was never retrieved" noise at
        garbage-collection time.

        The cancelled task is gathered for the same reason - a cancellation
        left unretrieved produces the same noise.
        """
        async with self._connect(model=self._model, config=self._config) as session:
            sender = asyncio.create_task(self._send_loop(session))
            receiver = asyncio.create_task(self._recv_loop(session))
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            # Tell the sender to stop before awaiting it. Cancelling the task
            # does not interrupt the queue wait running inside an executor
            # thread, so without this the gather below waits out the full
            # timeout; without the gather, the old code abandoned the thread
            # outright, leaking one per failed session - and rotation produces
            # one every nine minutes.
            self._closing.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    CLOSE_TIMEOUT_S,
                )
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("live session loops did not stop within %ss", CLOSE_TIMEOUT_S)
            for task in done:
                if task.exception() is not None:
                    raise task.exception()

    def _next_block(self) -> bytes | None:
        """One block, or None if the queue stayed empty for QUEUE_POLL_S."""
        try:
            return self._outbound.get(timeout=QUEUE_POLL_S)
        except queue.Empty:
            return None

    async def _send_loop(self, session) -> None:
        from google.genai import types

        loop = asyncio.get_running_loop()
        while not self._closing.is_set():
            # The outbound queue is thread-safe but blocking, so it is drained
            # on a worker thread rather than stalling the event loop - which
            # would stop the receive side too, for as long as nobody speaks.
            # The wait is BOUNDED so that setting _closing is enough to end
            # this loop: an unbounded get also parked a default-executor
            # thread forever, and those are joined at interpreter exit, so a
            # session that was never closed stopped the process exiting at all
            # despite the thread being a daemon.
            pcm = await loop.run_in_executor(None, self._next_block)
            if pcm is None:
                continue
            # MEASURED: scripts/exp02_voice_stability.py, exp03 and exp04 all
            # pass a `types.Blob(...)`, not a plain dict, even though
            # `AsyncSession.send_realtime_input`'s `audio` parameter also
            # accepts a BlobDict. Matching the shape that was actually
            # exercised against the live API rather than the untested
            # alternative that merely type-checks.
            await session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={TARGET_RATE}")
            )

    async def _recv_loop(self, session) -> None:
        # MEASURED: docs/experiments/02-voice-stability.md and
        # docs/experiments/03-session-limits.md both found that
        # `session.receive()` is a per-turn async generator, not a
        # connection-long one - it ends when one interaction/turn completes.
        # Re-invoking it in an outer loop is what keeps this receiving for
        # the life of the connection instead of silently going quiet after
        # the first turn.
        while True:
            async for message in session.receive():
                for event in parse_message(message):
                    self._inbound.put(event)


def build_factory(client, *, model: str = MODEL):
    """A SessionFactory closing over one genai client."""

    class _Factory:
        def open(self, target_lang: str, *, echo: bool, handle: str | None = None):
            return GeminiLiveSession(
                connect=client.aio.live.connect,
                config=build_config(target_lang=target_lang, echo=echo, handle=handle),
                model=model,
            )

    return _Factory()
