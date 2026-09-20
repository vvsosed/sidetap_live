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
    metrics.set_session_state(Direction.OUT, SessionState.DRAINING)
    metrics.add_rotation(Direction.OUT, forced=True, replayed_s=1.2)
    metrics.add_rotation(Direction.OUT, forced=False, replayed_s=0.0)

    state = metrics.snapshot().directions[Direction.OUT]
    assert state.session_state is SessionState.DRAINING
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
