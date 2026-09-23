import pytest

from sidetap_live.cli import build_parser, describe_graph, main
from tests.conftest import FakeClock, FakeGraphSource, FakeLauncher, FakeLinker


def test_devices_lists_sinks_sources_and_streams(zoom_graph):
    text = describe_graph(zoom_graph)
    assert "OUTPUT DEVICES" in text
    assert "INPUT DEVICES" in text
    assert "APPLICATIONS CURRENTLY PLAYING AUDIO" in text


def test_devices_suggests_the_app_flag(zoom_graph):
    assert "--app" in describe_graph(zoom_graph)


def test_devices_explains_an_empty_stream_list(idle_graph):
    # The single most common first-run confusion: an application does not
    # appear in the graph until it actually starts a stream.
    text = describe_graph(idle_graph)
    assert "start your" in text.lower()


def test_devices_hides_monitor_sources(zoom_graph):
    text = describe_graph(zoom_graph)
    monitors = [line for line in text.splitlines() if line.strip().endswith(".monitor")]
    assert monitors == []


def test_devices_command_returns_zero(idle_graph, capsys):
    code = main(
        ["devices"],
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(),
        linker=FakeLinker(),
        clock=FakeClock(),
    )
    assert code == 0
    assert "OUTPUT DEVICES" in capsys.readouterr().out


def test_doctor_command_returns_nonzero_when_a_check_fails(idle_graph, capsys, monkeypatch):
    # Hermetic, deliberately. Without these the test shells out to the real
    # pw-link and reads the developer's own GEMINI_API_KEY, so it would pass
    # or fail based on the machine it runs on rather than the code.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("sidetap_live.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "sidetap_live.doctor.subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stderr": "no session"})(),
    )
    code = main(
        ["doctor", "--no-api-check"],
        graph=FakeGraphSource(idle_graph),
        launcher=FakeLauncher(),
        linker=FakeLinker(),
        clock=FakeClock(),
    )
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_run_requires_both_languages():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--app", "zoom", "--their-lang", "ru-RU"])


def test_a_bad_pw_dump_reports_one_clean_line(capsys):
    import json

    class BrokenGraph:
        def snapshot(self):
            raise json.JSONDecodeError("bad", "", 0)

    code = main(["devices"], graph=BrokenGraph(), launcher=FakeLauncher(),
                linker=FakeLinker(), clock=FakeClock())
    assert code == 1
    assert "pw-dump" in capsys.readouterr().err


def test_an_unexpected_error_is_one_line_without_verbose(capsys):
    class ExplodingGraph:
        def snapshot(self):
            raise ValueError("kaboom")

    code = main(["devices"], graph=ExplodingGraph(), launcher=FakeLauncher(),
                linker=FakeLinker(), clock=FakeClock())
    assert code == 1
    err = capsys.readouterr().err
    assert "ValueError: kaboom" in err
    assert "Traceback" not in err


def test_verbose_reraises_for_a_real_traceback():
    class ExplodingGraph:
        def snapshot(self):
            raise ValueError("kaboom")

    with pytest.raises(ValueError):
        main(["devices", "-v"], graph=ExplodingGraph(), launcher=FakeLauncher(),
             linker=FakeLinker(), clock=FakeClock())


def test_tui_mode_does_not_log_to_a_terminal_textual_owns(tmp_path, monkeypatch):
    """Every line would otherwise be painted over as it is written.

    Including "this direction is now dead" - the one line explaining why
    nothing is being translated. A real run failed exactly that way.
    """
    import logging

    from sidetap_live import cli

    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "sidetap_live.log")
    args = build_parser().parse_args(
        ["run", "--app", "zoom", "--their-lang", "ru-RU", "--my-lang", "en-US",
         "--out", str(tmp_path)]
    )
    try:
        path, _ = cli._configure_logging(args, logging.INFO)
        assert path is not None, "TUI mode must not log to stderr"
        handlers = logging.getLogger().handlers
        assert any(isinstance(h, logging.FileHandler) for h in handlers)
        assert not any(type(h) is logging.StreamHandler for h in handlers)
    finally:
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)


