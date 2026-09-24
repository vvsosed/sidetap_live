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

        from sidetap_live.tui import TextStream

        # Text produced before the app started still reaches the pane: the
        # cursor begins at zero, so the first poll is a catch-up like any
        # other.
        assert "privet" in app.query_one("#source-in", TextStream).live_text
        assert "hello" in app.query_one("#target-in", TextStream).live_text
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


async def _tick(app, pilot):
    """Drive one refresh deterministically.

    Waiting on the 10 Hz timer is what made an earlier test flaky under load;
    the poll is a plain method, so call it.
    """
    app.refresh_from_metrics()
    await pilot.pause()


@pytest.mark.asyncio
async def test_new_speech_is_appended_not_the_whole_tail_re_rendered():
    """The pane writes what arrived since the last poll, and only that.

    Re-rendering Metrics' rolling tail every tick is what made the panes
    unreadable: the FRONT was chopped on every fragment, so all of the text
    re-wrapped ten times a second and the words crawled between lines. It
    also made per-tick work grow with the length of the call instead of with
    the speech in it, on the thread that shares a GIL with capture and
    playout.
    """
    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        metrics.append_text(Direction.IN, source="privet ")
        await _tick(app, pilot)
        metrics.append_text(Direction.IN, source="kak dela")
        await _tick(app, pilot)

        stream = app.query_one("#source-in", TextStream)
        assert stream.live_text == "privet kak dela", "text was re-appended"


@pytest.mark.asyncio
async def test_the_words_being_spoken_now_are_visible_before_the_line_fills():
    """A line's worth of speech is about three seconds.

    Holding the newest words back until they fill a line would hide exactly
    the part of the translation the user is waiting on, which is the whole
    reason to be looking at the pane at all.
    """
    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        metrics.append_text(Direction.IN, target="hello")
        await _tick(app, pilot)

        stream = app.query_one("#target-in", TextStream)
        assert stream.live_text == "hello"
        assert "hello" in stream.rendered_live_text


@pytest.mark.asyncio
async def test_a_long_stretch_settles_into_lines_broken_between_words():
    """Run this WIDER than 80 columns deliberately.

    RichLog measures a write against the Rich console, which is 80 columns,
    and the default test terminal is 80 too - so at the default size the
    re-wrap this guards against cannot happen and the test passes either way.
    Every real terminal this runs in is wider.
    """
    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        words = " ".join(f"word{i}" for i in range(80))
        metrics.append_text(Direction.IN, source=words)
        await _tick(app, pilot)

        stream = app.query_one("#source-in", TextStream)
        lines = stream.settled_lines
        assert len(lines) > 1, "a long stretch never settled into lines"
        for line in lines:
            assert len(line) <= stream.wrap_width, f"line overruns the pane: {line!r}"
            # And it must USE the pane's width. RichLog measures a write
            # against the Rich console (80 columns), not against its own
            # region, so an unqualified write() re-wraps anything longer and
            # leaves a ragged short line after every full one - which is what
            # the pane actually looked like before `width=` was passed.
            assert len(line) > stream.wrap_width // 2, (
                f"line was re-wrapped by the widget: {line!r} "
                f"(pane is {stream.wrap_width} wide)"
            )
        # No word may be cut in half by the line break.
        rejoined = " ".join(lines + ([stream.live_text] if stream.live_text else []))
        assert rejoined.split() == words.split(), "a word was split across lines"


@pytest.mark.asyncio
async def test_lines_already_on_screen_do_not_change_when_more_speech_arrives():
    """The anti-reflow guarantee, and the point of the whole exercise.

    Settled lines are written once and never rewritten, so text that has
    already been read stays exactly where it was and the eye can follow the
    stream. Anything that re-wraps the buffer breaks this.
    """
    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        metrics.append_text(Direction.IN, source=" ".join(f"word{i}" for i in range(60)))
        await _tick(app, pilot)
        stream = app.query_one("#source-in", TextStream)
        before = list(stream.settled_lines)
        assert before

        metrics.append_text(Direction.IN, source=" " + " ".join(f"later{i}" for i in range(60)))
        await _tick(app, pilot)
        after = stream.settled_lines

        assert after[: len(before)] == before, "earlier lines were re-wrapped"


