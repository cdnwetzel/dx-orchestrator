"""Coverage for the wiring between commands: the --gui pipeline, the VLM HTTP
call, the CLI entry point, and the remaining config resolvers.

These are the seams where one component hands off to another. They are the
least-exercised code in the package and the most likely to rot, because nothing
in the normal test path touches them.
"""
import json
import sys

import pytest

from dx import cli
from dx.cli import build_parser
from dx.config_loader import DEFAULT_PSOPERATOR_REPO, get_psoperator_repo


class _Ok:
    returncode = 0


@pytest.fixture
def gui_run(monkeypatch):
    """`dx run --gui` with pxx stubbed out, so only the GUI handoff is exercised."""
    monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/bin/pxx")
    monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: _Ok())

    calls = {"health": True, "launch": True, "audit": True, "launched": []}

    class FakeClient:
        def observer_health(self):
            return calls["health"]

        def launch_gui_task(self, task_description, real_input=False):
            calls["launched"].append((task_description, real_input))
            return calls["launch"]

        def verify_audit_log(self):
            return calls["audit"]

    monkeypatch.setattr("dx.cmd_run.PSOperatorClient", FakeClient)
    return calls


def _run(*argv):
    args = build_parser().parse_args(["run", *argv])
    args.func(args)


class TestGuiPipeline:
    def test_gui_task_receives_the_users_message(self, gui_run, capsys):
        _run("T-G", "--required_role", "widget-engineer", "-m", "click ok",
             "--no-commit", "--no-evidence", "--gui")
        assert gui_run["launched"] == [("click ok", False)]
        out = capsys.readouterr().out
        assert "GUI task completed" in out
        assert "audit log verified" in out
        assert "Task T-G completed" in out

    def test_real_input_flag_is_forwarded(self, gui_run):
        _run("T-G", "--required_role", "widget-engineer", "-m", "type",
             "--no-commit", "--no-evidence", "--gui", "--real-input")
        assert gui_run["launched"] == [("type", True)]

    def test_unhealthy_observer_warns_but_still_attempts(self, gui_run, capsys):
        gui_run["health"] = False
        _run("T-G", "--required_role", "widget-engineer", "-m", "x", "--no-commit", "--no-evidence", "--gui")
        assert "Observer not healthy" in capsys.readouterr().out
        assert gui_run["launched"], "should still have attempted the task"

    def test_gui_failure_exits_one(self, gui_run, capsys):
        gui_run["launch"] = False
        with pytest.raises(SystemExit) as exc:
            _run("T-G", "--required_role", "widget-engineer", "-m", "x",
                 "--no-commit", "--no-evidence", "--gui")
        assert exc.value.code == 1
        assert "GUI task failed" in capsys.readouterr().err

    def test_failed_audit_verification_is_a_warning_not_a_failure(self, gui_run, capsys):
        """The GUI action already happened; dx reports the audit gap rather than
        pretending the task did not run."""
        gui_run["audit"] = False
        _run("T-G", "--required_role", "widget-engineer", "-m", "x", "--no-commit", "--no-evidence", "--gui")
        out = capsys.readouterr().out
        assert "Audit log verification failed" in out
        assert "Task T-G completed" in out

    def test_no_gui_flag_never_touches_psoperator(self, monkeypatch):
        monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/bin/pxx")
        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: _Ok())
        monkeypatch.setattr(
            "dx.cmd_run.PSOperatorClient",
            lambda: pytest.fail("PSOperator constructed without --gui"),
        )
        _run("T-N", "--no-evidence", "--required_role", "widget-engineer", "-m", "x", "--no-commit")


class TestVlmCall:
    """The HTTP shape dx sends to the vision model, and RL-007's boundary."""

    @pytest.fixture
    def posted(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def __init__(self, payload, status_ok=True):
                self._payload = payload
                self._ok = status_ok

            def raise_for_status(self):
                if not self._ok:
                    raise RuntimeError("HTTP 500")

            def json(self):
                return self._payload

        state = {"reply": "YES the dialog is shown", "ok": True}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return FakeResponse({"response": state["reply"]}, state["ok"])

        import requests

        monkeypatch.setattr(requests, "post", fake_post)
        captured["state"] = state
        return captured

    def _verify(self, expected="the dialog is shown"):
        from dx.cmd_verify import _verify_with_vlm

        return _verify_with_vlm(b"\x89PNG fake", expected, "http://vlm.invalid/api/generate", "m")

    def test_image_is_base64_encoded_in_the_payload(self, posted):
        import base64

        self._verify()
        body = posted["json"]
        assert body["model"] == "m"
        assert body["stream"] is False
        assert base64.b64decode(body["images"][0]) == b"\x89PNG fake"

    def test_expected_state_reaches_the_prompt(self, posted):
        self._verify("a green success banner")
        assert "a green success banner" in posted["json"]["prompt"]

    def test_request_has_a_timeout(self, posted):
        """A hung VLM must not hang the merge gate forever."""
        self._verify()
        assert posted["timeout"], "no timeout set on the VLM request"

    @pytest.mark.parametrize(
        "reply,expected",
        [
            ("YES it matches", True),
            ("yes it matches", True),
            ("NO it is blank", False),
            ("Maybe?", False),
            ("", False),
        ],
    )
    def test_only_an_affirmative_answer_passes(self, posted, reply, expected):
        posted["state"]["reply"] = reply
        assert self._verify()[0] is expected

    def test_http_error_is_a_failed_check_not_an_exception(self, posted):
        """RL-007: an unreachable model must fail closed, and must not crash the
        caller — dx merge treats this as one gate among several."""
        posted["state"]["ok"] = False
        passed, detail = self._verify()
        assert passed is False
        assert "VLM error" in detail


class TestCliEntryPoint:
    def test_main_dispatches_to_the_subcommand(self, monkeypatch, capsys):
        """`dx roles list` returns normally rather than calling sys.exit; main()
        must dispatch to it and let it return."""
        called = {}
        monkeypatch.setattr(sys, "argv", ["dx", "roles", "list"])
        monkeypatch.setattr(
            "dx.cmd_roles.cmd_roles_list",
            lambda args: called.update(slug_filter=args.slug, json=args.json),
        )
        cli.main()
        assert called == {"slug_filter": None, "json": False}

    def test_main_with_no_args_exits_two(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["dx"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2

    def test_module_is_executable(self):
        """`python -m dx.cli` is how the subprocess tests invoke dx."""
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            [sys.executable, "-m", "dx.cli", "--version"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root / "src")},
        )
        assert r.returncode == 0
        assert r.stdout.strip().startswith("dx ")

    def test_roles_list_json_is_machine_readable(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["dx", "roles", "list", "--json"])
        try:
            cli.main()
        except SystemExit:
            pass
        json.loads(capsys.readouterr().out)


class TestPsoperatorRepoResolution:
    def test_env_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path))
        assert get_psoperator_repo() == tmp_path

    def test_manifest_key_is_honoured(self, monkeypatch, tmp_path):
        cfg = tmp_path / "m.yml"
        cfg.write_text(f'psoperator:\n  repo: "{tmp_path / "from-manifest"}"\n', encoding="utf-8")
        monkeypatch.setenv("DX_CONFIG", str(cfg))
        monkeypatch.delenv("PSOPERATOR_REPO", raising=False)
        assert get_psoperator_repo() == tmp_path / "from-manifest"

    def test_falls_back_to_the_documented_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PSOPERATOR_REPO", raising=False)
        monkeypatch.setenv("DX_CONFIG", str(tmp_path / "absent.yml"))
        assert get_psoperator_repo() == DEFAULT_PSOPERATOR_REPO
