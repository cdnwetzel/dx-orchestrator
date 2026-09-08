"""`dx verify-gui` — configuration resolution and the RL-007 boundary.

The VLM answer is advisory evidence. These tests pin that dx never invents a
host to talk to, and that the verifier's verdict is reported, not trusted as a
merge gate (dx merge still requires a GPG signature).
"""
import json

import pytest

from dx.cli import build_parser
from dx.cmd_verify import GuiConfigError, gui_target, verify_gui


def test_target_comes_from_the_manifest():
    target = gui_target()
    assert target.vlm_endpoint == "http://vlm.invalid:11434/api/generate"
    assert target.vlm_model == "test-vl:3b"
    assert target.ssh_host == "tester@vlm.invalid"


def test_env_overrides_the_manifest(monkeypatch):
    monkeypatch.setenv("DX_VLM_MODEL", "override-vl:7b")
    monkeypatch.setenv("DX_VLM_ENDPOINT", "http://override.invalid/api/generate")
    monkeypatch.setenv("DX_GUI_SSH_HOST", "someone@override.invalid")
    target = gui_target()
    assert target.vlm_model == "override-vl:7b"
    assert target.vlm_endpoint == "http://override.invalid/api/generate"
    assert target.ssh_host == "someone@override.invalid"


def test_screenshot_cmd_has_a_documented_default(monkeypatch, tmp_path):
    cfg = tmp_path / "m.yml"
    cfg.write_text(
        "gui_verification:\n"
        '  vlm_endpoint: "http://x.invalid/api/generate"\n'
        '  vlm_model: "m"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DX_CONFIG", str(cfg))
    assert gui_target().screenshot_cmd == "import -window root -"


class TestNoHostDefaults:
    """Regression: a real lab IP and SSH username were baked in as fallbacks, so
    an unconfigured install would silently SSH to someone else's machine.
    """

    @pytest.fixture
    def empty_manifest(self, monkeypatch, tmp_path):
        cfg = tmp_path / "empty.yml"
        cfg.write_text("roles: {}\n", encoding="utf-8")
        monkeypatch.setenv("DX_CONFIG", str(cfg))
        return cfg

    def test_missing_endpoint_is_an_error_not_a_default(self, empty_manifest):
        with pytest.raises(GuiConfigError) as exc:
            gui_target()
        assert "vlm_endpoint" in str(exc.value)
        assert "no default host" in str(exc.value)

    def test_error_names_the_manifest_being_read(self, empty_manifest):
        with pytest.raises(GuiConfigError) as exc:
            gui_target()
        assert str(empty_manifest) in str(exc.value)

    def test_missing_ssh_host_blocks_ssh_capture(self, monkeypatch, tmp_path):
        cfg = tmp_path / "m.yml"
        cfg.write_text(
            "gui_verification:\n"
            '  vlm_endpoint: "http://x.invalid/api/generate"\n'
            '  vlm_model: "m"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("DX_CONFIG", str(cfg))
        gui_target()  # fine without SSH
        with pytest.raises(GuiConfigError) as exc:
            gui_target(require_ssh=True)
        assert "ssh_host" in str(exc.value)
        assert "--screenshot" in str(exc.value)

    def test_verify_gui_reports_config_error_rather_than_raising(self, empty_manifest):
        """cmd_merge calls this; it must degrade to a failed check, not a crash."""
        passed, detail = verify_gui("anything")
        assert passed is False
        assert "config error" in detail

    def test_no_ssh_is_attempted_without_configuration(self, empty_manifest, monkeypatch):
        monkeypatch.setattr(
            "dx.cmd_verify.subprocess.run",
            lambda *a, **k: pytest.fail("ssh was invoked with no configured host"),
        )
        assert verify_gui("anything")[0] is False


def test_capture_error_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ssh: connect to host vlm.invalid port 22: No route to host")

    monkeypatch.setattr("dx.cmd_verify._capture_ssh", boom)
    passed, detail = verify_gui("anything")
    assert passed is False
    assert "capture error" in detail
    assert "No route to host" in detail


def test_vlm_yes_and_no_are_both_honoured(monkeypatch, tmp_path, capsys):
    shot = tmp_path / "screen.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    for answer, expected_pass in (("YES — the dialog is shown", True), ("NO — blank", False)):
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, _a=answer: (_a.startswith("YES"), _a),
        )
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json"]
        )
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is expected_pass
        assert exc.value.code == (0 if expected_pass else 1)


def test_missing_screenshot_file_exits_one(capsys, tmp_path):
    args = build_parser().parse_args(
        ["verify-gui", "--screenshot", str(tmp_path / "nope.png"), "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_psoperator_snapshot_dir_is_overridable(monkeypatch, tmp_path, capsys):
    """Never read another user's snapshot directory in tests or in CI."""
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    monkeypatch.setenv("PSOPERATOR_SNAPSHOT_DIR", str(snaps))
    args = build_parser().parse_args(["verify-gui", "--use-psoperator", "--json"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert "no PSOperator snapshots found" in json.loads(capsys.readouterr().out)["error"]
