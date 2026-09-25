"""Command line entry point and wiring."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from .adapters import (
    MissingToolError,
    PwDumpGraphSource,
    PwLinkLinker,
    SubprocessLauncher,
    SystemClock,
)
from .capture import CaptureError
from .graph import PLAYBACK_STREAM, SINK, SOURCE, PwGraph
from .ports import Clock, GraphSource, Linker, ProcessLauncher
from .types import IDLE_SUSPEND_S, LAG_CAP_S

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidetap-live",
        description="Real-time two-way voice interpretation for any call.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    devices = sub.add_parser(
        "devices", help="list sinks, sources and apps currently playing audio"
    )
    # Every subcommand takes -v: main()'s catch-all uses it to choose between
    # one clean line and a real traceback.
    devices.add_argument("-v", "--verbose", action="store_true")

    doctor = sub.add_parser("doctor", help="check the environment before a call")
    doctor.add_argument(
        "--install", action="store_true", help="write the virtual-mic config if absent"
    )
    doctor.add_argument(
        "--repair", action="store_true", help="restore the audio graph after a crash"
    )
    doctor.add_argument(
        "--no-api-check", action="store_true", help="skip opening a live session"
    )
    # The live-session probe opens one session per language named; without
    # them it probes "en" only, which proves the key and not the languages.
    doctor.add_argument(
        "--their-lang", metavar="BCP47", help="open a live session for this language too"
    )
    doctor.add_argument(
        "--my-lang", metavar="BCP47", help="open a live session for this language too"
    )
    doctor.add_argument("-v", "--verbose", action="store_true")

    run = sub.add_parser(
        "run",
        help="start interpreting",
        epilog="Note: there is no --phrase flag. sidetap boosts recognition "
        "of names and jargon through Speech-to-Text phrase hints; this "
        "single speech-to-speech model exposes no equivalent, so names are "
        "at the model's mercy.",
    )

    sources = run.add_argument_group("audio sources")
    sources.add_argument(
        "--app",
        metavar="NAME",
        required=True,
        help="tap this application's audio (e.g. zoom, viber). "
        "Run `sidetap-live devices` mid-call to see the options.",
    )
    sources.add_argument("--mic", help="microphone node name or substring")
    sources.add_argument(
        "--latency", default="100ms", help="PipeWire stream latency (default 100ms)"
    )

    langs = run.add_argument_group("languages")
    langs.add_argument(
        "--their-lang", required=True, metavar="BCP47",
        help="what the remote party speaks, e.g. ru-RU",
    )
    langs.add_argument(
        "--my-lang", required=True, metavar="BCP47",
        help="what you speak, e.g. en-US",
    )

    out = run.add_argument_group("output")
    out.add_argument("--out", type=Path, default=Path("transcripts"))
    # default=None, not LAG_CAP_S, so run.py can tell "unset" from a value
    # and the constant stays the single source of the default. The help text
    # derives from it for the same reason.
    out.add_argument(
        "--lag-cap",
        type=lag_cap,
        default=None,
        metavar="SECONDS",
        help="seconds of un-spoken translation to allow before dropping the "
        f"oldest (default {LAG_CAP_S:g}). Raising it means hearing more while "
        "falling further behind; it does not stop the backlog growing.",
    )
    out.add_argument(
        "--duck-level",
        type=duck_level,
        default=0.0,
        metavar="0.0-1.0",
        help="how loud the remote party's original stays while the "
             "translation speaks: 0.0 replaces it (default), 0.2 holds it "
             "under the way an interpreting booth does",
    )
    out.add_argument(
        "--echo-out",
        dest="echo_out",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="when you already speak their language, still send synthesised "
             "audio. On by default: your real mic is never linked to the "
             "messenger, so silence means they hear nothing at all",
    )
    out.add_argument(
        "--idle-suspend",
        dest="idle_suspend",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=f"close the session after {int(IDLE_SUSPEND_S)}s of silence and "
             "reopen on speech. On by default - leaving it off bills "
             "continuously for a session nobody is using",
    )
    out.add_argument("--no-tui", action="store_true", help="plain console logging")
    out.add_argument("-v", "--verbose", action="store_true")
    return parser


# Only a fallback, for when --out cannot be written to. A session's log
# normally lands beside its own transcript.
FALLBACK_LOG_PATH = Path.home() / ".local/state/sidetap_live/sidetap_live.log"
LOG_PATH = FALLBACK_LOG_PATH


def session_name() -> str:
    """The stem shared by a session's .log, .jsonl and .md.

    One name so a run's files sort together and a log line can be matched
    to the utterance it explains. Sub-second, so runs cannot collide.
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _configure_logging(args, level: int) -> tuple[Path | None, str | None]:
    """stderr, unless Textual is about to take the terminal away.

    Textual paints over the whole screen, so under the TUI stderr would
    destroy every log line, including the one explaining why a direction
    died.

    A `run` also writes a log file next to its transcript, named after the
    same session. Returns that path and the session name.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    # -v raises this package's verbosity only, so third-party debug chatter
    # does not bury our lines. Third-party warnings still come through.
    root.setLevel(logging.WARNING)
    logging.getLogger("sidetap_live").setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if args.cmd != "run":
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)
        return None, None

    # Without the TUI, stderr is the live copy and the file the durable one.
    if getattr(args, "no_tui", False):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)

    session = session_name()
    for candidate in (Path(args.out) / f"{session}.log", FALLBACK_LOG_PATH):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(candidate, encoding="utf-8")
        except OSError:
            continue
        handler.setFormatter(fmt)
        root.addHandler(handler)
        return candidate, session

    if not root.handlers:
        # Never leave a run with nowhere to report a failure.
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    return None, session


def lag_cap(value: str) -> float:
    """Seconds of un-spoken translation to tolerate. Must be positive.

    At 0 or below Playout would trim on every submit and the user would hear
    almost nothing.
    """
    seconds = float(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("--lag-cap must be greater than 0")
    return seconds


def duck_level(value: str) -> float:
    """0.0 replaces the original entirely; 0.2 is interpreter-booth mode.

    Above 1.0 wpctl would AMPLIFY the original over the translation.
    """
    level = float(value)
    if not 0.0 <= level <= 1.0:
        raise argparse.ArgumentTypeError("--duck-level must be between 0.0 and 1.0")
    return level


def describe_graph(graph: PwGraph) -> str:
    """Human-readable graph summary. Run this while your call is live."""
    lines: list[str] = ["", "=== OUTPUT DEVICES (sinks) ==="]
    for node in graph.by_class(SINK):
        default = " [default]" if node.name == graph.default_sink else ""
        lines.append(f"  serial={node.serial:<6} {node.label}{default}")
        lines.append(f"      node.name = {node.name}")

    lines += ["", "=== INPUT DEVICES (sources / microphones) ==="]
    for node in graph.by_class(SOURCE):
        if node.name.endswith(".monitor"):
            continue
        default = " [default]" if node.name == graph.default_source else ""
        lines.append(f"  serial={node.serial:<6} {node.label}{default}")
        lines.append(f"      node.name = {node.name}")

    lines += ["", "=== APPLICATIONS CURRENTLY PLAYING AUDIO ==="]
    streams = graph.by_class(PLAYBACK_STREAM)
    if not streams:
        lines.append("  (none - start your Zoom/Viber call, then run this again)")
    for node in streams:
        lines.append(
            f"  serial={node.serial:<6} {node.app_name or '?'}"
            f"  binary={node.app_binary or '?'}  pid={node.pid}"
        )
        lines.append(f"      --app '{node.app_binary or node.app_name}'")
    lines.append("")
    return "\n".join(lines)


def _doctor(args, graph: GraphSource, launcher, linker, clock) -> int:
    from .doctor import (
        check_activity,
        check_api_key,
        check_linking,
        check_live_session,
        check_pipewire_version,
        check_tools,
        check_virtmic,
        install_virtmic_config,
        render_report,
    )
    from .routing import Router

    if args.install:
        if install_virtmic_config():
            print(
                "Wrote the virtual-mic config. Apply it with:\n"
                "  systemctl --user restart pipewire pipewire-pulse\n"
            )
        else:
            print("Virtual-mic config already exists; left untouched.\n")

    if args.repair:
        from .adapters import PwLoopbackFactory

        repaired = Router(
            graph=graph,
            linker=linker,
            unlinker=linker,
            loopbacks=PwLoopbackFactory(launcher),
        ).repair()
        print("Repaired the audio graph.\n" if repaired else "Nothing to repair.\n")

    checks = [check_pipewire_version(), *check_tools()]
    checks.append(check_virtmic(graph.snapshot()))
    checks.append(check_linking())
    api_key_check = check_api_key()
    checks.append(api_key_check)
    checks.append(check_activity())
    # Gated on the key check too, or os.environ[...] below raises KeyError
    # instead of printing the report.
    if not args.no_api_check and api_key_check.ok:
        from google import genai

        from .live import build_factory

        # The languages the user named, in order, de-duplicated; else "en".
        languages = tuple(
            dict.fromkeys(
                lang for lang in (args.their_lang, args.my_lang) if lang
            )
        ) or ("en",)
        checks.append(
            check_live_session(
                build_factory(genai.Client(api_key=os.environ["GEMINI_API_KEY"])),
                languages=languages,
            )
        )

    print(render_report(checks))
    return 0 if all(c.ok for c in checks) else 1


def main(
    argv: list[str] | None = None,
    *,
    graph: GraphSource | None = None,
    launcher: ProcessLauncher | None = None,
    linker: Linker | None = None,
    clock: Clock | None = None,
    sessions=None,
) -> int:
    args = build_parser().parse_args(argv)

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    log_path, session = _configure_logging(args, level)

    graph = graph or PwDumpGraphSource()
    launcher = launcher or SubprocessLauncher()
    linker = linker or PwLinkLinker()
    clock = clock or SystemClock()

    try:
        if args.cmd == "devices":
            print(describe_graph(graph.snapshot()))
            return 0
        if args.cmd == "doctor":
            return _doctor(args, graph, launcher, linker, clock)
        from .run import run_session

        if log_path is not None:
            print(f"Log: {log_path}")
        return run_session(
            args,
            graph=graph,
            launcher=launcher,
            linker=linker,
            clock=clock,
            session=session,
            sessions=sessions,
        )
    except KeyboardInterrupt:
        # Ctrl-C before the handler is installed, e.g. during auth.
        return 130
    except json.JSONDecodeError as exc:
        # Not a RuntimeError, so the clause below would miss it.
        print(
            f"Could not parse pw-dump output ({exc}). Is PipeWire running? "
            "Check with: pw-dump | head",
            file=sys.stderr,
        )
        return 1
    except (CaptureError, MissingToolError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Everything else the outside world throws (auth, a dead pw-dump, a
        # full disk) gets one clean line; -v re-raises for a traceback.
        if getattr(args, "verbose", False):
            raise
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
