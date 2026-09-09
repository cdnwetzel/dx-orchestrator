"""`dx run` — the Anchored hard-block and hardware routing."""
import json

import pytest

from dx import cmd_run
from dx.cli import build_parser
from dx.evidence import EvidenceError


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
    _run("T-004", "--no-evidence", "--required_role", "widget-engineer", "-m", "build a widget", "--no-commit")

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
    _run("T-005", "--no-evidence", "--required_role", "widget-engineer", "-m", "x", "--no-commit")

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
    _run("T-006", "--no-evidence", "--required_role", "widget-engineer", "-m", "x", "--no-commit")
    assert "--commit" not in captured["cmd"]

    _run("T-007", "--no-evidence", "--required_role", "widget-engineer", "-m", "x")
    assert "--commit" in captured["cmd"]


def _pxx_exiting(code):
    class Result:
        returncode = code

    return lambda *a, **k: Result()


@pytest.mark.parametrize("pxx_code", [1, 3, 42, 127])
def test_pxx_failure_reports_dx_task_failed(monkeypatch, pxx_code):
    """A failing pxx is EXIT_TASK_FAILED whatever pxx itself returned.

    dx used to return pxx's code verbatim, which put a downstream tool in
    charge of dx's contract.
    """
    monkeypatch.setattr("dx.cmd_run.subprocess.run", _pxx_exiting(pxx_code))
    with pytest.raises(SystemExit) as exc:
        _run("T-008", "--no-evidence", "--required_role", "widget-engineer", "-m", "x", "--no-commit")
    assert exc.value.code == cmd_run.EXIT_TASK_FAILED


def test_pxx_exiting_2_is_not_mistaken_for_an_anchored_refusal(monkeypatch, capsys):
    """The regression this contract exists for.

    pxx exits 2 in the wild — a missing shell safeguard does it — and dx used to
    hand that straight back. A caller reading exit 2 would conclude governance
    had refused the role when in fact the task ran and failed. The two outcomes
    demand opposite responses: one is "fix your task", the other is "you are not
    allowed to run this at all".
    """
    monkeypatch.setattr("dx.cmd_run.subprocess.run", _pxx_exiting(2))
    with pytest.raises(SystemExit) as exc:
        _run("T-009", "--no-evidence", "--required_role", "widget-engineer", "-m", "x", "--no-commit")
    assert exc.value.code != cmd_run.EXIT_ANCHORED_REFUSED
    assert exc.value.code == cmd_run.EXIT_TASK_FAILED
    # pxx's real code is not discarded — it moves into the message.
    assert "pxx exit 2" in capsys.readouterr().err


def test_anchored_refusal_still_owns_exit_2():
    """The other half: nothing may erode the code that means 'policy refused'."""
    assert cmd_run.EXIT_ANCHORED_REFUSED == 2
    assert cmd_run.EXIT_TASK_FAILED != cmd_run.EXIT_ANCHORED_REFUSED


