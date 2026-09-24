"""Textual dashboard.

Textual owns the main thread's event loop and POLLS Metrics.snapshot() on an
interval. Nothing in the pipeline calls into this module - that one-way
dependency is what makes --no-tui and the headless test suite the same code
path rather than a second implementation.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Static

# Private on purpose: Textual exports Footer but not the per-key widget it
# builds, and a footer key is the only place a toggle's state can be shown
# where the user already looks for it. tests/test_tui.py asserts the import
# and the resulting colour, so a Textual release that moves this fails the
# suite rather than silently leaving the key unlit.
from textual.widgets._footer import FooterKey

from .metrics import Health, Metrics
from .types import Direction, SessionState

REFRESH_HZ = 10

MARKERS = {Health.OK: "●", Health.RETRYING: "◐", Health.FAILED: "○"}
TITLES = {Direction.IN: "THEM → you", Direction.OUT: "YOU → them"}


def health_marker(health: Health) -> str:
    return MARKERS[health]


def format_lag(seconds: float) -> str:
    return f"{seconds:.1f}s"


def format_rotations(total: int, forced: int) -> str:
    """The clean/forced split, not just a count.

    A call that rotated six times cleanly behaved as designed; one that forced
    four of six did not, and the spec's pause assumption is what needs
    revisiting. A bare total hides the difference.
    """
    if total == 0:
        return "—"
    return f"{total}" if forced == 0 else f"{total} ({forced} forced)"


def format_state(state: SessionState) -> str:
    return state.value


class SidetapLiveApp(App):
    CSS = """
    Screen { layout: vertical; }
    .pane { border: round $primary; padding: 1 2; height: 1fr; }
    .pane.alarm { border: heavy $error; }
    .title { text-style: bold; }
    .interim { color: $text-muted; }
    .target { text-style: bold; }
    .stats { color: $text-muted; }

    /* An engaged toggle. $warning, not $error: .pane.alarm owns $error for
       "something is wrong", and bypass and mute are things the user did on
       purpose.

       Both component classes have to be named, and NOT because of the
       background - FooterKey's own background does reach them. It is the
       foreground: $footer-key-foreground is itself amber in the default
       theme, so a rule that set only the background paints the key letter
       #ffa62b on a #fea62b fill and the letter vanishes. gruvbox and nord
       are nearly as bad. $text re-resolves against the new background, which
       is what keeps the key readable on every built-in theme. */
    FooterKey.-engaged {
        background: $warning;
        .footer-key--key { background: $warning; color: $text; text-style: bold; }
        .footer-key--description { background: $warning; color: $text; text-style: bold; }
    }
    """

    BINDINGS = [
        ("b", "bypass", "Bypass"),
        ("m", "mute", "Mute out"),
        ("f", "flush", "Drop backlog"),
        ("q", "quit_session", "Quit"),
    ]

    def __init__(self, metrics: Metrics, session):
        super().__init__()
        self._metrics = metrics
        self._session = session

    def compose(self) -> ComposeResult:
        for direction in Direction:
            suffix = direction.value
            yield Vertical(
                Static(TITLES[direction], classes="title", id=f"title-{suffix}"),
                Static("", classes="interim", id=f"source-{suffix}"),
                Static("", classes="target", id=f"target-{suffix}"),
                Static("", classes="stats", id=f"stats-{suffix}"),
                classes="pane",
                id=f"pane-{suffix}",
            )
        yield Footer()

    def on_mount(self) -> None:
        # Deferred rather than called straight away: on_mount can fire before
        # compose()'s children have finished mounting, and an immediate call
        # here intermittently raced query_one() against the still-mounting
        # DOM. call_after_refresh runs once the screen has settled.
        self.call_after_refresh(self.refresh_from_metrics)
        self.set_interval(1 / REFRESH_HZ, self.refresh_from_metrics)

    def refresh_from_metrics(self) -> None:
        # The polling interval keeps ticking until Textual gets around to
        # cancelling it, which happens slightly after the app stops running
        # during shutdown - so a tick can still land here mid-teardown, after
        # screens have started being pruned but before the timer is stopped.
        # Guarding on is_running (rather than letting query_one raise) is
        # what makes that race harmless instead of an occasional NoMatches
        # crashing whatever test happens to be tearing down at that instant.
        if not self.is_running:
            return
        # The only thing run_session()'s SIGINT/SIGTERM handler does is set
        # this flag, and _run_headless is built around polling it. Without the
        # same check here, App.run() never returned on a signal: every worker
        # thread stopped itself, but run_session()'s `finally: shutdown()` -
        # and with it router.restore() - never ran, leaving the call routed
        # through the duck and silenced until somebody pressed a key. Ctrl-C
        # cannot cover for this, because Textual clears the terminal's ISIG
        # flag while it owns the screen and no SIGINT is delivered at all.
        #
        # Polled here rather than pushed from the pipeline deliberately: this
        # module reads state and never gets called into, which is what keeps
        # --no-tui and the headless suite the same code path.
        if self._session is not None and self._session.stop.is_set():
            self.exit()
            return
        snapshot = self._metrics.snapshot()
        for direction, state in snapshot.directions.items():
            suffix = direction.value
            self.query_one(f"#source-{suffix}", Static).update(state.source)
            self.query_one(f"#target-{suffix}", Static).update(state.target)
            # The two alarms are named, not merely coloured. They point at
            # opposite ends of the pipeline - NO AUDIO means nothing is
            # arriving to work on, DEAD AIR means speech went in and nothing
            # came out - and a user who cannot tell them apart cannot act on
            # either.
            if state.no_audio:
                alarm = "  NO AUDIO ARRIVING"
            elif state.dead_air:
                alarm = "  DEAD AIR"
            else:
                alarm = ""
            self.query_one(f"#stats-{suffix}", Static).update(
                f"session {health_marker(state.session)} {format_state(state.session_state)}   "
                f"backlog {format_lag(state.backlog_s)}   "
                f"offset {format_lag(state.offset_s)}   "
                f"rot {format_rotations(state.rotations, state.forced_rotations)}   "
                f"dropped {format_lag(state.dropped_s)}/{state.capture_dropped}"
                f"{alarm}"
            )
            pane = self.query_one(f"#pane-{suffix}")
            pane.set_class(state.dead_air or state.no_audio, "alarm")

        # Overlap leads because it is the result, not a diagnostic: it is the
        # fraction of the call where both people were talking at once.
        bypassed = "BYPASSED  " if snapshot.bypassed else ""
        # An em dash, not 0%: without webrtcvad this cannot be measured, and
        # showing a confident zero for the one metric the project exists to
        # produce would be worse than showing nothing.
        overlap = (
            "—" if snapshot.overlap_pct is None else f"{snapshot.overlap_pct:.0f}%"
        )
        self.sub_title = f"{bypassed}overlap {overlap}  est. ${snapshot.cost_usd:.2f}"

        # Mute has no Metrics mirror of its own - unlike bypass, which the
        # session already writes into Metrics on its way to the playouts.
        # `playout.suppressed` on the OUT direction IS the truth here, so the
        # toggle reads it the same way action_flush already reaches into
        # session.playouts, rather than inventing a second copy in Metrics
        # for a value the TUI is the only reader of.
        muted = (
            self._session.playouts[Direction.OUT].suppressed
            if self._session is not None
            else False
        )
        self._paint_toggles({"bypass": snapshot.bypassed, "mute": muted})

    def _paint_toggles(self, engaged: dict[str, bool]) -> None:
        """Light the footer key of a toggle that is currently on.

        Re-applied every tick rather than once per keypress, because Footer
        rebuilds its FooterKey children from scratch whenever screen bindings
        change (bindings_changed -> recompose) and would drop a class set
        once. Polling is also what keeps the key honest: it shows what Metrics
        says, not what this app believes it asked for.

        Keyed on the binding's action, so keys with no toggle state - flush,
        quit, Textual's own command palette - are skipped by the lookup
        rather than by a list here that could fall out of date.
        """
        for key in self.query(FooterKey):
            state = engaged.get(key.action)
            if state is not None:
                key.set_class(state, "-engaged")

    def action_bypass(self) -> None:
        """Toggle against Metrics, not against a flag kept here.

        A local mirror is a second copy of the truth that nothing reconciles:
        if set_bypass raises partway, or anything else ever changes the
        session's state, the mirror and the snapshot disagree and the next
        press does the opposite of what the screen shows.
        """
        if self._session is None:
            return
        target = not self._metrics.snapshot().bypassed
        # Off the UI thread. set_bypass reaches _link_real_mic, which takes a
        # pw-dump snapshot (timeout 10 s) and one or two pw-link calls (5 s
        # each). Inline, that froze the whole dashboard - no repaint, no other
        # key, not even quit - for as long as PipeWire took to answer, which
        # is precisely when it would be slow. Session serialises the work
        # under its own lifecycle lock, so queued presses are safe; the toggle
        # is read here, on the UI thread, so it still reflects what the screen
        # showed when the key was pressed.
        self.run_worker(
            lambda: self._session.set_bypass(target),
            thread=True,
            group="bypass",
            name="set-bypass",
        )

    def action_mute(self) -> None:
        """Stop sending your translated voice, without leaving the call.

        Routed through Session and read back from Metrics, NOT from
        playout.suppressed. Bypass suppresses the same OUT playout, so
        `not playout.suppressed` asks the wrong question while bypassed and
        would un-suppress OUT mid-bypass - putting translated speech over the
        unmediated conversation bypass exists to step out of.

        Keeping the read on Metrics also preserves the one-way dependency
        that makes --no-tui and the headless suite the same code path: this
        module polls a snapshot and never reaches into the pipeline's state.
        """
        if self._session is not None:
            self._session.set_mute_out(not self._metrics.snapshot().muted_out)

    def action_flush(self) -> None:
        if self._session is not None:
            for playout in self._session.playouts.values():
                playout.flush()

    def action_quit_session(self) -> None:
        if self._session is not None:
            self._session.stop.set()
        self.exit()


def run_tui(session) -> None:
    SidetapLiveApp(metrics=session.metrics, session=session).run()
