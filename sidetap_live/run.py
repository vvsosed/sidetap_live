"""Build a session, run it, and give the audio graph back."""

from __future__ import annotations

import logging
import queue as queue_module
import signal
import threading
import time

from .activity import OverlapWatch, SpeechActivity, webrtc_detector
from .adapters import PwCatSink, PwLoopbackFactory, WpctlVolumeControl
from .capture import CaptureConfig, CaptureError, PipeWireCapture
from .cost import Rates
from .interpreter import DirectionInterpreter, InterpreterConfig
from .live import build_factory
from .metrics import Health, Metrics
from .playout import DuckControl, Playout, earcon
from .ports import LinkResult
from .preroll import PreRoll
from .routing import JOURNAL_PATH, VIRTMIC_SINK, Router
from .transcript import EventTranscript
from .types import LAG_CAP_S, NO_AUDIO_S, TTS_RATE, Direction

log = logging.getLogger(__name__)

SHUTDOWN_JOIN_S = 3.0
# SIGHUP is closing the terminal or losing SSH. Its default action exits with
# no `finally`, leaving the call routed through the duck, which outlives us.
SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Session:
    def __init__(
        self,
        args,
        graph,
        launcher,
        linker,
        clock,
        sessions=None,
        volume=None,
        journal_path=None,
        session_name=None,
    ):
        self._args = args
        self._graph = graph
        self._launcher = launcher
        self._linker = linker
        self._clock = clock
        # The one port that reaches the network. Built lazily in setup(), so
        # constructing a Session in a test needs no API key.
        self._sessions = sessions
        # Injectable, because WpctlVolumeControl shells out to the real wpctl.
        self._volume = volume or WpctlVolumeControl()
        # Overridable so tests do not write to the real home directory.
        # Production uses routing.JOURNAL_PATH, where `doctor --repair` looks.
        self._journal_path = journal_path if journal_path is not None else JOURNAL_PATH
        # Shared with the log file so a run's three artifacts sort together.
        self._session_name = session_name

        self.metrics = Metrics()
        # Session-wide: set by Ctrl-C, or once every direction has died.
        self.stop = threading.Event()
        # Per-direction: each interpreter pump runs until its own event is
        # set, so a fatal error stops one direction without dropping the call.
        self.direction_stop = {d: threading.Event() for d in Direction}
        self.playouts: dict[Direction, Playout] = {}
        self.sinks: dict[Direction, PwCatSink] = {}
        self.interpreters: dict[Direction, DirectionInterpreter] = {}
        self.router_restored = False
        self._threads: list[threading.Thread] = []
        self._shutdown_done = False
        # The signal handler, the TUI and run_session's finally can all reach
        # shutdown(), and the TUI can call set_bypass while shutdown is tearing
        # the same links down.
        self._lifecycle_lock = threading.RLock()
        self._bypassed = False
        # Tracked here, not read off the OUT playout, because bypass
        # suppresses the same playout and the two must not be confused.
        self._muted_out = False
        self._real_mic_links: list[tuple[int, int]] = []
        # None until setup() creates them. setup() can fail before any exist,
        # and shutdown() must survive that rather than bury the original
        # error under an AttributeError.
        self.router = None
        self.transcript = None
        self.capture = None

    def setup(self) -> None:
        args = self._args

        # BEFORE the router touches anything: engage() is the first
        # irreversible act, and a fatal check should leave nothing to undo.
        snapshot = self._graph.snapshot()
        virtmic = snapshot.node_by_name(VIRTMIC_SINK)
        if virtmic is None:
            # No fallback: pw-cat with no --target plays to the default sink,
            # so the remote party would silently hear nothing.
            raise CaptureError(
                f"{VIRTMIC_SINK} does not exist, so the translation sent to "
                "the other party has nowhere to go. Run: sidetap-live doctor "
                "--install, then systemctl --user restart pipewire "
                "pipewire-pulse"
            )

        # Also checked before engage(). The value is never logged, echoed or
        # stored beyond the client.
        api_key = None
        if self._sessions is None:
            import os

            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise CaptureError(
                    "GEMINI_API_KEY is not set. Run: sidetap-live doctor"
                )

        self.router = Router(
            graph=self._graph,
            linker=self._linker,
            unlinker=self._linker,
            loopbacks=PwLoopbackFactory(self._launcher),
            journal_path=self._journal_path,
        )
        self.router.repair()
        self.router.engage(app_pattern=args.app)

        self.transcript = EventTranscript(args.out, session=self._session_name)
        # Shared by both interpreters: one estimate for the whole call.
        self.rates = Rates()

        if self._sessions is None:
            from google import genai

            self._sessions = build_factory(genai.Client(api_key=api_key))

        # One detector per track: webrtcvad adapts to the noise floor, and a
        # room microphone and processed call audio have very different ones.
        self.activity = {
            d: SpeechActivity(webrtc_detector(), self._clock) for d in Direction
        }
        self.overlap = OverlapWatch(self.activity, self._clock)

        # Where each direction's audio is PLAYED. OUT must never be None:
        # pw-cat would play to your own speakers instead.
        default_sink = snapshot.node_by_name(snapshot.default_sink or "")
        sink_targets = {
            Direction.IN: default_sink.serial if default_sink else None,
            Direction.OUT: virtmic.serial,
        }
        # Which language each direction translates INTO: what reaches your
        # ears is in your language, what reaches theirs is in theirs.
        lang_targets = {
            Direction.IN: args.my_lang,
            Direction.OUT: args.their_lang,
        }
        # One time origin for both directions, so the interleaved transcript
        # lines up.
        session_t0 = self._clock.monotonic()

        for direction in Direction:
            sink = PwCatSink(
                self._launcher, target=sink_targets[direction], rate=TTS_RATE
            )
            self.sinks[direction] = sink
            # Only IN ducks: the virtual mic carries no original to duck.
            duck = (
                DuckControl(
                    self._volume,
                    object_id=lambda: self.router.duck_id,
                    level=args.duck_level,
                )
                if direction is Direction.IN
                else None
            )
            # args.lag_cap is None when --lag-cap is unset; Playout needs a
            # number.
            lag_cap = args.lag_cap if args.lag_cap is not None else LAG_CAP_S
            playout = Playout(direction, sink, duck=duck, lag_cap_s=lag_cap)
            self.playouts[direction] = playout

            self.interpreters[direction] = DirectionInterpreter(
                InterpreterConfig(
                    direction=direction,
                    target_lang=lang_targets[direction],
                    # See InterpreterConfig.echo.
                    echo=args.echo_out if direction is Direction.OUT else False,
                    idle_suspend=args.idle_suspend,
                ),
                sessions=self._sessions,
                playout=playout,
                activity=self.activity[direction],
                metrics=self.metrics,
                clock=self._clock,
                rates=self.rates,
                preroll=PreRoll(),
                on_event=self.transcript.write,
                on_fatal=self._on_direction_fatal,
                session_t0=session_t0,
            )

        self.capture = PipeWireCapture(
            CaptureConfig(
                mic=args.mic,
                # Unused: `--app` is required, so the REMOTE track always
                # uses the app tap.
                remote=None,
                app=args.app,
                latency=args.latency,
            ),
            graph=self._graph,
            launcher=self._launcher,
            linker=self._linker,
            clock=self._clock,
        )

    def start(self) -> None:
        self.capture.start()
        # A stream that restarts mid-call must be routed through the duck.
        self._spawn(self.router.run, (self.stop,), "routing-watch")
        self._spawn(self._poll_capture_health, (self.stop,), "capture-health")

        for direction, interpreter in self.interpreters.items():
            self._spawn(
                interpreter.pump,
                (self.capture.queues[direction.track], self.direction_stop[direction]),
                f"pump-{direction.value}",
            )
            self._spawn(
                self.playouts[direction].run,
                (self.stop,),
                f"playout-{direction.value}",
            )

    def _on_direction_fatal(self, direction: Direction, exc: BaseException) -> None:
        """One direction died of a configuration error.

        Stop that direction but keep the call up: a dead IN leaves the remote
        party audible, because the duck only closes during speech. A dead OUT
        is shown as failed and logged. Stop the call only when BOTH are gone.
        """
        self.metrics.set_health(direction, session=Health.FAILED)
        # Stops this direction's pump; shutdown() sets the rest.
        self.direction_stop[direction].set()
        # Its capture keeps delivering, so drain it: a full queue would log
        # drops for the rest of the call and read as NO AUDIO.
        track_queue = self.capture.queues.get(direction.track) if self.capture else None
        if track_queue is not None:
            self._spawn(self._discard, (track_queue,), f"discard-{direction.value}")
        log.error(
            "%s direction is dead: %s. The call continues one-way; Ctrl-C and "
            "check --%s-lang.",
            direction.value,
            exc,
            # IN translates into your language, OUT into theirs.
            "my" if direction is Direction.IN else "their",
        )
        if all(event.is_set() for event in self.direction_stop.values()):
            log.error("both directions are dead; stopping")
            self.stop.set()

    def _poll_capture_health(self, stop: threading.Event) -> None:
        """Two capture failures, both invisible from anywhere else.

        Drops: DroppingQueue only logs, so a lost stretch would read as
        nobody talking.

        No audio at all: an unlinked capture node delivers zero bytes rather
        than silence, and every pane stays green. Only the arrival counter
        failing to advance shows it.
        """
        seen = {d: (0, self._clock.monotonic()) for d in Direction}
        while not stop.is_set():
            now = self._clock.monotonic()
            for direction in Direction:
                track_queue = self.capture.queues.get(direction.track)
                if track_queue is None or self.direction_stop[direction].is_set():
                    # A stopped direction is already shown as failed.
                    continue
                self.metrics.set_capture_dropped(direction, track_queue.dropped)

                count, since = seen[direction]
                if track_queue.accepted != count:
                    seen[direction] = (track_queue.accepted, now)
                    self.metrics.set_no_audio(direction, False)
                elif self._armed(direction) and now - since > NO_AUDIO_S:
                    self.metrics.set_no_audio(direction, True)
            # Here, not per block: neither direction's pump sees both tracks.
            self.metrics.set_overlap_pct(self.overlap.sample())
            stop.wait(1.0)

    def _armed(self, direction: Direction) -> bool:
        """Should silence on this track count as a fault yet?

        The microphone always captures, so OUT going quiet is a fault. IN
        counts only once a stream is routed; before that the app is simply
        not playing yet.
        """
        return direction is Direction.OUT or self.router.has_routed

    def _discard(self, track_queue) -> None:
        """Throw away a stopped direction's capture until the session ends."""
        while not self.stop.is_set():
            try:
                track_queue.get(timeout=0.25)
            except queue_module.Empty:
                pass

    def _spawn(self, target, args, name) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def set_bypass(self, value: bool) -> None:
        """Three things at once, or it does not work.

        Un-duck, link the real microphone through, and suppress playout on
        both directions so translated speech does not talk over the raw
        conversation. The interpreters keep running for the transcript.
        """
        with self._lifecycle_lock:
            self._set_bypass_locked(value)

    def _set_bypass_locked(self, value: bool) -> None:
        self._apply_suppression_locked(value)
        if value:
            # Directly, not via tick(): a playout whose sink died may never
            # tick again, and bypass must still open the duck.
            for playout in self.playouts.values():
                if playout.duck is not None:
                    playout.duck.open()
        self.metrics.set_bypassed(value)
        # Set BEFORE linking: _link_real_mic can raise partway, and shutdown
        # must still unlink what landed. Nothing journals these links. Too
        # early costs an empty cleanup pass; too late leaks the raw mic.
        if value:
            self._bypassed = True
        try:
            self._link_real_mic(value)
        finally:
            if not value and not self._real_mic_links:
                self._bypassed = False

    def set_mute_out(self, value: bool) -> None:
        """Stop sending your translated voice, without leaving the call.

        The interpreters keep running; only the OUT playout is suppressed.
        Pressed while bypassed, this changes what you come back to, not what
        bypass is doing now.
        """
        with self._lifecycle_lock:
            # Flag and metric written together, so the lit key and the next
            # keypress agree.
            self._muted_out = value
            self.metrics.set_muted_out(value)
            self._apply_suppression_locked(self._bypassed)

    def _apply_suppression_locked(self, bypassed: bool) -> None:
        """OUT is suppressed if EITHER control says so; IN only by bypass.

        Takes `bypassed` as an argument because _set_bypass_locked updates
        self._bypassed after calling this.

        Acts only on a change, because set_suppressed() flushes and would cut
        the utterance in progress short.
        """
        for direction, want in (
            (Direction.IN, bypassed),
            (Direction.OUT, bypassed or self._muted_out),
        ):
            playout = self.playouts[direction]
            if playout.suppressed != want:
                playout.set_suppressed(want)

    def _link_real_mic(self, connected: bool) -> None:
        """Wire the user's real microphone straight into the virtual mic.

        Unlinking replays what was actually linked rather than recomputing
        it, because the default source can change mid-call. A leaked link
        outlives the process, and the journal does not record it.
        """
        if not connected:
            for out_id, in_id in self._real_mic_links:
                self._linker.unlink(out_id, in_id)
            self._real_mic_links.clear()
            return
        if self._real_mic_links:
            return
        snapshot = self._graph.snapshot()
        virtmic = snapshot.node_by_name(VIRTMIC_SINK)
        mic = snapshot.node_by_name(snapshot.default_source or "")
        if virtmic is None or mic is None:
            log.warning(
                "bypass could not find the default microphone, so the other "
                "party will hear nothing from you until you toggle it back"
            )
            return
        outputs = snapshot.ports_of(mic.id, "out")
        inputs = snapshot.ports_of(virtmic.id, "in")
        failed = False
        for index, out_port in enumerate(outputs):
            if not inputs:
                break
            # Same index-pairing limit as routing: mono and stereo only.
            in_port = inputs[min(index, len(inputs) - 1)]
            pair = (out_port.id, in_port.id)
            # Recorded BEFORE the attempt and withdrawn only on a definite
            # failure: unlinking a pair never linked is harmless, but missing
            # a live one leaves the raw mic wired in for good.
            self._real_mic_links.append(pair)
            # Checked, because with playout suppressed a failed link means
            # the remote party hears silence.
            if self._linker.link(*pair) is LinkResult.FAILED:
                self._real_mic_links.remove(pair)
                failed = True
        if failed:
            log.error(
                "bypass could not connect your microphone to the virtual mic, "
                "so the other party is hearing silence. Toggle bypass off to "
                "restore the interpretation, or check `pw-link -l`."
            )

    def alarm_dead_air(self) -> None:
        """Straight to the sink, bypassing the queue.

        An alarm queued behind the backlog it warns about would arrive late.
        """
        self.sinks[Direction.IN].write(earcon())

    def _join_workers(self) -> None:
        """ONE shared deadline, not one per thread, so `router.restore()`,
        which gives the user their call audio back, is not delayed."""
        deadline = time.monotonic() + SHUTDOWN_JOIN_S
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        still_running = [t.name for t in self._threads if t.is_alive()]
        if still_running:
            log.warning(
                "threads still running at shutdown: %s - restoring the graph "
                "anyway", ", ".join(still_running)
            )

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self.stop.set()
        for event in self.direction_stop.values():
            event.set()
        with self._lifecycle_lock:
            if self._bypassed or self._real_mic_links:
                # Do not leave the real microphone wired into the virtual
                # mic, which outlives the process. _real_mic_links is checked
                # too, since a failed set_bypass can leave links behind.
                try:
                    self._link_real_mic(False)
                except Exception:
                    log.exception(
                        "could not unlink the real microphone from the virtual "
                        "mic; run: pw-link -l and remove it by hand"
                    )
        try:
            if self.capture is not None:
                self.capture.stop.set()
                self.capture.shutdown()
            self._join_workers()
        except Exception:
            # Whatever went wrong, the graph still has to go back.
            log.exception("error during shutdown; restoring the graph anyway")
        finally:
            if self.router is not None:
                # None only if setup() failed before mutating anything.
                try:
                    self.router.restore()
                    self.router_restored = True
                except Exception:
                    log.exception(
                        "could not restore the audio graph. Run: sidetap-live "
                        "doctor --repair"
                    )
            # Directly, not via the playout threads: they exist only after
            # start(), and an orphaned pw-cat survives this process.
            for sink in self.sinks.values():
                try:
                    sink.close()
                except Exception:
                    log.debug("could not close a playback sink", exc_info=True)
            if self.transcript is not None:
                # Wrapped so a full disk does not make a clean exit look like
                # a crash.
                try:
                    self.transcript.close()
                except Exception:
                    log.exception("could not write the transcript markdown")


