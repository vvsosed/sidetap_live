import pytest

from sidetap_live.activity import SpeechActivity
from sidetap_live.cost import Rates
from sidetap_live.interpreter import DirectionInterpreter, InterpreterConfig
from sidetap_live.metrics import Metrics
from sidetap_live.playout import Playout
from sidetap_live.preroll import PreRoll
from sidetap_live.types import (
    BLOCK_BYTES,
    FATAL_OPEN_FAILURES,
    IDLE_SUSPEND_S,
    OVERLAP_MAX_S,
    REOPEN_BACKOFF_S,
    AudioChunk,
    Direction,
    SessionState,
)
from tests.conftest import FakeAudioSink, FakeClock, FakeSessionFactory

SPEECH = b"\x00\x40" * (BLOCK_BYTES // 2)
SILENCE = b"\x00\x00" * (BLOCK_BYTES // 2)


def build(*, echo=False, idle_suspend=True, speaking=True):
    clock = FakeClock()
    sessions = FakeSessionFactory()
    metrics = Metrics()
    playout = Playout(Direction.IN, FakeAudioSink())
    detector = (lambda pcm: pcm == SPEECH) if speaking else None
    interpreter = DirectionInterpreter(
        InterpreterConfig(
            direction=Direction.IN,
            target_lang="en",
            echo=echo,
            idle_suspend=idle_suspend,
        ),
        sessions=sessions,
        playout=playout,
        activity=SpeechActivity(detector, clock),
        metrics=metrics,
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )
    return interpreter, sessions, metrics, clock


def block(pcm):
    return AudioChunk(track="remote", pcm=pcm, t_start=0.0)


def test_it_starts_suspended_and_sends_nothing():
    interpreter, sessions, _, _ = build()
    assert interpreter.state is SessionState.SUSPENDED
    interpreter.feed(block(SILENCE))
    assert sessions.opens == []


def test_speech_opens_a_session_with_the_configured_target_and_echo():
    interpreter, sessions, _, _ = build(echo=True)
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING
    assert sessions.opens == [("en", True, None)]


def test_waking_replays_the_preroll_so_the_onset_is_not_lost():
    """The speech that woke the session happened before there was a session."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SPEECH))
    # Two silent blocks of pre-roll, plus the block that woke it.
    assert len(sessions.sessions[0].sent) == 3 * BLOCK_BYTES


def test_running_sends_every_block_including_silence():
    """No gate. A model reasoning over continuous audio gets continuous audio."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    sent_after_wake = len(sessions.sessions[0].sent)
    interpreter.feed(block(SILENCE))
    interpreter.feed(block(SILENCE))
    assert len(sessions.sessions[0].sent) == sent_after_wake + 2 * BLOCK_BYTES


def test_idle_suspends_and_closes_the_session():
    interpreter, sessions, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S + 1)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.SUSPENDED
    assert sessions.sessions[0].closed is True
    assert metrics.snapshot().directions[Direction.IN].session_state is SessionState.SUSPENDED


def test_no_idle_suspend_keeps_the_session_open():
    interpreter, sessions, _, clock = build(idle_suspend=False)
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S * 10)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING
    assert sessions.sessions[0].closed is False


def test_without_a_detector_it_opens_at_once_and_never_suspends():
    """Degraded mode: no webrtcvad means no pause detection, so the session is
    opened on the first block and held for the whole call."""
    interpreter, sessions, _, clock = build(speaking=False)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING
    clock.advance(IDLE_SUSPEND_S * 10)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.RUNNING


def test_input_audio_is_billed():
    interpreter, _, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    # 100 ms x 25 tokens/s x $3.50/M
    assert metrics.snapshot().cost_usd == pytest.approx(0.1 * 25 * 3.50 / 1e6, rel=1e-6)


from sidetap_live.types import AudioOut, GoAway

LOUD = b"\x00\x40" * 600       # 24 kHz s16, peak 0x4000 - reads as speech
QUIET = b"\x00\x00" * 600      # reads as silence


def goaway(interpreter, seconds=50.0):
    interpreter.note_event(GoAway(time_left_s=seconds))