@pytest.mark.asyncio
async def test_the_pane_catches_up_when_it_falls_further_behind_than_metrics_keeps():
    """Metrics keeps a bounded tail; the pane's cursor could outrun it.

    Only reachable if the UI thread stalls for many seconds - which is what
    running pw-dump on it used to do. It must resync rather than slice a
    negative offset out of the tail and print text that was never said.
    """
    from sidetap_live.metrics import LIVE_TEXT_CHARS
    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        metrics.append_text(Direction.IN, source="a" * (LIVE_TEXT_CHARS * 3))
        await _tick(app, pilot)

        stream = app.query_one("#source-in", TextStream)
        shown = "\n".join(stream.settled_lines) + stream.live_text
        assert "…" in shown, "the gap was not marked as a gap"
        assert shown.count("a") <= LIVE_TEXT_CHARS, "it invented text it never had"

        # And it carries on normally from there.
        metrics.append_text(Direction.IN, source=" after")
        await _tick(app, pilot)
        assert stream.live_text.endswith("after")


def _screen_text(app) -> str:
    """What is actually composited, not what the widgets believe.

    Reaches for the private compositor deliberately: asserting on widget
    attributes is exactly how a live line laid out outside its container
    shipped green.
    """
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


@pytest.mark.asyncio
async def test_the_line_being_spoken_is_on_screen_once_the_log_has_filled():
    """The newest words must survive the log filling up.

    Sizing the log `height: auto; max-height: 1fr` let it take the whole
    TextStream, so the live Static was laid out one row BELOW its container
    and clipped away. Everything still read correctly off the widgets - the
    text was simply never drawn - and the pane silently stopped showing the
    three seconds of speech the user is waiting on.
    """
    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        for i in range(40):
            metrics.append_text(Direction.IN, source=f" SRC{i:02d}x0 SRC{i:02d}x1 SRC{i:02d}x2")
            app.refresh_from_metrics()
            await pilot.pause()

        stream = app.query_one("#source-in", TextStream)
        assert stream.live_text, "nothing was pending - the test proves nothing"
        assert stream.live_text.strip() in _screen_text(app), (
            "the line being spoken is not on screen"
        )


@pytest.mark.asyncio
async def test_bracketed_transcription_is_not_taken_as_markup():
    """Transcripts contain brackets, and Static defaults to markup=True.

    The settled log is markup=False, so the same words changed as they
    scrolled: `[inaudible]` vanished on the live line and reappeared once it
    settled. An unmatched closing tag is worse - `[/b]` raises MarkupError out
    of the refresh timer and takes the dashboard down mid-call.
    """
    from textual.widgets import Static

    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        metrics.append_text(Direction.IN, source="he said [inaudible] then left")
        app.refresh_from_metrics()
        await pilot.pause()

        stream = app.query_one("#source-in", TextStream)
        assert "[inaudible]" in str(stream.query_one(Static).render())

        # And a tag that closes nothing must not end the call.
        metrics.append_text(Direction.IN, source=" closing [/b] tag")
        app.refresh_from_metrics()
        await pilot.pause()
        assert app.is_running


@pytest.mark.asyncio
async def test_double_width_text_wraps_by_cell_width_not_character_count():
    """`--their-lang zh-CN` is an ordinary input for this program.

    Measuring the cut in code points writes a line twice as wide as the pane,
    which RichLog then re-wraps - reintroducing the full line plus ragged
    remainder that passing `width=` exists to prevent.
    """
    from rich.cells import cell_len
    from textual.widgets import Static

    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        metrics.append_text(Direction.IN, source="你好世界" * 60)
        app.refresh_from_metrics()
        await pilot.pause()

        stream = app.query_one("#source-in", TextStream)
        widths = [cell_len(line) for line in stream.settled_lines]
        assert widths, "nothing settled"
        for width in widths:
            assert width <= stream.wrap_width, "a line is wider than the pane"
            assert width > stream.wrap_width // 2, (
                f"line was re-wrapped by the widget: cell widths {widths}"
            )
        assert stream.query_one(Static).size.height == 1, (
            "the live line grew past one row and pushes the layout"
        )