class TestEvidenceEmission:
    """`dx run` emits a `dx.role_task.v1` bundle (ROADMAP §1.1).

    Admission record: `docs/admissions/T-1101-evidence-bundles.md`, which
    requires that emission does not disturb the 0.7.1 exit-code contract.
    """

    @staticmethod
    def _no_git(monkeypatch):
        """Scope is not a git repo here; evidence must degrade, not explode."""
        monkeypatch.setattr("dx.cmd_run._git", lambda *a, **k: None)

    def _run_with_evidence(self, monkeypatch, tmp_path, pxx_code=0, *extra):
        class Result:
            returncode = pxx_code

        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: Result())
        self._no_git(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _run("T-EV", "--required_role", "widget-engineer", "-m", "x",
                 "--no-commit", "--evidence-dir", str(tmp_path), *extra)
        return exc.value.code

    def test_a_successful_run_emits_a_bundle(self, monkeypatch, tmp_path, capsys):
        class Result:
            returncode = 0

        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: Result())
        self._no_git(monkeypatch)
        _run("T-EV", "--required_role", "widget-engineer", "-m", "x",
             "--no-commit", "--evidence-dir", str(tmp_path))
        bundles = list((tmp_path / "T-EV").iterdir())
        assert len(bundles) == 1
        manifest = json.loads((bundles[0] / "manifest.json").read_text())
        assert manifest["result"]["passed"] is True
        assert manifest["role"] == "widget-engineer"
        assert "Evidence:" in capsys.readouterr().out

    def test_a_failed_run_still_emits_a_bundle(self, monkeypatch, tmp_path):
        """A store that only records successes is a highlight reel."""
        code = self._run_with_evidence(monkeypatch, tmp_path, pxx_code=2)
        assert code == cmd_run.EXIT_TASK_FAILED, "evidence changed the exit contract"
        manifest = json.loads(
            (next((tmp_path / "T-EV").iterdir()) / "manifest.json").read_text()
        )
        assert manifest["result"]["passed"] is False
        assert manifest["checks"]["pxx_exit_zero"]["ok"] is False

    def test_no_evidence_skips_emission_without_changing_the_outcome(
        self, monkeypatch, tmp_path
    ):
        class Result:
            returncode = 0

        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: Result())
        self._no_git(monkeypatch)
        _run("T-EV", "--required_role", "widget-engineer", "-m", "x",
             "--no-commit", "--no-evidence", "--evidence-dir", str(tmp_path))
        assert not tmp_path.exists() or not any(tmp_path.iterdir())

    def test_an_unwritable_receipt_fails_the_run_closed(self, monkeypatch, tmp_path):
        """A run that produced no receipt is not a receipted run. It must not
        report success — but it must also not masquerade as a task failure,
        which is what exit 3 means."""
        def boom(*a, **k):
            raise EvidenceError("disk on fire")

        class Result:
            returncode = 0

        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: Result())
        monkeypatch.setattr("dx.cmd_run.write_bundle", boom)
        self._no_git(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _run("T-EV", "--required_role", "widget-engineer", "-m", "x",
                 "--no-commit", "--evidence-dir", str(tmp_path))
        assert exc.value.code == cmd_run.EXIT_ERROR
        assert exc.value.code != cmd_run.EXIT_TASK_FAILED

    def test_the_dry_run_writes_no_evidence(self, monkeypatch, tmp_path):
        """--dry-run performs no work, so it has nothing to attest to."""
        self._no_git(monkeypatch)
        _run("T-EV", "--required_role", "widget-engineer", "-m", "x",
             "--dry-run", "--evidence-dir", str(tmp_path))
        assert not tmp_path.exists() or not any(tmp_path.iterdir())


def test_nonzero_exit_that_still_changed_the_scope_is_flagged_not_flatly_failed(
    monkeypatch, tmp_path, capsys
):
    """The 'non-zero but the code is fine' case: a model reaches for a shell pxx
    refuses and exits non-zero *after* writing correct code. dx must not report
    that as a flat failure — it says the scope changed and points at the receipt,
    while still exiting EXIT_TASK_FAILED (the exit code is the tool's, the
    judgement is the operator's)."""
    import subprocess as sp

    repo = tmp_path / "scope"
    repo.mkdir()
    sp.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    sp.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.invalid", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )

    real_run = sp.run

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return real_run(cmd, **kwargs)  # let dx's git probes run for real
        (repo / "hello.py").write_text("print('hi')\n")  # pxx wrote a file, then…
        class R:
            returncode = 2  # …exited non-zero (e.g. a refused ungated shell)
        return R()

    monkeypatch.setattr("dx.cmd_run.subprocess.run", fake_run)
    with pytest.raises(SystemExit) as exc:
        _run(
            "T-010", "--required_role", "widget-engineer", "-m", "x",
            "--no-commit", "--scope", str(repo), "--evidence-dir", str(tmp_path / "ev"),
        )
    assert exc.value.code == cmd_run.EXIT_TASK_FAILED
    err = capsys.readouterr().err
    assert "not proof the work is wrong" in err
    assert "pxx exit 2" in err
    assert "task failed" not in err  # not the flat-failure line


def test_nonzero_exit_with_no_change_is_still_a_flat_failure(monkeypatch, tmp_path, capsys):
    """The other half: pxx failed and wrote nothing → the plain failure line,
    the one TUTORIAL §6 and the docs-consistency test pin."""
    import subprocess as sp

    repo = tmp_path / "scope"
    repo.mkdir()
    sp.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    sp.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.invalid", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    real_run = sp.run

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return real_run(cmd, **kwargs)
        class R:
            returncode = 2  # wrote nothing
        return R()

    monkeypatch.setattr("dx.cmd_run.subprocess.run", fake_run)
    with pytest.raises(SystemExit) as exc:
        _run(
            "T-011", "--required_role", "widget-engineer", "-m", "x",
            "--no-commit", "--scope", str(repo), "--evidence-dir", str(tmp_path / "ev"),
        )
    assert exc.value.code == cmd_run.EXIT_TASK_FAILED
    assert "pxx task failed (pxx exit 2)" in capsys.readouterr().err