def test_goaway_opens_a_replacement_at_once():
    """No waiting for a pause: the replacement needs ~3s to warm up, so it
    must start warming the moment the window opens."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.OVERLAPPING
    assert len(sessions.sessions) == 2
    assert sessions.opens[1] == ("en", False, None)


def test_both_sessions_are_fed_while_overlapping():
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    before = len(sessions.sessions[0].sent)
    interpreter.feed(block(SPEECH))
    interpreter.feed(block(SPEECH))

    assert len(sessions.sessions[0].sent) == before + 2 * BLOCK_BYTES
    assert len(sessions.sessions[1].sent) == 2 * BLOCK_BYTES


def test_the_replacements_output_is_discarded_until_it_takes_over():
    """Its first seconds translate audio the live session already spoke."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))

    interpreter.note_event_from(sessions.sessions[1], AudioOut(pcm=LOUD))
    assert interpreter._playout.backlog_s() == 0.0


def test_switch_needs_the_replacement_warm_and_the_outgoing_silent():
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions

    # Outgoing still speaking: no switch even though the replacement is warm.
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    interpreter.note_event_from(old, AudioOut(pcm=LOUD))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.OVERLAPPING
    assert old.closed is False

    # Outgoing falls silent: switch.
    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING
    assert old.closed is True


