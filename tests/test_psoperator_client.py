"""`PSOperatorClient` — the subprocess wrapper around the GUI actuator.

dx never drives a mouse itself; it shells out. These tests pin the contract at
that boundary: what gets configured, what gets executed, and that every failure
mode degrades to a False return rather than an exception, because `dx run --gui`
treats this as one step in a longer pipeline.
"""
import subprocess

import pytest

from dx.psoperator_client import PSOperatorClient, run_agent_script


@pytest.fixture
def psop_repo(monkeypatch, tmp_path):
    """A psoperator clone with a runnable run_agent.py."""
    examples = tmp_path / "psoperator" / "examples"
    examples.mkdir(parents=True)
    (examples / "run_agent.py").write_text("print('ran')\n", encoding="utf-8")
    monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path / "psoperator"))
    return tmp_path / "psoperator"


class TestConfiguration:
    def test_values_come_from_the_manifest(self):
        client = PSOperatorClient()
        assert client.observer_port == "9764"
        assert client.model_name == "test-operator"

    def test_env_overrides_the_manifest(self, monkeypatch):
        monkeypatch.setenv("PSOPERATOR_OBSERVER_PORT", "9999")
        monkeypatch.setenv("PSOPERATOR_MODEL_NAME", "override-model")
        client = PSOperatorClient()
        assert client.observer_port == "9999"
        assert client.model_name == "override-model"

    def test_documented_defaults_apply_when_unconfigured(self, monkeypatch, tmp_path):
        bare = tmp_path / "bare.yml"
        bare.write_text("roles: {}\n", encoding="utf-8")
        monkeypatch.setenv("DX_CONFIG", str(bare))
        client = PSOperatorClient()
        assert client.gatekeeper_port == "8765"
        assert client.executor_port == "8766"
        assert client.audit_log_path == "psoperator_audit.jsonl"

    def test_construction_survives_a_missing_manifest(self, monkeypatch, tmp_path):
        """dx run --gui must not crash just because no manifest exists yet."""
        monkeypatch.setenv("DX_CONFIG", str(tmp_path / "absent.yml"))
        assert PSOperatorClient().observer_port == "8764"

    def test_every_port_is_passed_to_the_subprocess_as_a_string(self):
        env = PSOperatorClient()._env()
        for var in (
            "PSOPERATOR_OBSERVER_PORT",
            "PSOPERATOR_GATEKEEPER_PORT",
            "PSOPERATOR_EXECUTOR_PORT",
            "PSOPERATOR_MODEL_ENDPOINT",
            "PSOPERATOR_MODEL_NAME",
            "PSOPERATOR_AUDIT_LOG_PATH",
        ):
            assert isinstance(env[var], str), f"{var} must be a str for os.environ"

    def test_env_inherits_the_parent_environment(self, monkeypatch):
        monkeypatch.setenv("SOME_UNRELATED_VAR", "kept")
        assert PSOperatorClient()._env()["SOME_UNRELATED_VAR"] == "kept"


class TestRunAgentResolution:
    def test_path_follows_psoperator_repo(self, psop_repo):
        assert run_agent_script() == psop_repo / "examples" / "run_agent.py"

    def test_resolved_per_call_not_at_import(self, monkeypatch, tmp_path):
        """Regression: the path was a module-level constant frozen at import, so
        setting PSOPERATOR_REPO afterwards had no effect."""
        monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path / "first"))
        first = run_agent_script()
        monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path / "second"))
        assert run_agent_script() != first


class TestLaunchGuiTask:
    def test_missing_script_returns_false_with_guidance(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path / "absent"))
        assert PSOperatorClient().launch_gui_task("do a thing") is False
        out = capsys.readouterr().out
        assert "run_agent.py not found" in out
        assert "PSOPERATOR_REPO" in out

    def test_invokes_the_script_by_absolute_path(self, psop_repo, monkeypatch):
        """psoperator's examples/ is not a package, so `python -m examples.run_agent`
        does not resolve — it must be called by path."""
        captured = {}

        class _Ok:
            returncode = 0

        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **k: (captured.update(cmd=cmd, kw=k), _Ok())[1],
        )
        assert PSOperatorClient().launch_gui_task("open the editor") is True

        cmd = captured["cmd"]
        assert cmd[1] == str(psop_repo / "examples" / "run_agent.py")
        assert "-m" not in cmd
        assert cmd[2:] == ["--task", "open the editor"]

    def test_real_input_is_opt_in(self, psop_repo, monkeypatch):
        """Driving a real mouse and keyboard must never be the default."""
        captured = {}

        class _Ok:
            returncode = 0

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **k: (captured.update(cmd=cmd), _Ok())[1]
        )
        client = PSOperatorClient()

        client.launch_gui_task("t")
        assert "--real-input" not in captured["cmd"]

        client.launch_gui_task("t", real_input=True)
        assert "--real-input" in captured["cmd"]

    def test_nonzero_exit_is_a_failure(self, psop_repo, monkeypatch):
        class _Fail:
            returncode = 3

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Fail())
        assert PSOperatorClient().launch_gui_task("t") is False

    def test_timeout_returns_false_rather_than_raising(self, psop_repo, monkeypatch, capsys):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="run_agent", timeout=1)

        monkeypatch.setattr(subprocess, "run", boom)
        assert PSOperatorClient().launch_gui_task("t") is False
        assert "timed out" in capsys.readouterr().out

    def test_timeout_is_passed_through(self, psop_repo, monkeypatch):
        captured = {}

        class _Ok:
            returncode = 0

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **k: (captured.update(k), _Ok())[1]
        )
        PSOperatorClient().launch_gui_task("t", timeout=42)
        assert captured["timeout"] == 42


class TestAuxiliaryCommands:
    def test_audit_verify_success(self, monkeypatch):
        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Ok())
        assert PSOperatorClient().verify_audit_log() is True

    def test_audit_verify_failure_is_reported(self, monkeypatch, capsys):
        class _Fail:
            returncode = 1
            stdout = ""
            stderr = "chain broken at row 4"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Fail())
        assert PSOperatorClient().verify_audit_log() is False
        assert "chain broken at row 4" in capsys.readouterr().out

    def test_audit_verify_uses_the_configured_log_path(self, monkeypatch):
        captured = {}

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **k: (captured.update(cmd=cmd), _Ok())[1]
        )
        PSOperatorClient().verify_audit_log()
        assert captured["cmd"][-1] == "psoperator_audit.jsonl"
        PSOperatorClient().verify_audit_log("/tmp/other.jsonl")
        assert captured["cmd"][-1] == "/tmp/other.jsonl"

    @pytest.mark.parametrize(
        "method", ["verify_audit_log", "observer_health"]
    )
    def test_missing_cli_degrades_to_false(self, monkeypatch, method, capsys):
        """dx must report 'PSOperator not available', never crash."""
        def missing(*a, **k):
            raise FileNotFoundError("psoperator")

        monkeypatch.setattr(subprocess, "run", missing)
        assert getattr(PSOperatorClient(), method)() is False

    def test_emergency_stop_is_safe_when_the_cli_is_absent(self, monkeypatch):
        def missing(*a, **k):
            raise FileNotFoundError("psoperator")

        monkeypatch.setattr(subprocess, "run", missing)
        PSOperatorClient().emergency_stop()  # must not raise

    def test_observer_health_reflects_exit_code(self, monkeypatch):
        for rc, expected in ((0, True), (1, False)):
            class _R:
                returncode = rc

            monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
            assert PSOperatorClient().observer_health() is expected
