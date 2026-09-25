import argparse
import dataclasses
import logging
import signal
import threading
import time
from pathlib import Path

import pytest

from sidetap_live.metrics import Metrics
from sidetap_live.run import Session
from sidetap_live.types import NO_AUDIO_S, TTS_RATE, Direction
from tests.conftest import (
    FakeClock,
    FakeGraphSource,
    FakeLauncher,
    FakeLinker,
    FakeSessionFactory,
    FakeVolumeControl,
)


@dataclasses.dataclass
class FakePorts:
    """One session's worth of injectable collaborators, bundled.

    Session.setup() and Session.start() never touch real audio hardware, a
    real network, or real credentials when built from one of these - every
    port is a fake from tests/conftest.py.
    """

    graph: FakeGraphSource
    launcher: FakeLauncher
    linker: FakeLinker
    clock: FakeClock
    volume: FakeVolumeControl
    journal_path: Path


@pytest.fixture
def session_args(tmp_path):
    return argparse.Namespace(
        app="zoom",
        mic=None,
        latency="100ms",
        their_lang="ru-RU",
        my_lang="en-US",
        out=tmp_path,
        lag_cap=None,
        no_tui=True,
        verbose=False,
        duck_level=0.0,
        echo_out=True,
        idle_suspend=True,
    )


# routing_graph throughout, not the zoom fixture: setup() refuses to start
# without sidetap_tts_sink, and only this fixture has it. It carries the same
# ZOOM VoiceEngine stream, so engage(app_pattern="zoom") still matches.
#
# journal_path is pinned under tmp_path rather than left at Router's real
# default (~/.local/state/sidetap_live/routing-journal.json). Session.setup()
# below runs Router.engage() against the routing fixture, which routes a real
# link and journals it - against the default path that would write to the
# machine actually running this suite, not a fixture. Every other Router test
# in tests/test_routing.py makes the same substitution.
@pytest.fixture
def fake_ports(tmp_path, routing_graph) -> FakePorts:
    return FakePorts(
        graph=FakeGraphSource(routing_graph),
        launcher=FakeLauncher(),
        linker=FakeLinker(),
        clock=FakeClock(),
        volume=FakeVolumeControl(),
        journal_path=tmp_path / "routing-journal.json",
    )


@pytest.fixture
def fake_ports_without_virtmic(tmp_path, routing_graph) -> FakePorts:
    from sidetap_live.routing import VIRTMIC_SINK

    without = dataclasses.replace(
        routing_graph,
        nodes=tuple(n for n in routing_graph.nodes if n.name != VIRTMIC_SINK),
    )
    return FakePorts(
        graph=FakeGraphSource(without),
        launcher=FakeLauncher(),
        linker=FakeLinker(),
        clock=FakeClock(),
        volume=FakeVolumeControl(),
        journal_path=tmp_path / "routing-journal.json",
    )


def build_session(args, ports: FakePorts, *, sessions=None, **overrides) -> Session:
    """Session, built from one FakePorts bundle with any port swappable.

    `overrides` lets one test replace a single collaborator - a half-broken
    linker, say - without rebuilding the rest of the bundle.
    """
    kwargs = dict(
        graph=ports.graph,
        launcher=ports.launcher,
        linker=ports.linker,
        clock=ports.clock,
        sessions=sessions,
        volume=ports.volume,
        journal_path=ports.journal_path,
    )
    kwargs.update(overrides)
    return Session(args, **kwargs)


def test_in_never_echoes_and_out_always_may(session_args, fake_ports):
    """The asymmetry is a consequence of the duck policy, not a preference."""
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    assert session.interpreters[Direction.IN]._config.echo is False
    assert session.interpreters[Direction.OUT]._config.echo is True