def test_a_cold_replacement_does_not_take_over_however_quiet_it_is():
    """Switching to a session that is not yet producing reintroduces the
    3-second hole this design exists to remove."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, _ = sessions.sessions

    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.OVERLAPPING
    assert old.closed is False


def test_the_overlap_is_bounded_and_a_bounded_switch_counts_as_forced():
    interpreter, sessions, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    # Outgoing never falls silent.
    interpreter.note_event_from(old, AudioOut(pcm=LOUD))
    clock.advance(OVERLAP_MAX_S + 1)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.RUNNING
    assert old.closed is True
    state = metrics.snapshot().directions[Direction.IN]
    assert (state.rotations, state.forced_rotations) == (1, 1)


def test_a_clean_switch_is_not_counted_as_forced():
    interpreter, sessions, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))

    state = metrics.snapshot().directions[Direction.IN]
    assert (state.rotations, state.forced_rotations) == (1, 0)
    assert state.replayed_s == 0.0


def test_rotation_replays_no_preroll():
    """The replacement has been listening for seconds; there is nothing it
    missed. Only an idle-suspend wake replays."""
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions
    sent_before_switch = len(new.sent)
    interpreter.note_event_from(new, AudioOut(pcm=LOUD))
    interpreter.note_event_from(old, AudioOut(pcm=QUIET))
    interpreter.feed(block(SPEECH))

    # Exactly one more block - the one that drove the switch. No replay burst.
    assert len(new.sent) == sent_before_switch + BLOCK_BYTES


from sidetap_live.metrics import Health
from sidetap_live.types import (
    DEAD_AIR_S,
    TTS_BYTES_PER_S,
    Closed,
    ResumptionHandle,
    SourceText,
    TargetText,
)


def build_with_events(**kwargs):
    events = []
    interpreter, sessions, metrics, clock = build(**kwargs)
    interpreter._on_event = events.append
    return interpreter, sessions, metrics, clock, events


def test_audio_out_reaches_playout_and_is_billed():
    interpreter, _, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=b"\x00" * TTS_BYTES_PER_S))

    assert interpreter._playout.backlog_s() == pytest.approx(1.0)
    state = metrics.snapshot().directions[Direction.IN]
    assert state.backlog_s == pytest.approx(1.0)
    # 1 s of output at 25 tokens/s x $21/M, on top of the input already billed.
    assert metrics.snapshot().cost_usd > 25 * 21.0 / 1e6 * 0.9


def test_both_transcriptions_become_separate_events():
    interpreter, _, metrics, _, events = build_with_events()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(SourceText(text="privet"))
    interpreter.note_event(TargetText(text="hello"))

    assert [(e.kind, e.text) for e in events] == [
        ("source", "privet"),
        ("target", "hello"),
    ]
    state = metrics.snapshot().directions[Direction.IN]
    assert (state.source, state.target) == ("privet", "hello")


def test_offset_is_speech_onset_to_first_audio_out():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(1.4)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].offset_s == pytest.approx(1.4)


def test_offset_measures_the_stretch_not_every_chunk():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(1.4)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    clock.advance(5.0)
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].offset_s == pytest.approx(1.4)


def test_dead_air_fires_when_speech_produces_nothing():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(DEAD_AIR_S + 1)
    interpreter.feed(block(SPEECH))
    assert metrics.snapshot().directions[Direction.IN].dead_air is True


def test_audio_out_clears_the_dead_air_alarm():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(DEAD_AIR_S + 1)
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=b"\x00" * 100))
    assert metrics.snapshot().directions[Direction.IN].dead_air is False


def test_a_dead_session_is_reopened_on_the_last_handle():
    interpreter, sessions, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(ResumptionHandle(handle="h-9"))
    interpreter.note_event(Closed(reason="boom"))
    assert metrics.snapshot().directions[Direction.IN].session is Health.OK

    interpreter.feed(block(SPEECH))
    assert len(sessions.sessions) == 2
    assert sessions.opens[1] == ("en", False, "h-9")


def test_a_dead_session_while_suspended_is_not_reopened():
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    clock.advance(IDLE_SUSPEND_S + 1)
    interpreter.feed(block(SILENCE))
    assert interpreter.state is SessionState.SUSPENDED

    interpreter.note_event(Closed(reason="closed by us"))
    interpreter.feed(block(SILENCE))
    assert len(sessions.sessions) == 1


def test_goaway_arriving_as_an_event_enters_overlapping():
    # NOTE: the plan's Task 21 text names this test "...enters_draining" and
    # asserts SessionState.DRAINING, a state that does not exist - it is the
    # pre-reconciliation name for what Task 20's make-before-break rewrite
    # renamed to OVERLAPPING (see the plan's own Task 20 section, "In feed(),
    # replace the DRAINING branch", and SessionState's docstring). Every other
    # rotation test in this file already asserts OVERLAPPING for this same
    # transition. Corrected here rather than left permanently red; see the
    # Task 21 report for the full disagreement.
    interpreter, _, _, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(GoAway(time_left_s=30.0))
    assert interpreter.state is SessionState.OVERLAPPING


def test_a_death_mid_overlap_promotes_the_replacement_instead_of_orphaning_it():
    """Regression: the replacement was left fed, billed and never closed.

    feed() sends to _pending for as long as it is set, so a replacement that
    is neither promoted nor closed keeps consuming audio and money for the
    rest of the call, with its receive thread still alive. The window this
    happens in is 50s every ~9 minutes.
    """
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(GoAway(time_left_s=50.0))
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.OVERLAPPING
    replacement = interpreter._pending

    interpreter.note_event(Closed(reason="outgoing died"))
    interpreter.feed(block(SPEECH))

    # Promoted, not orphaned: no third session, nothing left dangling.
    assert len(sessions.sessions) == 2
    assert interpreter.state is SessionState.RUNNING
    assert interpreter._pending is None
    assert replacement.closed is False          # it is the live one now

    sent = len(replacement.sent)
    for _ in range(3):
        interpreter.feed(block(SPEECH))
    # Fed exactly once per block, as the on-air session - not twice, and not
    # as a leaked second consumer.
    assert len(replacement.sent) == sent + 3 * BLOCK_BYTES


class _DeadFactory:
    """A factory whose sessions never open."""

    def __init__(self):
        self.attempts = 0

    def open(self, target_lang, *, echo, handle=None):
        self.attempts += 1
        raise RuntimeError("PERMISSION_DENIED: API key revoked")


def build_dead():
    from sidetap_live.cost import Rates
    from sidetap_live.preroll import PreRoll

    clock = FakeClock()
    sessions = _DeadFactory()
    metrics = Metrics()
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.IN, target_lang="en", echo=False),
        sessions=sessions,
        playout=Playout(Direction.IN, FakeAudioSink()),
        activity=SpeechActivity(lambda pcm: True, clock),
        metrics=metrics,
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )
    return interpreter, sessions, metrics, clock


def test_a_failed_open_falls_back_to_suspended_not_stuck_in_opening():
    """OPENING is a lie after a failure, and a trap.

    Nothing re-wakes an OPENING direction and nothing sends from one, so the
    direction goes silently dead for the rest of the call with only the
    dead-air alarm ever noticing.
    """
    interpreter, _, metrics, _ = build_dead()
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.SUSPENDED
    assert metrics.snapshot().directions[Direction.IN].session is Health.FAILED


def test_a_failed_open_backs_off_instead_of_retrying_every_block():
    """Ten attempts a second at an API that just refused us is how a revoked
    key becomes a rate-limit ban."""
    from sidetap_live.types import REOPEN_BACKOFF_S

    interpreter, sessions, _, clock = build_dead()
    for _ in range(20):
        interpreter.feed(block(SPEECH))
    assert sessions.attempts == 1

    clock.advance(REOPEN_BACKOFF_S + 0.1)
    interpreter.feed(block(SPEECH))
    assert sessions.attempts == 2


def test_recovery_clears_the_backoff_and_the_health_flag():
    interpreter, sessions, metrics, clock = build_dead()
    interpreter.feed(block(SPEECH))
    assert metrics.snapshot().directions[Direction.IN].session is Health.FAILED

    working = FakeSessionFactory()
    interpreter._sessions = working
    clock.advance(__import__("sidetap_live.types",fromlist=["x"]).REOPEN_BACKOFF_S + 0.1)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.RUNNING
    assert metrics.snapshot().directions[Direction.IN].session is Health.OK


class _FlakyFactory(FakeSessionFactory):
    """Opens fine except on the nth call, which raises."""

    def __init__(self, fail_on: int):
        super().__init__()
        self._fail_on = fail_on
        self.attempts = 0

    def open(self, target_lang, *, echo, handle=None):
        self.attempts += 1
        if self.attempts == self._fail_on:
            raise RuntimeError("503 backend unavailable")
        return super().open(target_lang, echo=echo, handle=handle)


def build_flaky(fail_on):
    clock = FakeClock()
    sessions = _FlakyFactory(fail_on)
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.OUT, target_lang="ru", echo=True),
        sessions=sessions,
        playout=Playout(Direction.OUT, FakeAudioSink()),
        activity=SpeechActivity(lambda pcm: pcm == SPEECH, clock),
        metrics=Metrics(),
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
    )
    return interpreter, sessions, clock


def test_a_replacement_that_cannot_be_opened_does_not_escape_or_wedge_the_state():
    """_open() guards its connect; _open_pending() did not.

    It runs on the receive thread, whose loop logs and swallows, so a 503 at
    the nine-minute mark silently left _pending None with the state still
    RUNNING - and note_goaway() early-returns on anything but RUNNING, so
    nothing ever tried again. The session then overran GoAway's time_left and
    the server killed it with 1008, mid-conversation.
    """
    interpreter, sessions, _ = build_flaky(fail_on=2)
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING

    goaway(interpreter)  # must not raise out onto the receive thread

    assert interpreter.state is SessionState.RUNNING
    assert interpreter._pending is None


def test_a_replacement_that_failed_to_open_is_retried_before_the_window_closes():
    """A transient failure must cost a retry, not the connection."""
    interpreter, sessions, clock = build_flaky(fail_on=2)
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    assert interpreter.state is SessionState.RUNNING, "precondition: the open failed"

    clock.advance(REOPEN_BACKOFF_S + 0.1)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.OVERLAPPING
    assert interpreter._pending is not None


def test_a_replacement_that_dies_mid_overlap_is_dropped_not_promoted():
    """note_event_from handled only AudioOut and ResumptionHandle from
    _pending; everything else hit the bare `return`, so a Closed from the
    replacement was discarded.

    It therefore never warmed, OVERLAP_MAX_S expired, and _switch_if_ready
    forced the switch onto a session that was already dead. Nothing recovered
    afterwards, because that session's events() had already ended, so _dead
    was never set again: the direction went silent for the rest of the call.
    On OUT that means the remote party hears nothing at all.
    """
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.OVERLAPPING
    outgoing, replacement = sessions.sessions[0], sessions.sessions[1]

    interpreter.note_event_from(replacement, Closed(reason="1007 invalid argument"))

    assert interpreter._pending is None, "the dead replacement was kept"
    assert interpreter.state is SessionState.RUNNING
    assert interpreter._session is outgoing, "the still-live session was abandoned"

    # And the overlap must not fire later on the corpse.
    clock.advance(OVERLAP_MAX_S + 1.0)
    interpreter.feed(block(SPEECH))
    assert interpreter._session is outgoing


def test_an_endlessly_failing_open_is_eventually_reported_as_fatal():
    """Session._on_direction_fatal existed with no production caller.

    _open() falls back to SUSPENDED and retries every REOPEN_BACKOFF_S
    forever, so a permanently misconfigured --their-lang - the region-subtag
    1007, a revoked key - retried against the API for the whole call instead
    of ever saying so. The direction showed Health.FAILED, but the "both
    directions are dead, stop the call" net and the "check --their-lang"
    guidance could never fire.
    """
    fatal = []
    clock = FakeClock()
    interpreter = DirectionInterpreter(
        InterpreterConfig(direction=Direction.IN, target_lang="ru-RU", echo=False),
        sessions=_DeadFactory(),
        playout=Playout(Direction.IN, FakeAudioSink()),
        activity=SpeechActivity(lambda pcm: True, clock),
        metrics=Metrics(),
        clock=clock,
        rates=Rates(),
        preroll=PreRoll(seconds=1.0),
        on_fatal=lambda direction, exc: fatal.append((direction, exc)),
    )

    for _ in range(FATAL_OPEN_FAILURES - 1):
        interpreter.feed(block(SPEECH))
        clock.advance(REOPEN_BACKOFF_S + 0.1)
    assert fatal == [], "gave up before the retries were exhausted"

    interpreter.feed(block(SPEECH))

    assert len(fatal) == 1
    assert fatal[0][0] is Direction.IN

    # And it must not keep firing for the rest of the call.
    clock.advance(REOPEN_BACKOFF_S + 0.1)
    interpreter.feed(block(SPEECH))
    assert len(fatal) == 1


def test_audio_from_a_retired_session_is_not_played():
    """The old session's queued events keep arriving after the switch.

    note_event_from only filters _pending; anything else fell straight
    through to _dispatch. close() lets the receive thread drain whatever the
    session had already queued, so audio the outgoing session produced before
    the handover was played AFTER the replacement took over - repeating a
    sentence, which is the exact artefact discarding the replacement's early
    output exists to prevent, just from the other side.
    """
    interpreter, sessions, _, _ = build()
    interpreter.feed(block(SPEECH))
    goaway(interpreter)
    interpreter.feed(block(SPEECH))
    old, new = sessions.sessions

    interpreter.note_event_from(new, AudioOut(pcm=LOUD))     # replacement warm
    interpreter.note_event_from(old, AudioOut(pcm=QUIET))    # outgoing silent
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING
    assert interpreter._session is new

    backlog = interpreter._playout.backlog_s()
    interpreter.note_event_from(old, AudioOut(pcm=LOUD))     # queued before close

    assert interpreter._playout.backlog_s() == backlog, (
        "a retired session's audio was played over the replacement's"
    )


def test_the_replacement_is_billed_while_it_overlaps():
    """Both sessions are fed during a rotation, so both are charged for.

    feed() sends to _pending directly rather than through _send, so the
    replacement's input never reached Metrics - the estimate understated
    every rotation, and rotations are the one part of the call where the
    input bill doubles.
    """
    interpreter, sessions, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING

    # What one block costs with a single session live.
    before = metrics.snapshot().cost_usd
    interpreter.feed(block(SPEECH))
    single = metrics.snapshot().cost_usd - before
    assert single > 0, "precondition: a block costs something"

    goaway(interpreter)
    assert interpreter.state is SessionState.OVERLAPPING

    # The same block, now going to two live sessions.
    before = metrics.snapshot().cost_usd
    interpreter.feed(block(SPEECH))
    both = metrics.snapshot().cost_usd - before

    assert both == pytest.approx(single * 2, rel=1e-6), (
        "only one of the two live sessions was billed for"
    )


def test_discarded_padding_is_reported_and_still_billed():
    """Padding is charged for whether or not we keep it.

    The model generated those bytes and billed them; the drain is our choice
    after the fact. Billing what survived would under-report a call by the
    majority of its output - 63% of the bytes on the measured 2026-10-05 run
    - and the figure on screen is what the user decides whether to keep
    talking on.

    `squelched_s` has to reach Metrics separately from `dropped_s` too: the
    dashboard reports the latter as audio lost, and this is routine.
    """
    from sidetap_live.types import TARGET_LATENCY_S

    interpreter, _, metrics, _ = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=QUIET * 120))        # 3 s of padding

    state = metrics.snapshot().directions[Direction.IN]
    assert state.squelched_s > 0.0, "the drain never reached Metrics"
    assert state.dropped_s == 0.0, "padding was reported as lost audio"
    assert state.backlog_s <= TARGET_LATENCY_S
    # Billed on the 3 s sent, not the ~1 s that survived the drain. Taken
    # from the interpreter's own rates, so a price change cannot quietly
    # lower this bound below the figure it is meant to catch.
    expected = interpreter._rates.output_usd(3.0)
    assert metrics.snapshot().cost_usd >= expected, (
        f"billed {metrics.snapshot().cost_usd} for 3 s of output, "
        f"expected at least {expected}"
    )


def test_the_audio_probe_sees_every_chunk_with_the_target_text_timing(tmp_path):
    """The measurement has to pair energy with the model's own speaking signal.

    Energy alone could not separate padding from speech on the 2026-10-06
    call - the keep-alive stream crossed SPEECH_PEAK every ~100 ms. Whether a
    target-text event arrived recently is a signal that does not depend on
    amplitude, so the probe is useless unless the two are recorded together.
    """
    import json

    from sidetap_live.probe import AudioProbe

    path = tmp_path / "probe.jsonl"
    interpreter, _, _, clock = build()
    interpreter._probe = AudioProbe(path, flush_every=1)
    interpreter.feed(block(SPEECH))

    interpreter.note_event(TargetText(text="hello"))
    interpreter.note_event(AudioOut(pcm=LOUD))
    clock.advance(2.5)
    interpreter.note_event(AudioOut(pcm=QUIET))
    interpreter._probe.close()

    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(entries) == 2, entries
    assert entries[0]["tt"] == pytest.approx(0.0), "target text had just arrived"
    assert entries[1]["tt"] == pytest.approx(2.5), "2.5 s of silence since it"
    assert max(entries[0]["peaks"]) > max(entries[1]["peaks"])


def test_the_probe_is_off_unless_asked_for():
    """It writes a file per call and costs work on the receive thread."""
    interpreter, _, _, _ = build()
    assert interpreter._probe is None
    interpreter.feed(block(SPEECH))
    interpreter.note_event(AudioOut(pcm=LOUD))      # must not raise


# ---------- suspending a direction whose output nobody wants ----------


def suppressed(interpreter, clock, *, seconds=None):
    """Mark output unwanted and let the grace period pass."""
    from sidetap_live.types import SUPPRESSED_SUSPEND_S

    interpreter.set_output_wanted(False)
    clock.advance(SUPPRESSED_SUSPEND_S if seconds is None else seconds)


def test_a_long_mute_suspends_the_session_instead_of_paying_for_it():
    """Mute suppresses playout; it used to leave the session open and billing.

    Measured on the 2026-10-09 call: muted for ~30 minutes, OUT still took
    delivery of 2,129 s of translated audio that was discarded on arrival -
    about $1.12 at $21.00/M out and 25 tokens/s. IDLE_SUSPEND_S never fires
    while you are still talking, so nothing closed it.
    """
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    assert interpreter.state is SessionState.RUNNING

    suppressed(interpreter, clock)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.SUSPENDED
    assert sessions.sessions[0].closed, "the session was left open while muted"


def test_a_short_bypass_does_not_churn_the_session():
    """Bypass is often seconds long - four on one call, two under 10 s.

    A fresh session emits nothing for ~3 s, so closing and reopening around a
    brief bypass costs dead air on return to save a fraction of a cent.
    """
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))

    suppressed(interpreter, clock, seconds=2.0)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.RUNNING
    assert not sessions.sessions[0].closed


def test_a_suspended_direction_stops_billing():
    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    suppressed(interpreter, clock)
    interpreter.feed(block(SPEECH))

    before = metrics.snapshot().cost_usd
    for _ in range(50):                          # 5 s of muted speech
        interpreter.feed(block(SPEECH))
    assert metrics.snapshot().cost_usd == before, "still billing while suspended"


def test_releasing_suppression_reopens_without_waiting_for_speech():
    """Otherwise unmuting costs the ~3 s a fresh session takes to find its
    voice, on top of however long the user takes to start talking. Opening on
    release instead hides the warm-up inside their reaction time."""
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    suppressed(interpreter, clock)
    interpreter.feed(block(SPEECH))
    opened = len(sessions.sessions)

    interpreter.set_output_wanted(True)
    interpreter.feed(block(SILENCE))             # NOT speech

    assert len(sessions.sessions) == opened + 1, "waited for speech to reopen"


def test_audio_recorded_while_muted_is_never_sent_after_unmute():
    """Mute means do not transmit this, and the pre-roll spans the boundary.

    feed() fills the pre-roll whatever the state, and _open(replay=True)
    drains it into the new session - so without clearing it, unmuting sends
    the model up to PREROLL_S of what was said while muted, and the remote
    party hears a translation of it.
    """
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    suppressed(interpreter, clock)
    interpreter.feed(block(SPEECH))

    secret = b"\x11\x22" * 1600                  # 100 ms, not the speech token
    interpreter.feed(block(secret))

    interpreter.set_output_wanted(True)
    interpreter.feed(block(SPEECH))

    sent = bytes(sessions.sessions[-1].sent)
    assert secret not in sent, "audio from the muted window was transmitted"


def test_unmuting_does_not_raise_a_false_dead_air_alarm():
    """_speech_at is set while muted and nothing ever clears it, because no
    output arrives to clear it - so the first feed after unmuting looks like
    six seconds of speech with nothing coming out, and sounds the earcon."""
    from sidetap_live.types import DEAD_AIR_S

    interpreter, _, metrics, clock = build()
    interpreter.feed(block(SPEECH))
    suppressed(interpreter, clock)
    interpreter.feed(block(SPEECH))
    clock.advance(DEAD_AIR_S + 1.0)              # muted, and still talking
    interpreter.feed(block(SPEECH))

    interpreter.set_output_wanted(True)
    interpreter.feed(block(SPEECH))
    interpreter.feed(block(SPEECH))

    assert metrics.snapshot().directions[Direction.IN].dead_air is False


def test_suspending_for_suppression_drops_a_warming_replacement():
    """A replacement opened for a rotation bills too, and _close only ever
    touches the session on air."""
    interpreter, sessions, _, clock = build()
    interpreter.feed(block(SPEECH))
    interpreter.note_event(GoAway(time_left_s=50.0))
    assert len(sessions.sessions) == 2, "no replacement was opened"

    suppressed(interpreter, clock)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.SUSPENDED
    assert sessions.sessions[1].closed, "the replacement was left open, billing"


def test_no_idle_suspend_keeps_the_session_through_a_mute():
    """--no-idle-suspend is an explicit choice to pay for responsiveness."""
    interpreter, sessions, _, clock = build(idle_suspend=False)
    interpreter.feed(block(SPEECH))
    suppressed(interpreter, clock, seconds=600.0)
    interpreter.feed(block(SPEECH))

    assert interpreter.state is SessionState.RUNNING
    assert not sessions.sessions[0].closed
