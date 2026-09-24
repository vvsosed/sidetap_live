import asyncio

import pytest

from sidetap_live.metrics import Health, Metrics
from sidetap_live.tui import (
    REFRESH_HZ,
    SidetapLiveApp,
    format_lag,
    format_rotations,
    health_marker,
)
from sidetap_live.types import Direction, SessionState


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

    class StubPlayout:
        suppressed = False

    class StoppableSession:
        def __init__(self):
            self.metrics = Metrics()
            self.stop = threading.Event()
            self.playouts = {d: StubPlayout() for d in Direction}

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

    class StubPlayout:
        suppressed = False

    class SlowSession:
        def __init__(self):
            self.metrics = Metrics()
            self.stop = threading.Event()
            self.playouts = {d: StubPlayout() for d in Direction}

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
