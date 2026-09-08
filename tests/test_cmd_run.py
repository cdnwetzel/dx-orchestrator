"""`dx run` — the Anchored hard-block and hardware routing."""
import pytest

from dx.cli import build_parser


@pytest.fixture(autouse=True)
def _fake_pxx(monkeypatch):
    """Routing is what is under test, not whether pxx is installed."""
    monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/bin/pxx")


def _run(*argv):
    args = build_parser().parse_args(["run", *argv])
    args.func(args)


def test_dry_run_reports_the_resolved_route(capsys):
    _run("T-001", "--required_role", "widget-engineer", "-m", "hello", "--dry-run")
    out = capsys.readouterr().out
    assert "T-001" in out
    assert "http://vllm.invalid:8007" in out
    assert "test-27b" in out
    assert "vllm" in out
    assert "High" in out


def test_dry_run_uses_the_default_route_for_an_unrouted_role(capsys):
    _run("T-002", "--required_role", "rotating-reviewer", "-m", "hi", "--dry-run")
    out = capsys.readouterr().out
    assert "http://ollama.invalid:11434" in out
    assert "test-14b" in out


def test_dry_run_does_not_execute_pxx(monkeypatch):
    def explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("subprocess.run called during --dry-run")

    monkeypatch.setattr("dx.cmd_run.subprocess.run", explode)
    _run("T-003", "--required_role", "widget-engineer", "-m", "hi", "--dry-run")


def test_anchored_role_is_hard_blocked(capsys):
    """The seven Anchored roles require a named accountable human. Exit 2 is the
    documented signal, distinct from exit 1 (an ordinary error).
    """
    with pytest.raises(SystemExit) as exc:
        _run("T-BLOCK", "--required_role", "oracle-sme", "-m", "decide", "--dry-run")
    assert exc.value.code == 2

    err = capsys.readouterr().err
    assert "Anchored" in err
    assert "oracle-sme" in err
    assert "will not run it autonomously" in err


def test_anchored_block_prints_the_handoff_from_the_card(capsys):
    with pytest.raises(SystemExit):
        _run("T-BLOCK", "--required_role", "oracle-sme", "-m", "decide", "--dry-run")
    err = capsys.readouterr().err
    assert "Hands to" in err
    assert "--force" in err


def test_force_bypasses_the_anchored_block(capsys):
    """--dry-run returns rather than exiting, so reaching the DRY RUN block
    means the Anchored gate was bypassed."""
    _run("T-F", "--required_role", "oracle-sme", "-m", "go", "--dry-run", "--force")
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "oracle-sme" in out
    assert "Anchored" in out


def test_force_bypass_announces_itself_on_stderr(capsys):
    """--force is documented as audit-visible; it has to actually emit a record."""
    _run("T-F", "--required_role", "oracle-sme", "-m", "go", "--dry-run", "--force")
    err = capsys.readouterr().err
    assert "--force in effect" in err
    assert "oracle-sme" in err
    assert "separation-of-duties" in err


def test_unknown_role_exits_one(capsys):
    with pytest.raises(SystemExit) as exc:
        _run("T-X", "--required_role", "no-such-role", "-m", "hi", "--dry-run")
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_missing_roles_directory_points_at_the_override(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path / "absent"))
    with pytest.raises(SystemExit) as exc:
        _run("T-X", "--required_role", "widget-engineer", "-m", "hi", "--dry-run")
    assert exc.value.code == 1
    assert "DX_ROLES_PATH" in capsys.readouterr().err


def test_role_context_is_injected_into_the_pxx_prompt(monkeypatch):
    """The mandate and must_not sections must actually reach the model."""
    captured = {}

    class Result:
        returncode = 0

    def fake_run(cmd, env=None, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = env
        return Result()

    monkeypatch.setattr("dx.cmd_run.subprocess.run", fake_run)
    _run("T-004", "--required_role", "widget-engineer", "-m", "build a widget", "--no-commit")

    prompt = captured["cmd"][captured["cmd"].index("--message") + 1]
    assert "[ROLE: widget-engineer" in prompt
    assert "MANDATE:" in prompt
    assert "MUST NOT" in prompt
    assert "Review or approve your own widget" in prompt
    assert "build a widget" in prompt


def test_routing_is_passed_to_pxx_as_environment(monkeypatch):
    captured = {}

    class Result:
        returncode = 0

    monkeypatch.setattr(
        "dx.cmd_run.subprocess.run",
        lambda cmd, env=None, **k: (captured.update(env=env), Result())[1],
    )
    _run("T-005", "--required_role", "widget-engineer", "-m", "x", "--no-commit")

    env = captured["env"]
    assert env["PXX_BASE_URL"] == "http://vllm.invalid:8007"
    assert env["PXX_MODEL"] == "test-27b"
    assert env["PXX_PROVIDER"] == "vllm"


def test_no_commit_omits_the_commit_flag(monkeypatch):
    captured = {}

    class Result:
        returncode = 0

    monkeypatch.setattr(
        "dx.cmd_run.subprocess.run",
        lambda cmd, env=None, **k: (captured.update(cmd=cmd), Result())[1],
    )
    _run("T-006", "--required_role", "widget-engineer", "-m", "x", "--no-commit")
    assert "--commit" not in captured["cmd"]

    _run("T-007", "--required_role", "widget-engineer", "-m", "x")
    assert "--commit" in captured["cmd"]


def test_pxx_failure_propagates_the_exit_code(monkeypatch):
    class Result:
        returncode = 3

    monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: Result())
    with pytest.raises(SystemExit) as exc:
        _run("T-008", "--required_role", "widget-engineer", "-m", "x", "--no-commit")
    assert exc.value.code == 3
