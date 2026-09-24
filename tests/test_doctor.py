
from sidetap_live.doctor import (
    Check,
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
from sidetap_live.routing import VIRTMIC_SINK, VIRTMIC_SOURCE


def test_a_check_renders_with_a_marker():
    report = render_report([Check("pipewire", True, "0.3.85"), Check("wpctl", False, "not found")])
    assert "pipewire" in report
    assert "0.3.85" in report
    assert "not found" in report


def test_the_report_says_overall_pass_only_when_everything_passed():
    assert "all checks passed" in render_report([Check("a", True, "")]).lower()
    assert "all checks passed" not in render_report([Check("a", False, "")]).lower()


def test_missing_tools_are_named_individually(monkeypatch):
    monkeypatch.setattr("sidetap_live.doctor.shutil.which", lambda name: None if name == "wpctl" else "/usr/bin/" + name)
    checks = check_tools()
    failed = [c for c in checks if not c.ok]
    assert len(failed) == 1
    assert failed[0].name == "wpctl"


def test_all_tools_present_passes(monkeypatch):
    monkeypatch.setattr("sidetap_live.doctor.shutil.which", lambda name: "/usr/bin/" + name)
    assert all(c.ok for c in check_tools())


def test_virtmic_check_wants_both_halves(idle_graph, tmp_path):
    # A sink with no source means the loopback half-loaded; the messenger sees
    # no microphone at all.
    #
    # config_path is pinned rather than left to default: the default is the
    # developer's own ~/.config, so on a machine where sidetap has actually
    # been installed this test would read a real file and get the other branch.
    check = check_virtmic(idle_graph, config_path=tmp_path / "absent.conf")
    assert check.ok is False
    assert VIRTMIC_SOURCE in check.detail or VIRTMIC_SINK in check.detail


def test_a_written_but_unloaded_config_says_restart_not_install(idle_graph, tmp_path):
    """Otherwise doctor tells you to run the command you just ran.

    That is how someone concludes the tool is broken and stops reading it.
    """
    written = tmp_path / "90-sidetap-mic.conf"
    written.write_text("# installed")
    check = check_virtmic(idle_graph, config_path=written)
    assert check.ok is False
    assert "restart" in check.detail
    assert "--install" not in check.detail


def test_no_config_at_all_still_says_install(idle_graph, tmp_path):
    check = check_virtmic(idle_graph, config_path=tmp_path / "absent.conf")
    assert check.ok is False
    assert "--install" in check.detail


def test_virtmic_check_passes_when_both_nodes_exist(idle_graph):
    from dataclasses import replace

    from sidetap_live.graph import SINK, SOURCE, PwNode

    nodes = idle_graph.nodes + (
        PwNode(id=9001, serial=9001, name=VIRTMIC_SINK, description="", media_class=SINK),
        PwNode(id=9002, serial=9002, name=VIRTMIC_SOURCE, description="", media_class=SOURCE),
    )
    assert check_virtmic(replace(idle_graph, nodes=nodes)).ok is True


def test_linking_check_fails_when_pw_link_cannot_reach_a_session(monkeypatch):
    """Presence on PATH is not the same as being able to link.

    The runtime symptom is one warning six seconds in and then permanent
    silence from the remote direction, so this has to fail at setup instead.
    """

    class Result:
        returncode = 1
        stderr = "failed to connect"

    monkeypatch.setattr("sidetap_live.doctor.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("sidetap_live.doctor.subprocess.run", lambda *a, **k: Result())
    check = check_linking()
    assert check.ok is False
    assert "PipeWire" in check.detail


def test_linking_check_passes_when_pw_link_lists(monkeypatch):
    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("sidetap_live.doctor.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("sidetap_live.doctor.subprocess.run", lambda *a, **k: Result())
    assert check_linking().ok is True


def test_the_report_does_not_claim_success_when_nothing_ran():
    assert "all checks passed" not in render_report([]).lower()


def test_install_writes_the_config_when_absent(tmp_path):
    path = tmp_path / "90-sidetap-mic.conf"
    assert install_virtmic_config(path) is True
    assert VIRTMIC_SOURCE in path.read_text()


def test_install_does_not_clobber_an_existing_file(tmp_path):
    path = tmp_path / "90-sidetap-mic.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# hand-edited")
    assert install_virtmic_config(path) is False
    assert path.read_text() == "# hand-edited"


def test_install_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "a" / "b" / "90-sidetap-mic.conf"
    assert install_virtmic_config(path) is True
    assert path.exists()


def test_pw_cli_is_one_of_the_required_tools():
    """check_pipewire_version shells out to it.

    Left out of the list, a machine missing only pw-cli is told PipeWire is
    version 0.0.0 and to upgrade - which is not the problem it has.
    """
    from sidetap_live.doctor import REQUIRED_TOOLS

    assert "pw-cli" in REQUIRED_TOOLS


def test_an_unreadable_version_is_not_reported_as_version_zero(monkeypatch):
    monkeypatch.setattr("sidetap_live.doctor.installed_pw_version", lambda: (0, 0, 0))
    check = check_pipewire_version()
    assert check.ok is False
    assert "0.0.0" not in check.detail
    assert "pw-cli" in check.detail


def test_a_real_version_below_the_minimum_still_says_so(monkeypatch):
    monkeypatch.setattr("sidetap_live.doctor.installed_pw_version", lambda: (0, 3, 40))
    check = check_pipewire_version()
    assert check.ok is False
    assert "0.3.40" in check.detail


def test_a_missing_key_fails():
    check = check_api_key(env={})
    assert check.ok is False
    assert "GEMINI_API_KEY" in check.detail


def test_a_present_key_is_not_echoed():
    """Never print a credential, not even partially."""
    check = check_api_key(env={"GEMINI_API_KEY": "sk-secret-value"})
    assert check.ok is True
    assert "secret" not in check.detail


def test_a_missing_detector_warns_rather_than_fails(monkeypatch):
    """Unlike sidetap, where it multiplied the bill twentyfold in silence.

    webrtcvad-wheels is a hard dependency (see pyproject.toml), so it is
    actually installed in this environment and `webrtc_detector()` would
    otherwise succeed - the auto-detect fallback has to be forced to fail
    here rather than relied on to be absent, or this test is only
    accidentally deterministic.
    """
    monkeypatch.setattr("sidetap_live.activity.webrtc_detector", lambda *a, **k: None)
    check = check_activity(detector=None)
    assert check.ok is True
    assert check.warn is True
    assert "idle-suspend" in check.detail


def test_a_working_detector_neither_fails_nor_warns():
    check = check_activity(detector=lambda pcm: True)
    assert (check.ok, check.warn) == (True, False)


def test_a_live_session_that_opens_passes():
    """check_live_session takes a SessionFactory, not a bare callable.

    Matches ports.SessionFactory (`.open(target_lang, *, echo, handle=None)`)
    and what live.build_factory() actually returns: an object with an
    `.open()` method, not itself callable. Task 26 wires this as
    `check_live_session(build_factory(genai.Client(api_key=...)))`.
    """
    opened = []

    class _Session:
        def send(self, pcm):
            ...

        def events(self):
            return iter(())

        def close(self):
            ...

    class _Factory:
        def open(self, target_lang, *, echo, handle=None):
            opened.append(target_lang)
            return _Session()

    check = check_live_session(_Factory())
    assert check.ok is True
    assert opened == ["en"]


def test_a_live_session_that_raises_fails_with_the_reason():
    class _Factory:
        def open(self, target_lang, *, echo, handle=None):
            raise RuntimeError("PERMISSION_DENIED: model not available")

    check = check_live_session(_Factory())
    assert check.ok is False
    assert "PERMISSION_DENIED" in check.detail


def test_the_report_marks_warnings_distinctly():
    report = render_report([Check("activity", True, "degraded", warn=True)])
    assert "WARN" in report
    # A warning must not claim everything passed.
    assert "All checks passed" not in report



def test_the_live_check_probes_the_languages_the_user_named():
    """--their-lang/--my-lang were parsed and then ignored.

    The help text said "check this language too" and nothing checked it: the
    probe always opened "en". This project's own lesson from the 1007
    post-mortem is that a health check doing less than the real thing does
    not check the real thing, and the languages are the part most likely to
    be wrong.
    """
    opened = []

    class _Factory:
        def open(self, target_lang, *, echo, handle=None):
            opened.append(target_lang)

            class _S:
                def close(self_inner):
                    pass

            return _S()

    check = check_live_session(_Factory(), languages=("ru-RU", "en-US"))
    assert check.ok
    assert opened == ["ru-RU", "en-US"]


def test_the_live_check_names_the_language_that_failed():
    class _Factory:
        def open(self, target_lang, *, echo, handle=None):
            if target_lang == "xx":
                raise RuntimeError("1007 Request contains an invalid argument")

            class _S:
                def close(self_inner):
                    pass

            return _S()

    check = check_live_session(_Factory(), languages=("en", "xx"))
    assert not check.ok
    assert "xx" in check.detail