def run_session(args, *, graph, launcher, linker, clock, sessions=None,
                session=None) -> int:
    session_obj = Session(
        args, graph, launcher, linker, clock, sessions, session_name=session,
    )

    def handle_signal(*_):
        session_obj.stop.set()

    # Installed BEFORE setup(), which mutates the graph and takes time, and
    # put back afterwards so they do not outlive this session.
    previous = {sig: signal.signal(sig, handle_signal) for sig in SHUTDOWN_SIGNALS}
    try:
        try:
            session_obj.setup()
        except BaseException:
            # setup() is not atomic: a failure after engage() would leave the
            # call routed into the duck with the process gone.
            session_obj.shutdown()
            raise

        # start() is guarded too: it runs after engage() and can fail, e.g.
        # capture.start() timing out waiting for a capture node.
        try:
            session_obj.start()

            if args.no_tui:
                _run_headless(session_obj)
            else:
                from .tui import run_tui

                run_tui(session_obj)
        finally:
            session_obj.shutdown()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    saved = [
        session_obj.transcript.jsonl_path,
        session_obj.transcript.md_path,
        session_obj.transcript.original_path,
        session_obj.transcript.translated_path,
    ]
    log_path = session_obj.transcript.jsonl_path.with_suffix(".log")
    if log_path.exists():
        saved.append(log_path)
    print("\nSaved:")
    for path in saved:
        print(f"  {path}")
    return 0


def _run_headless(session: Session) -> None:
    alarmed = False
    # Latched per direction so a standing condition is reported once rather
    # than twice a second for the rest of the call.
    deaf: dict[Direction, bool] = {d: False for d in Direction}
    while not session.stop.is_set():
        session.stop.wait(0.5)
        snapshot = session.metrics.snapshot()
        out = snapshot.directions[Direction.OUT]
        if out.dead_air and not alarmed:
            log.error("DEAD AIR: you are speaking and nothing is reaching the call")
            session.alarm_dead_air()
            alarmed = True
        elif not out.dead_air:
            alarmed = False

        # The headless counterpart of the TUI's NO AUDIO ARRIVING.
        for direction, state in snapshot.directions.items():
            if state.no_audio and not deaf[direction]:
                log.error(
                    "NO AUDIO reaching the %s direction - its capture node is "
                    "delivering nothing; check `wpctl status` and the app is "
                    "still streaming",
                    direction.value,
                )
            deaf[direction] = state.no_audio
