"""Check the environment before a call, not during one.

A missing API key or a session that will not open should fail at setup, not
mid-conversation.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .adapters import MIN_PW_VERSION, installed_pw_version
from .graph import PwGraph
from .routing import VIRTMIC_CONFIG, VIRTMIC_CONFIG_PATH, VIRTMIC_SINK, VIRTMIC_SOURCE

# pw-cli is unused at runtime but check_pipewire_version() needs it; listing
# it reports a missing pw-cli as such, not as an ancient PipeWire.
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
        # The sentinel for "pw-cli missing or failed", not a real version.
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

    The config file is consulted to tell "not installed" from "installed but
    not loaded", which need different actions.
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
            "config is written but not loaded yet - run: "
            "systemctl --user restart pipewire pipewire-pulse",
        )
    return Check(
        "virtual mic",
        False,
        f"missing {', '.join(missing)} - run `sidetap-live doctor --install` then "
        "`systemctl --user restart pipewire pipewire-pulse`",
    )


def check_linking() -> Check:
    """Does pw-link actually reach a running PipeWire session?

    check_tools() only proves the binary is on PATH. A pw-link that cannot
    reach a session leaves the IN direction silently deaf at runtime.
    `pw-link -l` needs a live session, so it is a cheap functional probe.
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
    """Can we detect speech?

    Not fatal, since nothing is gated: without a detector sessions open at
    once and idle-suspend never fires.
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


def check_live_session(factory, languages: tuple[str, ...] = ("en",)) -> Check:
    """Open one real session per language and close it.

    `factory` is any SessionFactory; production passes
    `live.build_factory(genai.Client(api_key=...))`. The user's languages are
    probed because they are the part most likely to be wrong.
    """
    for language in languages:
        try:
            session = factory.open(language, echo=False)
        except Exception as exc:
            return Check(
                "live session", False, f"{language}: {type(exc).__name__}: {exc}"
            )
        try:
            session.close()
        except Exception as exc:
            return Check(
                "live session", False, f"{language}: opened but failed to close: {exc}"
            )
    return Check(
        "live session",
        True,
        "opened and closed a live-translate session for "
        + ", ".join(languages),
    )


def install_virtmic_config(path: Path = VIRTMIC_CONFIG_PATH) -> bool:
    """Write the config if absent. Returns True if it wrote one.

    Never clobbers: the user may have tuned it.
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
