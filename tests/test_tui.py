import pytest

from sidetap_live.metrics import Health, Metrics
from sidetap_live.tui import SidetapLiveApp, format_lag, format_rotations, health_marker
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
