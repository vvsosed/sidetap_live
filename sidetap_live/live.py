"""The only thing in this package that talks to Gemini.

google-genai's Live API is asyncio-native and everything else here is
threaded, so asyncio is confined to this module: each GeminiLiveSession owns
one thread running one event loop and presents the synchronous
InterpreterSession Protocol outward.

`parse_message` is pure and outside the class, so the translation from SDK
messages to our events, the part most likely to be wrong, is testable offline.
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

# How long the send loop waits on the outbound queue before checking
# _closing. Bounded, because an indefinite get cannot be interrupted.
QUEUE_POLL_S = 0.1

_SENTINEL = object()


def seconds_of(value) -> float:
    """Coerce whatever `time_left` turns out to be into seconds.

    Measured as the STRING '50s' (docs/experiments/03-session-limits.md).
    Other shapes are accepted too, because a wrong guess silently makes the
    rotation window zero.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Guarded: 'PT50S' or '1m30s' would raise, and a ValueError on the
        # GoAway path kills the session instead of forcing a rotation.
        try:
            return float(value.rstrip("s") or 0.0)
        except ValueError:
            pass
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

    Everything unrecognised is dropped here, giving the state machine a
    finite input alphabet. getattr throughout, because the preview SDK's
    message shape is not stable.
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

    A code like "ru-RU" is accepted at connect, then the server closes with
    1007 once it uses it, a second or so into a call.

    A SCRIPT subtag is kept: BCP-47 is language[-script][-region] and a
    script is four letters, so "zh-Hans" must not become "zh".

    The CLI takes full BCP-47 because that is what users type; normalising
    here keeps the workaround in the one module that talks to Gemini.
    """
    parts = code.split("-")
    kept = [parts[0]]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            kept.append(part.title())
    return "-".join(kept)


def build_config(*, target_lang: str, echo: bool, handle: str | None):
    """The session config, as the SDK's own typed objects.

    `translation_config` is a TOP-LEVEL field of LiveConnectConfig, despite
    the REST docs nesting it under generationConfig. `GenerationConfig` also
    has one, so the nested form connects with only a DeprecationWarning and
    yields a conversational agent with its own turn-taking instead of an
    interpreter. Do not move it under generation_config.

    `session_resumption` and `context_window_compression` are top-level too.

    The SDK is imported lazily so the package imports without it.
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
            # way the capture pump does not stall behind it.
            log.warning("live outbound queue full; dropped a block")

    def events(self):
        while True:
            item = self._inbound.get()
            if item is _SENTINEL:
                return
            yield item

    def close(self) -> None:
        """Must not block indefinitely: every caller is on a pump thread.

        Setting _closing is enough, since the send loop polls it. A put on
        the bounded outbound queue could block forever on a wedged socket.
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
            # Always: events() must return or its receive thread leaks.
            self._inbound.put(_SENTINEL)

    async def _main(self) -> None:
        """Run the send and receive loops until one of them finishes.

        The finished task's exception is re-raised: `asyncio.wait` returns
        it rather than raising, and an unread API rejection (a 1007, a
        revoked key) would otherwise be reported as a normal close. The
        cancelled task is gathered so its exception is retrieved too.
        """
        async with self._connect(model=self._model, config=self._config) as session:
            sender = asyncio.create_task(self._send_loop(session))
            receiver = asyncio.create_task(self._recv_loop(session))
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            # Tell the sender to stop before awaiting it: cancelling does
            # not interrupt its queue wait in the executor thread, and the
            # gather would wait out the full timeout.
            self._closing.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    CLOSE_TIMEOUT_S,
                )
            except TimeoutError:
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
            # Drained on a worker thread, because a blocking get would stall
            # the event loop and with it the receive side. The wait is
            # bounded so setting _closing ends the loop; an unbounded one
            # parks an executor thread that blocks interpreter exit.
            pcm = await loop.run_in_executor(None, self._next_block)
            if pcm is None:
                continue
            # A types.Blob rather than a dict: the shape the experiments
            # exercised against the live API.
            await session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={TARGET_RATE}")
            )

    async def _recv_loop(self, session) -> None:
        # `session.receive()` ends after each turn, not with the connection,
        # so it is re-invoked in an outer loop.
        while not self._closing.is_set():
            produced = False
            async for message in session.receive():
                produced = True
                for event in parse_message(message):
                    self._inbound.put(event)
            # An empty turn means the connection is finished, not idle: a
            # live receive() waits for the next message. Without this the
            # loop spins on a half-closed socket. Returning ends the session
            # and lets the interpreter reopen it.
            if not produced:
                return


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