def test_only_the_in_direction_has_a_duck(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    assert session.playouts[Direction.IN].duck is not None
    assert session.playouts[Direction.OUT].duck is None


def test_targets_are_crossed(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    assert session.interpreters[Direction.IN]._config.target_lang == session_args.my_lang
    assert session.interpreters[Direction.OUT]._config.target_lang == session_args.their_lang


def test_setup_fails_before_engaging_when_the_virtual_mic_is_missing(
    session_args, fake_ports_without_virtmic
):
    """Nothing may be rewired before a fatal check, or there is nobody left
    to restore it."""
    session = build_session(
        session_args, fake_ports_without_virtmic, sessions=FakeSessionFactory()
    )
    with pytest.raises(Exception, match="doctor"):
        session.setup()
    assert session.router is None


def test_the_graph_is_restored_on_shutdown(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.shutdown()
    assert session.router_restored is True


def test_the_graph_is_restored_even_if_a_worker_join_fails(session_args, fake_ports):
    """A half-restored graph leaves the user with no call audio at all."""
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()

    def explode():
        raise RuntimeError("join blew up")

    session._join_workers = explode
    session.shutdown()
    assert session.router_restored is True


def test_shutdown_is_idempotent(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.shutdown()
    session.shutdown()
    assert session.router_restored is True


def test_the_transcript_is_closed_on_shutdown(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.shutdown()
    assert session.transcript.md_path.exists()
    # Every rendered file, not just the interleaved one: they are written in
    # the same close(), so a half-done shutdown leaves the set incomplete.
    assert session.transcript.original_path.exists()
    assert session.transcript.translated_path.exists()


def test_bypass_opens_the_duck_and_links_the_real_mic(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_bypass(True)
    # All three together, or translated speech talks over the unmediated
    # conversation bypass exists to step out of.
    assert session.metrics.snapshot().bypassed is True
    assert session.playouts[Direction.IN].suppressed is True
    assert session.playouts[Direction.OUT].suppressed is True
    session.shutdown()


def test_bypass_toggles_back(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_bypass(True)
    session.set_bypass(False)
    assert session.metrics.snapshot().bypassed is False
    assert session.playouts[Direction.IN].suppressed is False
    session.shutdown()


def test_mute_suppresses_only_the_outbound_direction(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_mute_out(True)
    assert session.playouts[Direction.OUT].suppressed is True
    # Mute is "stop sending my voice", not "stop the call". Suppressing IN
    # too would silence the person you are listening to.
    assert session.playouts[Direction.IN].suppressed is False
    assert session.metrics.snapshot().muted_out is True
    session.shutdown()


def test_muting_clears_the_backlog_rather_than_deferring_it(session_args, fake_ports):
    """A queue built before the mute is a conversation that has moved on.

    Session.set_mute_out has to reach Playout.set_suppressed, which flushes.
    Assigning `suppressed` directly would suppress and keep the backlog, and
    unmuting would then play a voice recapping the last minute.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.playouts[Direction.OUT].submit(b"\x00" * (TTS_RATE * 2 * 3))  # 3 seconds
    assert session.playouts[Direction.OUT].backlog_s() == 3.0

    session.set_mute_out(True)
    assert session.playouts[Direction.OUT].suppressed is True
    assert session.playouts[Direction.OUT].backlog_s() == 0.0
    session.shutdown()


def test_mute_while_bypassed_does_not_un_suppress_the_outbound_playout(
    session_args, fake_ports
):
    """Bypass's third effect must survive the mute key.

    Before mute became a flag of its own, `m` read `not playout.suppressed` -
    which under bypass is `not True` - and switched OUT back on, putting
    translated speech over the unmediated conversation bypass exists to step
    out of.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_bypass(True)
    session.set_mute_out(True)
    assert session.playouts[Direction.OUT].suppressed is True
    session.set_mute_out(False)
    assert session.playouts[Direction.OUT].suppressed is True
    session.shutdown()


def test_leaving_bypass_restores_mute_rather_than_clearing_it(session_args, fake_ports):
    """Muting, bypassing and coming back used to leave you audible."""
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_mute_out(True)
    session.set_bypass(True)
    session.set_bypass(False)
    assert session.playouts[Direction.OUT].suppressed is True
    assert session.metrics.snapshot().muted_out is True
    # IN was only ever suppressed by bypass, so it comes back.
    assert session.playouts[Direction.IN].suppressed is False
    session.shutdown()


def test_leaving_bypass_un_suppresses_when_not_muted(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_bypass(True)
    session.set_bypass(False)
    assert session.playouts[Direction.OUT].suppressed is False
    session.shutdown()


def test_an_unchanged_suppression_state_is_not_re_applied(session_args, fake_ports):
    """set_suppressed flushes, and flush cuts the utterance in progress short.

    Pressing `b` while already muted must not chop the sentence that is
    playing on the IN side, and re-asserting OUT's unchanged state must not
    chop anything either.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_mute_out(True)

    calls = []
    out = session.playouts[Direction.OUT]
    original = out.set_suppressed
    out.set_suppressed = lambda value: (calls.append(value), original(value))[1]

    session.set_bypass(True)
    assert calls == [], "OUT was already suppressed by mute; nothing to re-apply"
    session.set_mute_out(True)
    assert calls == [], "setting mute to the value it already had re-flushed"
    session.shutdown()


def test_one_direction_dying_does_not_drop_the_call(session_args, fake_ports):
    """A bad language must not kill an OUT direction that is fine."""
    from sidetap_live.metrics import Health

    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.direction_stop[Direction.IN].set()
    session._on_direction_fatal(Direction.IN, RuntimeError("bad lang"))

    assert session.metrics.snapshot().directions[Direction.IN].session is Health.FAILED
    assert session.stop.is_set() is False, "the call was dropped"
    session.shutdown()


def test_the_session_stops_once_every_direction_is_dead(session_args, fake_ports):
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    for direction in Direction:
        session.direction_stop[direction].set()
        session._on_direction_fatal(direction, RuntimeError("bad lang"))

    assert session.stop.is_set() is True
    session.shutdown()


def test_quitting_while_bypassed_unlinks_the_real_mic(session_args, fake_ports):
    """The virtual mic outlives the process and the journal never saw this link.

    Left behind, the user's raw voice reaches every later call alongside the
    translation, and `doctor --repair` cannot find it to undo it.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    before = set(fake_ports.linker.links)
    session.set_bypass(True)
    added = set(fake_ports.linker.links) - before
    assert added, "bypass linked nothing, so this test would prove nothing"
    session.shutdown()
    for pair in added:
        assert pair in fake_ports.linker.unlinks, (
            f"{pair} was left wired into the virtual mic"
        )


def test_bypass_opens_the_duck_without_waiting_for_a_tick(session_args, fake_ports):
    """A playout thread whose sink has died never ticks again.

    If bypass only set a flag for tick() to read, the duck would stay shut and
    the one escape hatch from a bad interpretation would be silence.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    duck = session.playouts[Direction.IN].duck
    assert duck is not None, "the routing fixture is meant to produce a duck"
    duck.close()
    session.set_bypass(True)
    assert duck.is_open is True
    session.shutdown()


def test_a_deaf_track_is_reported_rather_than_looking_healthy(session_args, fake_ports):
    """An unlinked capture node delivers zero bytes, not silence.

    So nothing downstream sees anything to react to - every pane stays green
    while that direction hears nothing at all.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    stop = threading.Event()
    thread = threading.Thread(
        target=session._poll_capture_health, args=(stop,), daemon=True
    )
    thread.start()
    fake_ports.clock.advance(NO_AUDIO_S + 2.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if session.metrics.snapshot().directions[Direction.OUT].no_audio:
            break
    stop.set()
    thread.join(timeout=2.0)
    assert session.metrics.snapshot().directions[Direction.OUT].no_audio is True
    session.shutdown()


def test_a_quiet_inbound_track_is_not_a_fault_before_the_call_starts(
    session_args, fake_ports
):
    """Starting sidetap-live before the call is an ordinary thing to do.

    Alarming on it would train the user to ignore the one warning that counts.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.router._routed.clear()
    assert session._armed(Direction.IN) is False
    assert session._armed(Direction.OUT) is True
    session.shutdown()


def test_the_interpreters_keep_running_while_bypassed(session_args, fake_ports):
    """So the transcript stays continuous and toggling back restarts nothing."""
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session.set_bypass(True)
    assert session.stop.is_set() is False
    session.shutdown()


def test_a_failure_in_start_still_gives_the_audio_graph_back(session_args, fake_ports):
    """start() runs after engage() has already rewired the call.

    capture.start() can time out waiting for a node. Left outside the guard,
    that exits with the user's call audio routed into a duck node and no
    process left to undo it.
    """
    from sidetap_live import run as run_module

    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())

    def explode():
        raise RuntimeError("capture node never appeared")

    def fake_session(*a, **k):
        session.start = explode
        return session

    original = run_module.Session
    run_module.Session = fake_session
    try:
        with pytest.raises(RuntimeError, match="capture node never appeared"):
            run_module.run_session(
                session_args,
                graph=fake_ports.graph,
                launcher=fake_ports.launcher,
                linker=fake_ports.linker,
                clock=fake_ports.clock,
            )
    finally:
        run_module.Session = original

    assert session.router_restored is True, "the call was left inside the duck"


def test_closing_the_terminal_shuts_down_like_ctrl_c(session_args, fake_ports, monkeypatch):
    """SIGHUP's default action exits with no `finally`, so nothing is restored.

    The duck loopback runs in its own session and outlives us, leaving the
    call routed through it. The handlers are put back once the session ends.
    """
    from sidetap_live import run as run_module

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    installed = {}

    def setup():
        installed.update({sig: signal.getsignal(sig) for sig in signals})
        raise RuntimeError("stop after the handlers are in")

    session.setup = setup
    monkeypatch.setattr(run_module, "Session", lambda *a, **k: session)
    before = {sig: signal.getsignal(sig) for sig in signals}

    with pytest.raises(RuntimeError, match="stop after the handlers are in"):
        run_module.run_session(
            session_args,
            graph=fake_ports.graph,
            launcher=fake_ports.launcher,
            linker=fake_ports.linker,
            clock=fake_ports.clock,
        )

    assert callable(installed[signal.SIGHUP]), "SIGHUP still kills the process"
    installed[signal.SIGHUP](signal.SIGHUP, None)
    assert session.stop.is_set(), "SIGHUP did not ask the session to stop"
    after = {sig: signal.getsignal(sig) for sig in signals}
    assert after == before, "the session's handlers outlived it"


def test_bypass_does_not_claim_success_when_the_mic_link_fails(
    session_args, fake_ports, caplog
):
    """Both playouts are already suppressed by this point.

    So a silently failed link means the other party hears nothing at all while
    the interface reports bypass as fully engaged.
    """
    import logging

    from sidetap_live.ports import LinkResult

    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    session._linker = FakeLinker(result=LinkResult.FAILED)
    with caplog.at_level(logging.ERROR):
        session.set_bypass(True)

    assert session._real_mic_links == [], "a failed link was recorded as live"
    assert "hearing silence" in caplog.text
    session.shutdown()


def test_a_bypass_that_raises_partway_still_gets_cleaned_up(session_args, fake_ports):
    """The links already made are live even though set_bypass never finished.

    They outlive the process, the virtual mic is permanent, and nothing
    journals them - so doctor --repair cannot find them either.
    """
    state = {"armed": False}

    class HalfBrokenLinker(FakeLinker):
        def link(self, src_port, dst_port):
            # Armed only across set_bypass, so router.restore()'s own
            # re-linking during shutdown still works - otherwise this test
            # would be about a broken restore rather than about the leak.
            # The link itself lands; what fails is everything after it, which
            # is the case a naive "record it once we know it worked" ordering
            # gets wrong.
            result = super().link(src_port, dst_port)
            if state["armed"]:
                raise RuntimeError("pw-link vanished")
            return result

    linker = HalfBrokenLinker()
    session = build_session(
        session_args, fake_ports, sessions=FakeSessionFactory(), linker=linker
    )
    session.setup()

    state["armed"] = True
    with pytest.raises(RuntimeError):
        session.set_bypass(True)
    state["armed"] = False

    live = list(session._real_mic_links)
    assert live, "this test proves nothing unless a link really was made"
    assert session._bypassed is True, "bypass must be claimed before it can leak"

    session.shutdown()
    for pair in live:
        assert pair in linker.unlinks, f"{pair} was left wired into the virtual mic"


def test_a_sink_spawned_before_a_failed_setup_is_not_left_running(
    session_args, fake_ports
):
    """The playout threads' own finally cannot help here.

    Those threads only exist once start() has succeeded. The launcher uses
    start_new_session=True, so an orphaned pw-cat survives this process
    entirely and accumulates on every failed launch.
    """
    from sidetap_live import run as run_module

    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())

    def boom(*a, **k):
        raise run_module.CaptureError("no default microphone")

    original = run_module.PipeWireCapture
    run_module.PipeWireCapture = boom
    try:
        with pytest.raises(run_module.CaptureError):
            session.setup()
    finally:
        run_module.PipeWireCapture = original

    assert session.sinks, "this test proves nothing unless a sink was built"
    session.shutdown()
    launcher = fake_ports.launcher
    playback = [
        w for w in launcher.writers
        if "--playback" in launcher.writer_calls[launcher.writers.index(w)]
    ]
    assert playback, "no pw-cat playback process was spawned"
    assert all(w.terminated for w in playback), "a pw-cat was left orphaned"


class _StubHeadlessSession:
    """Just enough Session for _run_headless."""

    def __init__(self):
        self.metrics = Metrics()
        self.alarms = 0

        class OnePass(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True

        self.stop = OnePass()

    def alarm_dead_air(self) -> None:
        self.alarms += 1


def test_headless_reports_no_audio_on_either_direction(caplog):
    """_poll_capture_health computes no_audio for both directions every
    second, and the TUI shows it as NO AUDIO ARRIVING - but _run_headless
    only ever looked at OUT's dead_air.

    An unlinked capture node delivers zero bytes rather than silence, so
    under --no-tui (systemd, screen) the remote party's stream dropping
    mid-call produced nothing in the log for the rest of the call. This is
    precisely the failure no_audio exists to catch.
    """
    from sidetap_live.run import _run_headless

    session = _StubHeadlessSession()
    session.metrics.set_no_audio(Direction.IN, True)

    with caplog.at_level(logging.ERROR, logger="sidetap_live.run"):
        _run_headless(session)

    assert any("NO AUDIO" in r.message for r in caplog.records), (
        "headless never reported that a direction had gone deaf"
    )


def test_a_fatal_direction_is_wired_from_the_interpreter(session_args, fake_ports):
    """_on_direction_fatal had no production caller - only tests reached it.

    Session must hand the interpreter a way back, and must stop that
    direction's pump when it fires, or the "both directions are dead" check
    can never become true.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    try:
        interpreter = session.interpreters[Direction.IN]
        assert interpreter._on_fatal is not None, "the interpreter cannot report a fatal"

        interpreter._on_fatal(Direction.IN, RuntimeError("1007 invalid argument"))
        assert session.direction_stop[Direction.IN].is_set(), (
            "the dead direction's pump was left running"
        )
        assert not session.stop.is_set(), "one dead direction dropped the whole call"
    finally:
        session.shutdown()


def test_a_fatal_direction_stops_only_its_own_pump(session_args, fake_ports):
    """Each pump runs until its direction's event is set, not the session's.

    Handed the session-wide `stop`, a pump ignores direction_stop, so the
    dead direction keeps retrying against the API for the rest of the call.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    spawned = {}
    session.capture.start = lambda: None
    session._spawn = lambda target, args, name: spawned.__setitem__(name, (target, args))
    session.start()
    pumps = {}
    for direction in Direction:
        target, args = spawned[f"pump-{direction.value}"]
        pumps[direction] = threading.Thread(target=target, args=args, daemon=True)
        pumps[direction].start()
    try:
        session._on_direction_fatal(Direction.IN, RuntimeError("bad lang"))
        pumps[Direction.IN].join(timeout=2.0)
        assert not pumps[Direction.IN].is_alive(), "the dead direction's pump kept running"
        assert pumps[Direction.OUT].is_alive(), "the healthy direction's pump stopped"
    finally:
        session.shutdown()
        for thread in pumps.values():
            thread.join(timeout=2.0)
    assert not pumps[Direction.OUT].is_alive(), "shutdown left a pump running"


def test_a_dead_direction_names_the_flag_for_its_target_language(
    session_args, fake_ports, caplog
):
    """IN translates into --my-lang and OUT into --their-lang."""
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    try:
        with caplog.at_level(logging.ERROR, logger="sidetap_live.run"):
            session._on_direction_fatal(Direction.IN, RuntimeError("bad lang"))
            session._on_direction_fatal(Direction.OUT, RuntimeError("bad lang"))
    finally:
        session.shutdown()
    messages = [r.getMessage() for r in caplog.records if "direction is dead" in r.message]
    assert "--my-lang" in messages[0]
    assert "--their-lang" in messages[1]


class _Ticks(threading.Event):
    """A stop event whose wait() advances the fake clock, for n iterations."""

    def __init__(self, clock, n):
        super().__init__()
        self._clock, self._n = clock, n

    def wait(self, timeout=None):
        self._clock.advance(timeout)
        self._n -= 1
        if self._n <= 0:
            self.set()
        return self.is_set()


def test_a_dead_direction_is_drained_and_not_reported_as_deaf(session_args, fake_ports):
    """Its pump has stopped, but its capture keeps delivering.

    Undrained, the queue fills: drops are logged for the rest of the call and,
    once arrivals stop being accepted, the pane shows NO AUDIO ARRIVING on a
    direction that is already marked failed.
    """
    from sidetap_live.types import AudioChunk

    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()
    try:
        session._on_direction_fatal(Direction.OUT, RuntimeError("bad lang"))
        track_queue = session.capture.queues[Direction.OUT.track]
        for _ in range(5):
            track_queue.put(AudioChunk(track=Direction.OUT.track, pcm=b"", t_start=0.0))
        deadline = time.monotonic() + 2.0
        while track_queue._queue.qsize() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert track_queue._queue.qsize() == 0, "the dead direction's queue is not drained"

        session._poll_capture_health(_Ticks(fake_ports.clock, int(NO_AUDIO_S) + 5))
        state = session.metrics.snapshot().directions[Direction.OUT]
        assert state.no_audio is False, "a dead direction was reported as deaf"
    finally:
        session.shutdown()


def test_setup_fails_before_engaging_when_the_api_key_is_missing(
    session_args, fake_ports, monkeypatch
):
    """The same rule the virtmic check above establishes: nothing may be
    rewired before a fatal check, or there is nobody left to restore it.

    The key check sat after router.engage(), with a comment claiming "setup()
    has not yet engaged the router, so nothing needs undoing" - which was
    false by the time it ran. Reading an environment variable has no side
    effects, so it belongs with the other cheap checks.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    session = build_session(session_args, fake_ports, sessions=None)

    with pytest.raises(Exception, match="GEMINI_API_KEY"):
        session.setup()

    assert session.router is None, "the graph was engaged before a fatal check"


def test_a_failing_transcript_close_does_not_swallow_a_clean_shutdown(
    session_args, fake_ports, monkeypatch
):
    """Every other step in shutdown() is wrapped; this one was not.

    The router has already been restored by the time it runs, so an
    exception here - a full disk, a removed path - propagated out of
    shutdown() and out of run_session()'s finally, past the "Saved:" summary,
    making a clean exit look like a crash.
    """
    session = build_session(session_args, fake_ports, sessions=FakeSessionFactory())
    session.setup()

    def boom():
        raise OSError("No space left on device")

    monkeypatch.setattr(session.transcript, "close", boom)

    session.shutdown()          # must not raise

    assert session.router_restored is True
