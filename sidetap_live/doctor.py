"""Check the environment before a call, not during one.

A missing API key, or a Live API session that will not open, should fail in a
second at setup, rather than two minutes into a conversation with the other
party waiting.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .adapters import MIN_PW_VERSION, installed_pw_version
from .graph import PwGraph
from .routing import VIRTMIC_CONFIG, VIRTMIC_CONFIG_PATH, VIRTMIC_SINK, VIRTMIC_SOURCE

# pw-cli belongs here even though sidetap never uses it at runtime:
# check_pipewire_version() shells out to it, and without it in this list a
# machine missing only pw-cli is told PipeWire is version 0.0.0 and to
# upgrade - sending the user after a problem they do not have.
REQUIRED_TOOLS = (
    "pw-dump",
    "pw-record",
    "pw-link",
    "pw-cat",
    "pw-loopback",
    "pw-cli",
    "wpctl",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    # A check that passed but degraded something. Distinct from ok=False so
    # that a missing speech detector does not read as a broken install.
    warn: bool = False


def check_tools() -> list[Check]:
    checks = []
    for tool in REQUIRED_TOOLS:
        path = shutil.which(tool)
        checks.append(Check(tool, path is not None, path or "not found"))
    return checks


def check_pipewire_version() -> Check:
    version = installed_pw_version()
    rendered = ".".join(str(p) for p in version)
    minimum = ".".join(str(p) for p in MIN_PW_VERSION)
    if version == (0, 0, 0):
        # Not a real version. installed_pw_version() returns this sentinel
        # when pw-cli is missing or will not run, and reporting it as though
        # PipeWire were ancient points the user at an upgrade rather than at
        # the actual problem.
        return Check(
            "pipewire",
            False,
            "could not read the version - is pw-cli installed and is PipeWire "
            "running for this user?",
        )
    return Check(
        "pipewire",
        version >= MIN_PW_VERSION,
        rendered if version >= MIN_PW_VERSION else f"{rendered}, need >= {minimum}",
    )


def check_virtmic(graph: PwGraph, config_path: Path = VIRTMIC_CONFIG_PATH) -> Check:
    """Are both halves of the virtual mic present in the live graph?

    The config file is consulted so this can tell the two failures apart. They
    need different actions, and conflating them tells a user who has just run
    `--install` to run `--install` - which is how someone concludes the tool is
    broken and stops reading its output.
    """
    sink = graph.node_by_name(VIRTMIC_SINK)
    source = graph.node_by_name(VIRTMIC_SOURCE)
    if sink is not None and source is not None:
        return Check("virtual mic", True, f"{VIRTMIC_SINK} + {VIRTMIC_SOURCE}")

    missing = [
        name
        for name, node in ((VIRTMIC_SINK, sink), (VIRTMIC_SOURCE, source))
        if node is None
    ]
    if config_path.exists():
        return Check(
            "virtual mic",
            False,
            f"config is written but not loaded yet - run: "
            f"systemctl --user restart pipewire pipewire-pulse",
        )
    return Check(
        "virtual mic",
        False,
        f"missing {', '.join(missing)} - run `sidetap doctor --install` then "
        "`systemctl --user restart pipewire pipewire-pulse`",
    )


def check_linking() -> Check:
    """Does pw-link actually reach a running PipeWire session?

    check_tools() only proves the binary is on PATH. A pw-link that exists but
    cannot talk to a session - no session running, wrong XDG_RUNTIME_DIR, a
    sandbox - fails at runtime as ONE warning about six seconds in, then
    debug-level forever, while the IN direction silently never produces a
    translation for the rest of the call. `pw-link -l` needs a live session, so
    it is a cheap functional probe.
    """
    try:
        result = subprocess.run(
            [shutil.which("pw-link") or "pw-link", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("linking", False, f"pw-link unusable: {type(exc).__name__}: {exc}")
    if result.returncode != 0:
        return Check(
            "linking",
            False,
            f"pw-link -l failed ({(result.stderr or '').strip()}) - is PipeWire "
            "running for this user? Without it the remote direction stays deaf.",
        )
    return Check("linking", True, "pw-link reaches the session")


def check_api_key(env: dict[str, str] | None = None) -> Check:
    """Is there a key at all?

    This project uses GEMINI_API_KEY and nothing else. It has no GCP
    credentials, no ADC and no --project: gemini-3.5-live-translate-preview
    is Developer API only. Never print any part of the value.
    """
    import os

    environ = os.environ if env is None else env
    if not environ.get("GEMINI_API_KEY"):
        return Check(
            "api key",
            False,
            "GEMINI_API_KEY is not set. Get one at aistudio.google.com/apikey",
        )
    return Check("api key", True, "GEMINI_API_KEY is set")


def check_activity(detector=None) -> Check:
    """Can we detect pauses?

    Not fatal, unlike sidetap's equivalent. There the silence gate was the
    difference between $0.10 and $2 an hour idle and its absence was silent;
    here nothing is gated, so a missing detector costs no money. It costs two
    behaviours: session rotation can no longer wait for a pause and always
    lands mid-speech, and idle-suspend never fires.
    """
    if detector is None:
        from .activity import webrtc_detector

        detector = webrtc_detector()
    if detector is None:
        return Check(
            "speech activity",
            True,
            "webrtcvad unavailable - every session rotation will be forced "
            "and idle-suspend is disabled. Run: uv sync",
            warn=True,
        )
    return Check("speech activity", True, "webrtcvad available")


def check_live_session(factory) -> Check:
    """Open one real session and close it.

    One cheap round trip here fails in a second, rather than two minutes into
    a live conversation with the graph already rewired. `factory` is anything
    with the SessionFactory shape (`.open(target_lang, *, echo,
    handle=None)`) - production passes
    `live.build_factory(genai.Client(api_key=...))`.
    """
    try:
        session = factory.open("en", echo=False)
    except Exception as exc:
        return Check("live session", False, f"{type(exc).__name__}: {exc}")
    try:
        session.close()
    except Exception as exc:
        return Check("live session", False, f"opened but failed to close: {exc}")
    return Check("live session", True, "opened and closed a live-translate session")


def install_virtmic_config(path: Path = VIRTMIC_CONFIG_PATH) -> bool:
    """Write the config if absent. Returns True if it wrote one.

    Never clobbers: the user may have tuned the rate or the description, and
    silently reverting that would be worse than doing nothing.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(VIRTMIC_CONFIG, encoding="utf-8")
    return True


def render_report(checks: list[Check]) -> str:
    if not checks:
        return "  No checks ran."
    width = max((len(c.name) for c in checks), default=0)
    lines = []
    for check in checks:
        status = "FAIL" if not check.ok else ("WARN" if check.warn else "OK  ")
        lines.append(f"  {status}  {check.name:<{width}}  {check.detail}")
    if all(c.ok and not c.warn for c in checks):
        lines.append("")
        lines.append("  All checks passed.")
    return "\n".join(lines)
