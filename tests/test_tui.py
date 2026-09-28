import asyncio
import contextlib

import pytest
from textual.widgets._footer import FooterKey

from sidetap_live.metrics import Health, Metrics
from sidetap_live.tui import (
    REFRESH_HZ,
    SidetapLiveApp,
    format_lag,
    format_rotations,
    health_marker,
)
from sidetap_live.types import Direction, SessionState
from tests.conftest import FakeSessionFactory, build_session


def test_markers_distinguish_the_three_states():
    assert len({health_marker(h) for h in Health}) == 3


def test_rotations_show_the_clean_forced_split():
    """The split is what says whether the pause assumption survived contact."""
    assert format_rotations(0, 0) == "—"
    assert format_rotations(5, 0) == "5"
    assert format_rotations(5, 2) == "5 (2 forced)"


def test_lag_is_one_decimal():
    assert format_lag(1.234) == "1.2s"


@pytest.mark.asyncio
async def test_the_pane_shows_both_transcription_streams():
    metrics = Metrics()
    metrics.append_text(Direction.IN, source="privet", target="hello")
    metrics.set_backlog_s(Direction.IN, 0.4)
    metrics.set_session_state(Direction.IN, SessionState.RUNNING)

    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Static

        assert "privet" in str(app.query_one("#source-in", Static).content)
        assert "hello" in str(app.query_one("#target-in", Static).content)
        assert "0.4s" in str(app.query_one("#stats-in", Static).content)


@pytest.mark.asyncio
async def test_overlap_and_cost_are_in_the_subtitle():
    metrics = Metrics()
    metrics.set_overlap_pct(12.5)
    metrics.add_cost(1.23)
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "overlap 12%" in app.sub_title
        assert "$1.23" in app.sub_title


@pytest.mark.asyncio
async def test_the_tui_exits_when_the_session_is_stopped_from_outside():
    """SIGTERM must be able to end the call.

    run_session()'s signal handler does nothing but `session.stop.set()`.
    _run_headless polls that flag; the TUI did not, so App.run() never
    returned, run_session()'s `finally: session.shutdown()` never ran, and
    router.restore() never ran either - leaving the user's call routed
    through the duck and silenced until somebody pressed a key.

    Ctrl-C cannot stand in for this: Textual clears the terminal's ISIG flag
    while it owns the screen, so no SIGINT is delivered at all.
    """
    import threading

    class StoppableSession:
        def __init__(self):
            self.metrics = Metrics()
            self.stop = threading.Event()

    session = StoppableSession()
    app = SidetapLiveApp(metrics=session.metrics, session=session)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running

        session.stop.set()            # what handle_signal does, and all it does

        # Wait for the refresh timer rather than assuming a fixed number of
        # frames: it runs at REFRESH_HZ, so two pauses are not guaranteed to
        # contain a tick and the assertion below would flake under load.
        for _ in range(50):
            await pilot.pause()
            if not app.is_running:
                break
            await asyncio.sleep(1 / REFRESH_HZ)

        assert not app.is_running, (
            "the TUI ignored session.stop, so shutdown() and router.restore() "
            "would never run"
        )


@pytest.mark.asyncio
async def test_bypass_does_not_run_graph_work_on_the_ui_thread():
    """`b` is the escape hatch, reached for when things are already wrong.

    set_bypass reaches _link_real_mic, which takes a pw-dump snapshot
    (timeout 10 s) and one or two pw-link calls (5 s each). Run inline in the
    action handler that froze the whole dashboard - no repaint, no other key,
    not even `q` - for up to ~20 s if PipeWire was slow, which is exactly
    when it would be slow.
    """
    import threading

    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    class SlowSession:
        def __init__(self):
            self.metrics = Metrics()
            self.stop = threading.Event()

        def set_bypass(self, value):
            started.set()
            release.wait(2.0)          # stands in for a slow pw-dump
            finished.set()

    session = SlowSession()
    app = SidetapLiveApp(metrics=session.metrics, session=session)
    async with app.run_test() as pilot:
        await pilot.pause()

        app.action_bypass()
        assert not finished.is_set(), "set_bypass ran inline on the UI thread"

        # Yield to the loop so the worker can be scheduled - blocking on a
        # threading.Event here would stop the very loop that starts it.
        for _ in range(100):
            if started.is_set():
                break
            await pilot.pause()
            await asyncio.sleep(0.01)

        assert started.is_set(), "the bypass work never started"
        release.set()


@pytest.mark.asyncio
async def test_unmeasurable_overlap_shows_a_dash_not_zero_percent():
    metrics = Metrics()
    metrics.set_overlap_pct(None)
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "overlap —" in app.sub_title
        assert "0%" not in app.sub_title


def engaged_keys(app) -> set[str]:
    """Footer keys currently lit, named by the action each one triggers."""
    return {key.action for key in app.query(FooterKey) if key.has_class("-engaged")}


@contextlib.contextmanager
def live_session(args, ports):
    """A real Session, set up and always shut down.

    setup() opens the transcript's long-lived append handle, starts a pw-cat
    sink per direction and engages the router; without the shutdown these
    tests leak all three and leave the graph rewired. The caller must let this
    close AFTER `app.run_test()`: shutdown() sets session.stop, which makes the
    next tick call app.exit().
    """
    session = build_session(args, ports, sessions=FakeSessionFactory())
    session.setup()
    try:
        yield session
    finally:
        session.shutdown()


