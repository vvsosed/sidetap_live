from sidetap_live.metrics import Health, Metrics
from sidetap_live.types import Direction, SessionState


def test_snapshot_is_a_deep_copy():
    metrics = Metrics()
    first = metrics.snapshot()
    metrics.set_backlog_s(Direction.IN, 1.5)
    assert first.directions[Direction.IN].backlog_s == 0.0
    assert metrics.snapshot().directions[Direction.IN].backlog_s == 1.5


def test_session_state_and_rotations_are_recorded():
    metrics = Metrics()
    metrics.set_session_state(Direction.OUT, SessionState.OVERLAPPING)
    metrics.add_rotation(Direction.OUT, forced=True, replayed_s=1.2)
    metrics.add_rotation(Direction.OUT, forced=False, replayed_s=0.0)

    state = metrics.snapshot().directions[Direction.OUT]
    assert state.session_state is SessionState.OVERLAPPING
    assert state.rotations == 2
    assert state.forced_rotations == 1
    assert state.replayed_s == 1.2


def test_health_defaults_ok_and_flips():
    metrics = Metrics()
    assert metrics.snapshot().directions[Direction.IN].session is Health.OK
    metrics.set_health(Direction.IN, session=Health.FAILED)
    assert metrics.snapshot().directions[Direction.IN].session is Health.FAILED


def test_overlap_is_session_wide_not_per_direction():
    metrics = Metrics()
    metrics.set_overlap_pct(12.5)
    assert metrics.snapshot().overlap_pct == 12.5


def test_mute_is_recorded_separately_from_bypass():
    """Both suppress the OUT playout, but the TUI must know which the user
    asked for: pressed while bypassed, mute changes what you come back to."""
    metrics = Metrics()
    snapshot = metrics.snapshot()
    assert (snapshot.bypassed, snapshot.muted_out) == (False, False)

    metrics.set_muted_out(True)
    metrics.set_bypassed(True)
    snapshot = metrics.snapshot()
    assert (snapshot.bypassed, snapshot.muted_out) == (True, True)

    metrics.set_bypassed(False)
    assert metrics.snapshot().muted_out is True


def test_live_text_accumulates_rather_than_replacing():
    """The model sends no turn boundary, so each event is a few words.

    Replacing on every fragment left the dashboard showing two words of a
    sentence - which is what a real call actually looked like.
    """
    metrics = Metrics()
    for fragment in ("the coastal", " regions were", " cosmopolitan"):
        metrics.append_text(Direction.IN, source=fragment)
    assert metrics.snapshot().directions[Direction.IN].source == (
        "the coastal regions were cosmopolitan"
    )


def test_live_text_is_bounded_because_the_pane_is_not_a_log():
    from sidetap_live.metrics import LIVE_TEXT_CHARS

    metrics = Metrics()
    for _ in range(200):
        metrics.append_text(Direction.OUT, target="a long stretch of speech ")
    text = metrics.snapshot().directions[Direction.OUT].target
    assert len(text) == LIVE_TEXT_CHARS
    assert text.endswith("speech ")          # keeps the RECENT end, not the old


def test_the_two_streams_accumulate_independently():
    metrics = Metrics()
    metrics.append_text(Direction.IN, source="privet")
    metrics.append_text(Direction.IN, target="hello")
    metrics.append_text(Direction.IN, source=" kak dela")
    state = metrics.snapshot().directions[Direction.IN]
    assert (state.source, state.target) == ("privet kak dela", "hello")