@pytest.mark.asyncio
async def test_speech_is_not_lost_when_a_feed_fails():
    """The cursor is a promise that the text reached the screen.

    Advancing it before feed() returns means anything that raises - a markup
    error, a NoMatches during a mount race - drops that speech from the pane
    for the rest of the call.
    """
    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        stream = app.query_one("#source-in", TextStream)

        boom = {"armed": True}
        real_feed = stream.feed

        def failing_feed(text):
            if boom["armed"]:
                boom["armed"] = False
                raise RuntimeError("the widget refused")
            real_feed(text)

        stream.feed = failing_feed
        metrics.append_text(Direction.IN, source="do not lose me")
        with pytest.raises(RuntimeError):
            app.refresh_from_metrics()

        # Next poll must offer the same text again.
        app.refresh_from_metrics()
        await pilot.pause()
        assert "do not lose me" in stream.live_text


@pytest.mark.asyncio
async def test_scrolling_back_is_not_undone_by_new_speech():
    """Scrollback that a new fragment yanks away is not scrollback.

    README promises the mouse and arrow keys work; auto_scroll snaps to the
    end on every write, which during speech is about twice a second.
    """
    from textual.widgets import RichLog

    from sidetap_live.tui import TextStream

    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        for i in range(30):
            metrics.append_text(Direction.IN, source=f" frag{i:02d} aaa bbb ccc ddd eee")
            app.refresh_from_metrics()
            await pilot.pause()

        log = app.query_one("#source-in", TextStream).query_one(RichLog)
        assert log.max_scroll_y > 0, "nothing to scroll - the test proves nothing"
        log.scroll_to(y=0, animate=False)
        await pilot.pause()

        for i in range(30, 36):
            metrics.append_text(Direction.IN, source=f" frag{i:02d} aaa bbb ccc ddd eee")
            app.refresh_from_metrics()
            await pilot.pause()

        assert log.scroll_y == 0, "new speech yanked the view back to the end"


@pytest.mark.asyncio
async def test_the_text_panes_do_not_take_focus():
    """Nothing in this app was focusable before the panes became RichLogs.

    RichLog's own CSS tints whichever one holds focus, so one pane comes up
    shaded differently from the other three for no reason a user can act on -
    and Tab silently starts cycling them.
    """
    metrics = Metrics()
    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        assert app.focused is None, f"something took focus: {app.focused!r}"
        assert app.screen.focus_chain == [], "the panes joined the focus chain"


@pytest.mark.asyncio
async def test_a_failing_direction_shows_why_not_just_that_it_failed():
    """The reason outranks both alarms, because it explains them.

    NO AUDIO and DEAD AIR describe symptoms. "prepayment credits are
    depleted" is the one line that tells the user what to do, and under the
    TUI the log it used to go to is a file they cannot see.
    """
    from textual.widgets import Static

    metrics = Metrics()
    metrics.set_error(Direction.IN, "1011 None. Your prepayment credits are depleted.")
    metrics.set_dead_air(Direction.IN, True)

    app = SidetapLiveApp(metrics=metrics, session=None)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        banner = app.query_one("#error-in", Static)
        assert "credits are depleted" in str(banner.content)
        # On its own row and clipped to it. Appended to the stats line it
        # wrapped and pushed the other pane's stats off the screen entirely.
        assert banner.size.height == 1, "the reason wrapped and moved the layout"
        assert str(banner.content) in _screen_text(app) or len(
            str(banner.content)
        ) >= banner.size.width - 1

        # A healthy direction shows no banner at all - and costs no row for
        # it, which an empty Static would.
        assert app.query_one("#error-out", Static).display is False