@pytest.mark.asyncio
async def test_bypass_does_not_light_the_mute_key(session_args, fake_ports):
    """The two toggles are separate latches and the footer must say so.

    Bypass suppresses the OUT playout, and the footer used to be painted from
    `playouts[OUT].suppressed` - which is `bypassed or muted_out`. So pressing
    `b` lit `Mute out` as well, while metrics.muted_out was still False: the
    lamp and the key that toggles it were reading different fields.
    """
    with live_session(session_args, fake_ports) as session:
        app = SidetapLiveApp(metrics=session.metrics, session=session)
        async with app.run_test() as pilot:
            await pilot.pause()
            session.set_bypass(True)
            app.refresh_from_metrics()
            await pilot.pause()

            assert session.playouts[Direction.OUT].suppressed is True  # precondition
            assert session.metrics.snapshot().muted_out is False
            assert engaged_keys(app) == {"bypass"}


@pytest.mark.asyncio
async def test_pressing_mute_under_bypass_changes_what_the_footer_shows(
    session_args, fake_ports
):
    """`m` must never be a keypress you cannot see the result of.

    With the lamp painted from playout suppression, `m` under bypass moved
    nothing on screen - the key was already lit - so you could not tell
    whether you had just muted or unmuted. Leaving bypass then dropped you
    into a muted OUT you did not remember asking for, and OUT has no raw path
    to fall through to: the remote party hears nothing and has no way to know.
    """
    with live_session(session_args, fake_ports) as session:
        app = SidetapLiveApp(metrics=session.metrics, session=session)
        async with app.run_test() as pilot:
            await pilot.pause()
            # set_bypass directly, not `b`: action_bypass hands the work to a
            # thread worker, and what this test is about is the `m` that
            # follows. bypass stays covered by
            # test_bypass_does_not_run_graph_work_on_the_ui_thread.
            session.set_bypass(True)
            app.refresh_from_metrics()
            await pilot.pause()
            before = "mute" in engaged_keys(app)

            # The real keypress, through BINDINGS: calling action_mute() by
            # name would stay green even if `m` were re-keyed away from it.
            # `m` hands the work to a thread worker, so wait for the flag it
            # sets rather than assuming one pause is enough - otherwise this
            # asserts on whichever side of the hop it happens to land.
            await pilot.press("m")
            for _ in range(100):
                if session.metrics.snapshot().muted_out:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)
            assert session.metrics.snapshot().muted_out, "the mute never landed"

            app.refresh_from_metrics()
            await pilot.pause()
            after = "mute" in engaged_keys(app)

            assert before != after, "pressing `m` changed nothing on screen"
            assert engaged_keys(app) == {"bypass", "mute"}

            # And leaving bypass leaves the latch you set, still visible.
            session.set_bypass(False)
            app.refresh_from_metrics()
            await pilot.pause()
            assert engaged_keys(app) == {"mute"}
            assert session.playouts[Direction.OUT].suppressed is True


@pytest.mark.asyncio
async def test_mute_does_not_run_session_work_on_the_ui_thread():
    """`m` and `b` contend for the same lock, so `m` must not block the loop.

    Session.set_mute_out and Session.set_bypass both take _lifecycle_lock,
    and set_bypass holds it across a pw-dump (timeout 10 s) plus up to two
    pw-link calls (5 s each). Run inline in the action handler, `m` pressed
    inside that window stopped the event loop for as long as bypass took:
    no repaint, no hotkey, not even `q`.

    That also means shutdown() and router.restore() never run, because the
    TUI is what polls session.stop - so the call stays routed through the
    duck and silenced.
    """
    import threading

    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    class SlowSession:
        def __init__(self):
            self.metrics = Metrics()
            self.stop = threading.Event()

        def set_mute_out(self, value):
            started.set()
            release.wait(2.0)          # stands in for bypass holding the lock
            finished.set()

    session = SlowSession()
    app = SidetapLiveApp(metrics=session.metrics, session=session)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("m")
        assert not finished.is_set(), "set_mute_out ran inline on the UI thread"

        for _ in range(100):
            if started.is_set():
                break
            await pilot.pause()
            await asyncio.sleep(0.01)

        assert started.is_set(), "the mute work never started"
        release.set()


@pytest.mark.asyncio
async def test_a_failing_hotkey_does_not_tear_the_dashboard_down():
    """Bypass is what you reach for when PipeWire is ALREADY misbehaving.

    set_bypass reaches PwDumpGraphSource.snapshot, which is a bare
    subprocess.run(timeout=10, check=True) - so a missing or wedged pw-dump
    raises straight into the worker. Textual's run_worker defaults to
    exit_on_error=True, which exits the app: pressing `b` to rescue a bad
    call ended it instead, with a Rich traceback as the only explanation.
    """
    import threading

    class ExplodingSession:
        def __init__(self):
            self.metrics = Metrics()
            self.stop = threading.Event()

        def set_bypass(self, value):
            raise RuntimeError("pw-dump: command not found")

    session = ExplodingSession()
    app = SidetapLiveApp(metrics=session.metrics, session=session)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("b")
        for _ in range(100):
            await pilot.pause()
            await asyncio.sleep(0.01)
            if "FAILED" in app.sub_title:
                break

        assert app.is_running, "a failed hotkey killed the dashboard"
        # And it has to be visible: under the TUI, logging goes to a file.
        assert "FAILED" in app.sub_title, app.sub_title