def test_no_tui_still_logs_to_stderr(tmp_path, monkeypatch):
    import logging

    from sidetap_live import cli

    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "sidetap_live.log")
    args = build_parser().parse_args(
        ["run", "--app", "z", "--their-lang", "ru-RU", "--my-lang", "en-US",
         "--no-tui", "--out", str(tmp_path)]
    )
    try:
        path, session = cli._configure_logging(args, logging.INFO)
        handlers = logging.getLogger().handlers
        # stderr for the live view, AND a file for the durable copy.
        assert any(type(h) is logging.StreamHandler for h in handlers)
        assert any(isinstance(h, logging.FileHandler) for h in handlers)
        assert path is not None and path.parent == tmp_path
        assert session and path.name == f"{session}.log"
    finally:
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)


def test_verbose_raises_sidetap_live_not_every_library(tmp_path, monkeypatch):
    """Root at DEBUG turns on urllib3, asyncio and grpc debug output too.

    This is the log a user reads because the TUI has hidden everything else;
    burying this program's own lines in third-party chatter defeats the point.
    """
    import logging

    from sidetap_live import cli

    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "sidetap_live.log")
    args = build_parser().parse_args(
        ["run", "--app", "z", "--their-lang", "ru-RU", "--my-lang", "en-US",
         "--no-tui", "-v", "--out", str(tmp_path)]
    )
    try:
        cli._configure_logging(args, logging.DEBUG)
        assert logging.getLogger("sidetap_live").level == logging.DEBUG
        assert logging.getLogger().level == logging.WARNING
        # a third-party DEBUG record must not pass, a WARNING must
        assert not logging.getLogger("urllib3.connectionpool").isEnabledFor(
            logging.DEBUG
        )
        assert logging.getLogger("urllib3.connectionpool").isEnabledFor(
            logging.WARNING
        )
    finally:
        logging.getLogger("sidetap_live").setLevel(logging.NOTSET)
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)


def test_the_lag_cap_help_does_not_hardcode_the_default():
    """Written out by hand it goes on claiming 30 after the constant changes.

    Documentation that lies, with nothing to catch it.
    """
    from sidetap_live.types import LAG_CAP_S

    action = next(
        a for a in build_parser()._subparsers._group_actions[0].choices["run"]._actions
        if a.dest == "lag_cap"
    )
    assert f"{LAG_CAP_S:g}" in action.help
    assert action.default is None, "None distinguishes unset from an explicit value"


def parse(*argv):
    return build_parser().parse_args(["run", "--app", "zoom",
                                      "--their-lang", "ru-RU",
                                      "--my-lang", "en-US", *argv])


def test_the_cascade_flags_are_gone():
    for flag in ("--project", "--region", "--mt-region", "--tts-region",
                 "--mt-model", "--voice-in", "--voice-out", "--phrase",
                 "--speaking-rate-in"):
        with pytest.raises(SystemExit):
            parse(flag, "x")


def test_duck_level_defaults_to_full_replacement():
    assert parse().duck_level == 0.0
    assert parse("--duck-level", "0.2").duck_level == 0.2


def test_duck_level_is_bounded():
    with pytest.raises(SystemExit):
        parse("--duck-level", "1.5")


def test_echo_out_defaults_on_because_silence_reaches_nobody():
    assert parse().echo_out is True
    assert parse("--no-echo-out").echo_out is False


def test_idle_suspend_defaults_on():
    assert parse().idle_suspend is True
    assert parse("--no-idle-suspend").idle_suspend is False


def test_lag_cap_defaults_to_none_so_run_can_tell_unset_from_equal():
    """None rather than LAG_CAP_S, and deliberately so.

    run.py resolves None to the constant. Defaulting to the constant here
    would make "user did not pass --lag-cap" indistinguishable from "user
    passed exactly 30", and it would force the help text to restate a number
    that already lives in types.py.
    """
    from sidetap_live.types import LAG_CAP_S

    assert parse().lag_cap is None
    assert parse("--lag-cap", "12").lag_cap == 12.0
    assert LAG_CAP_S == 30.0
