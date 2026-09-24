import asyncio
from types import SimpleNamespace

import pytest

from sidetap_live.live import build_config, parse_message, seconds_of
from sidetap_live.types import (
    AudioOut,
    GoAway,
    ResumptionHandle,
    SourceText,
    TargetText,
)


def message(**kwargs) -> SimpleNamespace:
    base = dict(data=None, server_content=None, go_away=None,
                session_resumption_update=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_audio_becomes_one_event():
    assert parse_message(message(data=b"\x01\x02")) == [AudioOut(pcm=b"\x01\x02")]


def test_both_transcriptions_come_through_separately():
    content = SimpleNamespace(
        input_transcription=SimpleNamespace(text="hello"),
        output_transcription=SimpleNamespace(text="privet"),
    )
    assert parse_message(message(server_content=content)) == [
        SourceText(text="hello"),
        TargetText(text="privet"),
    ]


def test_empty_transcription_text_is_not_an_event():
    content = SimpleNamespace(
        input_transcription=SimpleNamespace(text=""),
        output_transcription=None,
    )
    assert parse_message(message(server_content=content)) == []


def test_go_away_carries_seconds():
    go = SimpleNamespace(time_left="42s")
    assert parse_message(message(go_away=go)) == [GoAway(time_left_s=42.0)]


def test_unresumable_update_yields_no_handle():
    update = SimpleNamespace(new_handle="abc", resumable=False)
    assert parse_message(message(session_resumption_update=update)) == []
    update = SimpleNamespace(new_handle="abc", resumable=True)
    assert parse_message(message(session_resumption_update=update)) == [
        ResumptionHandle(handle="abc")
    ]


def test_unknown_message_is_dropped_not_raised():
    """The state machine above needs a finite input alphabet."""
    assert parse_message(SimpleNamespace(tool_call="something new")) == []


@pytest.mark.parametrize(
    "value,expected",
    [("42s", 42.0), ("0.5s", 0.5), (42, 42.0), (42.5, 42.5), (None, 0.0)],
)
def test_seconds_of_accepts_every_shape_the_sdk_might_use(value, expected):
    assert seconds_of(value) == expected


def test_config_sets_target_and_echo():
    config = build_config(target_lang="ru", echo=True, handle=None)
    assert config.translation_config.target_language_code == "ru"
    assert config.translation_config.echo_target_language is True


def test_translation_config_is_top_level_not_under_generation_config():
    """Regression guard for a silent failure mode.

    GenerationConfig also has a translation_config field, so the nested form
    type-checks and connects with only a DeprecationWarning - producing a
    conversational agent instead of an interpreter, with nothing in the logs
    saying so. See docs/experiments/01-connect.md.
    """
    config = build_config(target_lang="ru", echo=False, handle=None)
    assert config.translation_config is not None
    assert config.generation_config is None


def test_a_failing_session_reports_why_it_died():
    """The reason must reach the application, not just the logs.

    asyncio.wait RETURNS the completed task rather than raising, so a hard
    API rejection used to be stored in the task and never read: the session
    queued Closed(reason="ended"), the interpreter treated a fatal error as
    a normal close and quietly reopened, and the real diagnosis appeared
    only as "Task exception was never retrieved" at GC time.
    """
    import contextlib

    from sidetap_live.live import GeminiLiveSession
    from sidetap_live.types import Closed

    class _Session:
        async def send_realtime_input(self, **kwargs):
            await asyncio.sleep(0.01)

        async def receive(self):
            raise RuntimeError("1007 Request contains an invalid argument.")
            yield  # pragma: no cover - makes this an async generator

    @contextlib.asynccontextmanager
    async def connect(*, model, config):
        yield _Session()

    session = GeminiLiveSession(connect=connect, config={}, model="m")
    events = list(session.events())

    assert len(events) == 1
    assert isinstance(events[0], Closed)
    assert "1007" in events[0].reason
    assert events[0].reason != "ended"


def test_a_failing_session_does_not_leak_its_sender_thread():
    """_send_loop parks on a blocking queue.get in an executor thread.

    Cancelling the task does not interrupt that, so the session must push a
    sentinel to release it. Otherwise every failed session leaks a parked
    thread, and rotation produces one every nine minutes.
    """
    import contextlib
    import threading

    from sidetap_live.live import GeminiLiveSession

    before = threading.active_count()

    class _Session:
        async def send_realtime_input(self, **kwargs):
            await asyncio.sleep(0.01)

        async def receive(self):
            await asyncio.sleep(0.05)
            raise RuntimeError("1007 Request contains an invalid argument.")
            yield  # pragma: no cover - makes this an async generator

    @contextlib.asynccontextmanager
    async def connect(*, model, config):
        yield _Session()

    session = GeminiLiveSession(connect=connect, config={}, model="m")
    list(session.events())
    session._thread.join(timeout=5.0)
    assert not session._thread.is_alive()

    # Give the executor a moment to unwind, then confirm we are not growing.
    for _ in range(50):
        if threading.active_count() <= before + 1:
            break
        threading.Event().wait(0.05)
    assert threading.active_count() <= before + 1, "sender thread leaked"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("ru-RU", "ru"),          # region stripped - the bug that killed a real call
        ("en-US", "en"),
        ("uk-UA", "uk"),
        ("pt-BR", "pt"),
        ("ru", "ru"),             # already bare
        ("zh-Hans", "zh-Hans"),   # SCRIPT kept - cutting at the hyphen picks wrong script
        ("zh-Hans-CN", "zh-Hans"),
        ("sr-Cyrl-RS", "sr-Cyrl"),
    ],
)
def test_region_subtags_are_stripped_but_scripts_are_kept(given, expected):
    """target_language_code rejects a region, and does it late.

    "ru-RU" connects fine and survives a block or two, then the server closes
    with 1007 once it tries to use the code - so a probe that sends one chunk
    passes while a real call dies a second in.
    """
    from sidetap_live.live import normalise_language

    assert normalise_language(given) == expected


