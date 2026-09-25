"""Textual dashboard.

Textual owns the main thread and POLLS Metrics.snapshot(). Nothing in the
pipeline calls into this module, so --no-tui and the headless tests share one
code path.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Static

# Private, but the footer key is where the user looks for a toggle's state.
# tests/test_tui.py pins the import, so a Textual release that moves it fails
# the suite.
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
    """The clean/forced split, not just a count: forced rotations are the
    ones that may have cut a word."""
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

    /* An engaged toggle. $warning, not $error: $error means something is
       wrong, and bypass and mute are deliberate.

       Both component classes set `color: $text`, because the default key
       foreground is itself amber and would vanish on $warning. */
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
        # Deferred: on_mount can fire before compose()'s children finish
        # mounting, and query_one() would race them.
        self.call_after_refresh(self.refresh_from_metrics)
        self.set_interval(1 / REFRESH_HZ, self.refresh_from_metrics)

    def refresh_from_metrics(self) -> None:
        # A tick can land mid-teardown, before the timer is cancelled, where
        # query_one would raise NoMatches.
        if not self.is_running:
            return
        # The signal handler only sets this flag. Without this check
        # App.run() never returns, so shutdown() and router.restore() never
        # run and the call stays silenced. Ctrl-C cannot stand in: Textual
        # clears ISIG, so no SIGINT is delivered.
        if self._session is not None and self._session.stop.is_set():
            self.exit()
            return
        snapshot = self._metrics.snapshot()
        for direction, state in snapshot.directions.items():
            suffix = direction.value
            self.query_one(f"#source-{suffix}", Static).update(state.source)
            self.query_one(f"#target-{suffix}", Static).update(state.target)
            # Named, not merely coloured: NO AUDIO (nothing arriving) and
            # DEAD AIR (speech in, nothing out) need different fixes.
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

        bypassed = "BYPASSED  " if snapshot.bypassed else ""
        # An em dash, not 0%: without webrtcvad this cannot be measured.
        overlap = (
            "—" if snapshot.overlap_pct is None else f"{snapshot.overlap_pct:.0f}%"
        )
        self.sub_title = f"{bypassed}overlap {overlap}  est. ${snapshot.cost_usd:.2f}"

        # The lit key shows whether OUT is actually suppressed, which is
        # what the user hears.
        muted = (
            self._session.playouts[Direction.OUT].suppressed
            if self._session is not None
            else False
        )
        self._paint_toggles({"bypass": snapshot.bypassed, "mute": muted})

    def _paint_toggles(self, engaged: dict[str, bool]) -> None:
        """Light the footer key of a toggle that is currently on.

        Re-applied every tick: Footer rebuilds its keys when bindings change
        and would drop a class set once. Keyed on the binding's action, so
        keys with no toggle state are skipped by the lookup.
        """
        for key in self.query(FooterKey):
            state = engaged.get(key.action)
            if state is not None:
                key.set_class(state, "-engaged")

    def action_bypass(self) -> None:
        """Toggle against Metrics, not a local flag that could drift from it."""
        if self._session is None:
            return
        target = not self._metrics.snapshot().bypassed
        # Off the UI thread: set_bypass calls pw-dump and pw-link, which
        # would freeze every key, quit included. Session serialises the work,
        # and the target is read here so it matches what the screen showed.
        self.run_worker(
            lambda: self._session.set_bypass(target),
            thread=True,
            group="bypass",
            name="set-bypass",
        )

    def action_mute(self) -> None:
        """Stop sending your translated voice, without leaving the call.

        Read from Metrics, NOT playout.suppressed: bypass suppresses the same
        playout, so toggling that would un-suppress OUT mid-bypass.
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