def test_build_config_normalises_what_the_cli_passes():
    """The CLI takes full BCP-47 because that is what a user types."""
    assert build_config(target_lang="ru-RU", echo=False,
                        handle=None).translation_config.target_language_code == "ru"


@pytest.mark.asyncio
async def test_close_returns_even_when_the_socket_has_stopped_draining():
    """close() must never block on the outbound queue.

    send() already documents a full queue as an expected condition - it is
    what happens when the socket stops draining - and close() then put the
    sentinel with a BLOCKING put on that same bounded queue. With nothing
    consuming it, the put never returned.

    Every caller of close() is on the pump thread (_close, _suspend,
    _switch), so a wedged socket at rotation time stopped that direction
    feeding audio at all, for the rest of the call.
    """
    import contextlib
    import threading

    from sidetap_live.live import OUTBOUND_BLOCKS, GeminiLiveSession
    from sidetap_live.types import BLOCK_BYTES

    entered_send = threading.Event()

    class WedgedSocket:
        async def send_realtime_input(self, **_):
            entered_send.set()
            await asyncio.Event().wait()

        async def receive(self):
            await asyncio.Event().wait()
            yield

    @contextlib.asynccontextmanager
    async def connect(**_):
        yield WedgedSocket()

    session = GeminiLiveSession(connect=connect, config=None, model="m")

    # Pin the send loop INSIDE send_realtime_input before filling the queue.
    # Otherwise it may still be parked on the blocking get in the executor,
    # which would consume the sentinel and hide the bug intermittently.
    session.send(b"\x00" * BLOCK_BYTES)
    assert await asyncio.get_running_loop().run_in_executor(
        None, entered_send.wait, 5.0
    ), "the send loop never reached the socket"

    for _ in range(OUTBOUND_BLOCKS + 20):
        session.send(b"\x00" * BLOCK_BYTES)
    assert session._outbound.full(), "precondition: the socket is not draining"

    returned = threading.Event()
    threading.Thread(target=lambda: (session.close(), returned.set()), daemon=True).start()

    assert await asyncio.get_running_loop().run_in_executor(
        None, returned.wait, 8.0
    ), "close() blocked on a full outbound queue"


def test_an_unparseable_duration_degrades_instead_of_raising():
    """seconds_of promises to accept shapes other than the one observed.

    It coerced str with float(value.rstrip("s")), which throws on anything
    that is not a bare number - and it sits on the GoAway path, so a shape
    change in the preview SDK would kill the session at the one moment the
    rotation depends on, rather than costing a forced rotation.
    """
    assert seconds_of("PT50S") == 0.0
    assert seconds_of("1m30s") == 0.0
    assert seconds_of("50s") == 50.0      # still the measured shape


@pytest.mark.asyncio
async def test_the_receive_loop_does_not_spin_when_the_stream_ends():
    """`while True` around `async for ... session.receive()` had no exit.

    receive() is a per-turn generator, so the outer loop is what keeps the
    connection alive past the first turn. But a generator that ends without
    raising - a half-closed connection - turned that into a hot loop:
    measured at 3.4 million re-entries in 2 seconds, one pegged core for the
    rest of the call.
    """
    import contextlib

    from sidetap_live.live import GeminiLiveSession

    entries = 0

    class HalfClosed:
        async def send_realtime_input(self, **_):
            await asyncio.Event().wait()

        async def receive(self):
            nonlocal entries
            entries += 1
            return
            yield

    @contextlib.asynccontextmanager
    async def connect(**_):
        yield HalfClosed()

    session = GeminiLiveSession(connect=connect, config=None, model="m")
    await asyncio.sleep(1.0)
    session.close()

    assert entries < 1000, f"receive() was re-entered {entries:,} times in 1s"
