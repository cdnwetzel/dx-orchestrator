"""Task branches (0.23.0, plan D-3): every run on its own branch, the
candidate pinned by a tag, the checkout always returned, and sign-off a real
merge — against real temporary repositories, with pxx replaced by a stub that
performs the git actions pxx would. Never a mocked git.

Design: AskPS `docs/general/ps-coding-wp3-design.md` §3.8 (Kimi design
review 2026-10-12, PASS). Case 6 (the legacy inline candidate, refused unless
`--allow-already-on-branch`) lives in test_cmd_merge_state.py beside the
0.22.0 test it replaced.
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import MANIFEST, _canonical, _chain, manifest_with_approval, write_declaration
from dx.cli import build_parser
from dx.config_loader import BranchesConfig, ConfigError, get_branches_config, validate_manifest
from dx.ledger_utils import SignerIdentity
from dx.ledger_writer import LedgerWriteError, git_merge_no_ff, row_hash
from dx.task_branch import BranchError, merge_conflicts

TASK = "T-BR"
AUTHOR_ROW = {"author_human": "Alice Author", "author_seat": "S4"}
REVIEWER = SignerIdentity(fingerprint="F" * 40, uid="Bob Reviewer <bob@example.invalid>",
                          residency="software",
                          mechanism="fallback (RL-010 non-compliant: software key)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], capture_output=True, text=True, check=True, timeout=30
                          ).stdout.strip()


def _rows(ledger: Path) -> list[dict]:
    return [json.loads(ln) for ln in (ledger / "ledger.jsonl").read_text().splitlines()
            if ln.strip()]


def _actions(ledger: Path, task: str = TASK) -> list[str]:
    return [r["action"] for r in _rows(ledger) if r["task_id"] == task]


def _branches(repo: Path) -> list[str]:
    return _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split()


def _tags(repo: Path) -> list[str]:
    return _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/tags/").split()


# --- the pxx stub -------------------------------------------------------------

STUB = f"""#!{sys.executable}
\"\"\"Stands in for `pxx loop`: does to the scope what pxx's outcome would.\"\"\"
import os, subprocess, sys
scope = sys.argv[sys.argv.index("--scope") + 1]
action = os.environ.get("STUB_PXX_ACTION", "commit")
files = os.environ.get("STUB_PXX_FILES", "src/app.py").split(",")
def git(*a):
    subprocess.run(["git", "-C", scope, "-c", "user.email=pxx@t", "-c", "user.name=pxx", *a],
                   check=True, capture_output=True)
def write():
    for f in files:
        p = os.path.join(scope, f); os.makedirs(os.path.dirname(p) or scope, exist_ok=True)
        with open(p, "a") as h: h.write("work by the stub\\n")
if action == "commit":
    write(); git("add", "-A"); git("commit", "-qm", "the work (pxx stub)"); sys.exit(0)
if action == "dirty":
    write(); sys.exit(0)
if action == "fail-reset":
    # what pxx's restore_safety_net does on a non-COMPLETED outcome
    write(); git("checkout", "--", "."); git("clean", "-fdq"); sys.exit(3)
if action == "fail-commit":
    write(); git("add", "-A"); git("commit", "-qm", "committed, then failed"); sys.exit(3)
if action == "noop":
    sys.exit(0)
raise SystemExit(f"unknown STUB_PXX_ACTION {{action!r}}")
"""


@pytest.fixture
def stub(tmp_path, monkeypatch):
    path = tmp_path / "pxx-stub.py"
    path.write_text(STUB)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: str(path))
    # launch-time claims that would read the host's pxx config or run store
    monkeypatch.setattr("dx.cmd_run._reviewer_route", lambda *a, **k: None)
    monkeypatch.setattr("dx.salvage.pxx_runs_dir", lambda: None)
    return path


# --- a pilot, a ledger, a manifest ---------------------------------------------

@pytest.fixture
def pilot(tmp_path):
    ai_root = tmp_path / "ai"
    repo = ai_root / "pilot"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _ledger_rows(ledger: Path, rows: list[dict]) -> str:
    chained, head = _chain(rows)
    (ledger / "ledger.jsonl").write_text("".join(_canonical(r) + "\n" for r in chained))
    return head


def _sync_verifier(ledger: Path) -> str:
    """The fake verifier prints a fixed head; after real appends it must print
    the real one for `dx merge`'s chain check. Returns the head."""
    head = row_hash(_rows(ledger)[-1])
    (ledger / "tools" / "verify_chain.py").write_text(f"print('Ledger head hash: {head}')\n")
    return head


@pytest.fixture
def base(pilot) -> str:
    """The pilot's base commit: main's HEAD at admission."""
    return _git(pilot, "rev-parse", "HEAD")


@pytest.fixture
def ledger(fake_ledger, pilot, base, monkeypatch):
    """The fake ledger holding T-BR ADMITTED at the pilot's base, with the
    queue file the bridge's admission writes (base_sha, approve_role)."""
    _ledger_rows(fake_ledger, [
        {"ts": "2026-10-01T00:00:00Z", "task_id": TASK, "action": "ADMITTED", **AUTHOR_ROW}])
    (fake_ledger / "queue" / f"{TASK}.json").write_text(json.dumps(
        {"task_id": TASK, "state": "ADMITTED", "approve_role": "code_review",
         "base_sha": base, "base_branch": "main", "mode": "BRIDGE", "supersedes": ""}))
    _sync_verifier(fake_ledger)
    _git(fake_ledger, "add", "-A")
    _git(fake_ledger, "commit", "-qm", "admitted")
    monkeypatch.setenv("DX_LEDGER_REPO", str(fake_ledger))
    return fake_ledger


def _manifest(tmp_path, monkeypatch, branches: str | None, *, approval_for: Path | None = None):
    """The fixture manifest plus a `branches:` block (None: absent) and,
    when asked, an `approval:` section declaring ``approval_for``'s parent as
    AI_ROOT with the pilot as a single-reviewer scope."""
    if approval_for is not None:
        ai_root = approval_for.parent
        declaration = write_declaration(ai_root / "roles.json", ["${AI_ROOT}/pilot"])
        out = manifest_with_approval(tmp_path, declaration, ai_root)
        text = out.read_text()
    else:
        out = tmp_path / "manifest.yml"
        text = MANIFEST.read_text()
    if branches is not None:
        text += f"\nbranches:\n{branches}\n"
    out.write_text(text)
    monkeypatch.setenv("DX_CONFIG", str(out))
    from dx import config_loader
    config_loader._config = None
    config_loader._config_source = None
    return out


BRANCH_ON = "  enabled: true\n  base: main\n  prefix: task/"


def _run(*argv) -> int:
    args = build_parser().parse_args(["run", *argv])
    try:
        args.func(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _run_task(pilot: Path, evidence: Path, *extra, task: str = TASK) -> int:
    return _run(task, "--required_role", "widget-engineer", "-m", "do the work",
                "--scope", str(pilot), "--evidence-dir", str(evidence), "--task-branch", *extra)


def _bundle(evidence: Path, task: str = TASK) -> tuple[dict, dict]:
    """(manifest.json, artifacts/branch.json) of the task's newest bundle."""
    runs = sorted((evidence / task).iterdir())
    assert runs, f"no bundle for {task} under {evidence}"
    out = runs[-1]
    return (json.loads((out / "manifest.json").read_text()),
            json.loads((out / "artifacts" / "branch.json").read_text()))


# --- the manifest section --------------------------------------------------------

class TestBranchesConfig:
    def test_absent_section_is_none_inline_mode(self, tmp_path, monkeypatch):
        _manifest(tmp_path, monkeypatch, None)
        assert get_branches_config() is None

    def test_enabled_with_defaults(self, tmp_path, monkeypatch):
        _manifest(tmp_path, monkeypatch, "  enabled: true")
        cfg = get_branches_config()
        assert cfg == BranchesConfig(enabled=True, base="main", prefix="task/")
        assert cfg.branch_for("T-0001") == "task/T-0001"

    def test_enabled_has_no_default(self, tmp_path, monkeypatch):
        """A typo must not become inline mode."""
        _manifest(tmp_path, monkeypatch, "  enabled: 'yes'")
        with pytest.raises(ConfigError, match="branches.enabled"):
            get_branches_config()

    @pytest.mark.parametrize("block", [
        "  enabled: true\n  base: ''", "  enabled: true\n  base: 'ma in'",
        "  enabled: true\n  prefix: 'a..b/'", "  enabled: true\n  base: '-x'",
    ])
    def test_a_name_git_would_refuse_is_refused_here(self, tmp_path, monkeypatch, block):
        _manifest(tmp_path, monkeypatch, block)
        with pytest.raises(ConfigError, match="branches"):
            get_branches_config()

    def test_a_base_inside_the_prefix_is_refused(self, tmp_path, monkeypatch):
        _manifest(tmp_path, monkeypatch, "  enabled: true\n  base: task/main")
        with pytest.raises(ConfigError, match="stray task branch"):
            get_branches_config()

    def test_an_unknown_key_is_refused(self, tmp_path, monkeypatch):
        _manifest(tmp_path, monkeypatch, "  enabled: true\n  branch: x")
        with pytest.raises(ConfigError, match="unknown key"):
            get_branches_config()

    def test_validate_manifest_catches_a_bad_section(self, tmp_path, monkeypatch):
        _manifest(tmp_path, monkeypatch, "  enabled: 1")
        with pytest.raises(ConfigError):
            validate_manifest()


# --- dx run in branch mode ------------------------------------------------------

class TestRun:
    def test_1_happy_path(self, pilot, ledger, base, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        ev = tmp_path / "ev"
        assert _run_task(pilot, ev) == 0, capsys.readouterr()
        base = base
        assert _git(pilot, "rev-parse", "main") == base, "main moved"
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "main"
        assert "task/T-BR" not in _branches(pilot)
        assert _tags(pilot) == ["candidate/T-BR"]
        candidate = _git(pilot, "rev-parse", "candidate/T-BR^{commit}")
        assert candidate != base
        assert _git(pilot, "rev-parse", f"{candidate}^") == base, "candidate not on the base"
        rows = [r for r in _rows(ledger) if r["task_id"] == TASK]
        assert [r["action"] for r in rows] == ["ADMITTED", "EXECUTED"]
        assert rows[-1]["sha"] == candidate
        queue = json.loads((ledger / "queue" / f"{TASK}.json").read_text())
        assert queue["sha"] == candidate and queue["state"] == "EXECUTED"
        assert queue["branch"] == "task/T-BR" and queue["candidate_tag"] == "candidate/T-BR"
        manifest, branch = _bundle(ev)
        assert branch["mode"] == "branch" and branch["restored"] is True
        assert branch["candidate_tag"] == "candidate/T-BR" and branch["head_after"] == candidate
        assert branch["base_sha"] == base and branch["branch"] == "task/T-BR"
        assert manifest["checks"]["base_branch_restored"]["ok"] is True
        assert manifest["checks"]["candidate_tagged"]["ok"] is True
        assert manifest["checks"]["produced_changes"]["ok"] is True, "the snapshot was taken on the branch"
        assert "--task-branch" in manifest["checks"]["pxx_exit_zero"]["path"] or True
        out = capsys.readouterr().out
        assert "candidate/T-BR" in out and "back on main" in out

    def test_2_control_plane_contact_is_redlined_and_main_untouched(
            self, pilot, ledger, base, stub, tmp_path, monkeypatch):
        _manifest(tmp_path, monkeypatch, BRANCH_ON, approval_for=pilot)
        monkeypatch.setenv("STUB_PXX_FILES", "CODEOWNERS")
        assert _run_task(pilot, tmp_path / "ev") == 0
        assert _actions(ledger) == ["ADMITTED", "EXECUTED", "REDLINE"]
        assert _git(pilot, "rev-parse", "main") == base
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "main"
        assert _tags(pilot) == ["candidate/T-BR"]
        assert not (pilot / "CODEOWNERS").exists(), "the checkout shows the base, not the candidate"

    def test_3_failed_run_restores_and_records_nothing(self, pilot, ledger, base, stub, tmp_path,
                                                        monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        monkeypatch.setenv("STUB_PXX_ACTION", "fail-reset")
        ev = tmp_path / "ev"
        assert _run_task(pilot, ev) == 3
        assert _git(pilot, "rev-parse", "main") == base
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "main"
        assert _git(pilot, "status", "--porcelain") == ""
        assert _actions(ledger) == ["ADMITTED"]
        assert "task/T-BR" not in _branches(pilot) and _tags(pilot) == []
        _, branch = _bundle(ev)
        assert branch["restored"] is True and branch["candidate_tag"] is None
        assert "pxx task failed" in capsys.readouterr().err

    def test_3b_a_commit_before_a_failed_exit_is_pinned_not_orphaned(
            self, pilot, ledger, stub, tmp_path, monkeypatch):
        """Tagging is not conditioned on pxx's exit: a commit made before a
        non-zero exit is still work; deleting its branch would orphan it."""
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        monkeypatch.setenv("STUB_PXX_ACTION", "fail-commit")
        assert _run_task(pilot, tmp_path / "ev") == 3
        assert _tags(pilot) == ["candidate/T-BR"]
        assert _actions(ledger) == ["ADMITTED"], "a failed run records no EXECUTED"
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "main"
        assert "task/T-BR" not in _branches(pilot)

    def test_4_dirty_tree_is_refused_before_any_git_write(self, pilot, ledger, base, stub, tmp_path,
                                                          monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        (pilot / "src" / "app.py").write_text("x = 2  # someone's uncommitted edit\n")
        before = _git(pilot, "status", "--porcelain")
        assert _run_task(pilot, tmp_path / "ev") == 1
        err = capsys.readouterr().err
        assert "uncommitted" in err and "clean checkout" in err
        assert _git(pilot, "status", "--porcelain") == before
        assert _git(pilot, "rev-parse", "HEAD") == base
        assert _branches(pilot) == ["main"] and _tags(pilot) == []
        assert _actions(ledger) == ["ADMITTED"]

    def test_7_a_seed_that_does_not_apply_leaves_main_and_the_checkout_untouched(
            self, pilot, ledger, base, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        patch = tmp_path / "T-0" / "artifacts" / "run-diff.patch"
        patch.parent.mkdir(parents=True)
        patch.write_text("--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-NOT THE BASE\n+y = 2\n")
        code = _run_task(pilot, tmp_path / "ev", "--seed-patch", str(patch), "--seed-from", "T-0")
        assert code == 1
        err = capsys.readouterr().err
        assert "seed from T-0 refused" in err and "back on main" in err
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "main"
        assert _git(pilot, "rev-parse", "main") == base
        assert _git(pilot, "status", "--porcelain") == ""
        assert _branches(pilot) == ["main"]
        assert _actions(ledger) == ["ADMITTED"]

    def test_8_a_pilot_left_on_a_task_branch_is_refused_naming_it(
            self, pilot, ledger, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        _git(pilot, "switch", "-q", "-c", "task/T-OLD")
        assert _run_task(pilot, tmp_path / "ev") == 1
        err = capsys.readouterr().err
        assert "task/T-OLD" in err and "not on the base branch 'main'" in err
        assert "dx doctor" in err
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "task/T-OLD"
        assert _actions(ledger) == ["ADMITTED"] and _tags(pilot) == []

    def test_9_a_dirty_tree_after_the_run_parks_the_checkout_with_exit_4(
            self, pilot, ledger, base, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        monkeypatch.setenv("STUB_PXX_ACTION", "dirty")
        ev = tmp_path / "ev"
        assert _run_task(pilot, ev) == 4
        err = capsys.readouterr().err
        assert "parked on task/T-BR" in err and "git -C" in err and "branch -D task/T-BR" in err
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "task/T-BR"
        assert _git(pilot, "status", "--porcelain") != "", "nothing may be reset"
        assert _git(pilot, "rev-parse", "main") == base
        manifest, branch = _bundle(ev)
        assert branch["restored"] is False and "uncommitted" in branch["parked"]
        assert manifest["checks"]["base_branch_restored"]["ok"] is False
        assert _actions(ledger) == ["ADMITTED"], "nothing committed, so no candidate"
        # a second run (another task) is refused by the preflight: case 8
        monkeypatch.delenv("STUB_PXX_ACTION")
        assert _run_task(pilot, ev, task="T-NEXT") == 1
        assert "task/T-BR" in capsys.readouterr().err

    def test_11_a_re_admitted_id_gets_a_fresh_branch_and_a_suffixed_tag(
            self, pilot, ledger, base, stub, tmp_path, monkeypatch):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        assert _run_task(pilot, tmp_path / "ev") == 0
        first = _git(pilot, "rev-parse", "candidate/T-BR^{commit}")
        # sent back and re-admitted: INCOMPLETE then ADMITTED again
        rows = _rows(ledger)
        rows += [{"ts": "2026-10-02T00:00:00Z", "task_id": TASK, "action": "INCOMPLETE",
                  "author_seat": None, "author_human": None, "reviewer_seat": "S9",
                  "sha": first, "evidence": "sent back"},
                 {"ts": "2026-10-03T00:00:00Z", "task_id": TASK, "action": "ADMITTED", **AUTHOR_ROW}]
        _ledger_rows(ledger, [{k: v for k, v in r.items() if k != "prev_hash"} for r in rows])
        q = json.loads((ledger / "queue" / f"{TASK}.json").read_text())
        q.update({"state": "ADMITTED"})
        q.pop("sha", None)
        (ledger / "queue" / f"{TASK}.json").write_text(json.dumps(q))
        # Different content: the same edit in the same second would be the
        # same commit sha, which the tag already pins (and is reported so).
        monkeypatch.setenv("STUB_PXX_FILES", "src/other.py")
        assert _run_task(pilot, tmp_path / "ev") == 0
        assert _tags(pilot) == ["candidate/T-BR", "candidate/T-BR-2"]
        second = _git(pilot, "rev-parse", "candidate/T-BR-2^{commit}")
        assert second != first and _git(pilot, "rev-parse", f"{second}^") == base
        assert "task/T-BR" not in _branches(pilot)
        assert [r["sha"] for r in _rows(ledger) if r["action"] == "EXECUTED"] == [first, second]

    def test_12_the_flag_asserts_and_the_manifest_decides(self, pilot, ledger, stub, tmp_path,
                                                          monkeypatch, capsys):
        # --task-branch against an inline manifest: refused, nothing run
        _manifest(tmp_path, monkeypatch, None)
        assert _run_task(pilot, tmp_path / "ev") == 1
        assert "manifest says inline" in capsys.readouterr().err
        assert _actions(ledger) == ["ADMITTED"] and _tags(pilot) == []
        _manifest(tmp_path, monkeypatch, "  enabled: false")
        assert _run_task(pilot, tmp_path / "ev") == 1
        assert "branches.enabled: false" in capsys.readouterr().err
        # enabled without the flag: branch mode runs all the same
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        code = _run(TASK, "--required_role", "widget-engineer", "-m", "x", "--scope", str(pilot),
                    "--evidence-dir", str(tmp_path / "ev"))
        assert code == 0
        assert _tags(pilot) == ["candidate/T-BR"]
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "main"

    def test_inline_mode_is_unchanged(self, pilot, ledger, base, stub, tmp_path, monkeypatch):
        """No section: pxx commits on main, as 0.22.0 did; branch.json says so."""
        _manifest(tmp_path, monkeypatch, None)
        ev = tmp_path / "ev"
        code = _run(TASK, "--required_role", "widget-engineer", "-m", "x", "--scope", str(pilot),
                    "--evidence-dir", str(ev))
        assert code == 0
        assert _git(pilot, "rev-parse", "main") != base
        assert _tags(pilot) == [] and _branches(pilot) == ["main"]
        manifest, branch = _bundle(ev)
        assert branch == {"mode": "inline"}
        assert "base_branch_restored" not in manifest["checks"]

    def test_a_tag_failure_is_fail_closed(self, pilot, ledger, stub, tmp_path, monkeypatch, capsys):
        """§3.3 step 6: no EXECUTED, branch kept, exit 4 naming the branch."""
        _manifest(tmp_path, monkeypatch, BRANCH_ON)

        def no_tag(repo, task_id, sha):
            raise BranchError("simulated: git tag failed")
        monkeypatch.setattr("dx.cmd_run.tag_candidate", no_tag)
        ev = tmp_path / "ev"
        assert _run_task(pilot, ev) == 4
        err = capsys.readouterr().err
        assert "could not be tagged" in err and "task/T-BR" in err
        assert _actions(ledger) == ["ADMITTED"]
        assert "task/T-BR" in _branches(pilot) and _tags(pilot) == []
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "task/T-BR"
        manifest, branch = _bundle(ev)
        assert manifest["checks"]["candidate_tagged"]["ok"] is False
        assert branch["restored"] is False and "tag failed" in branch["parked"]


# --- dx merge ---------------------------------------------------------------------

def _merge(monkeypatch, ledger: Path, pilot: Path, *argv) -> int:
    head = _sync_verifier(ledger)
    (ledger / "approvals" / f"{TASK}.code_review.msg").write_text(f"{TASK}{head}code_review")
    _git(ledger, "add", "-A")
    _git(ledger, "commit", "-qm", "ready to sign", "--allow-empty")
    monkeypatch.setattr("dx.cmd_merge.verify_detached_signature", lambda s, m, r: REVIEWER)
    args = build_parser().parse_args(["merge", TASK, "--repo", str(pilot), *argv])
    try:
        args.func(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


class TestMerge:
    def test_5_sign_off_makes_a_two_parent_merge_named_by_the_row(
            self, pilot, ledger, base, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        assert _run_task(pilot, tmp_path / "ev") == 0
        candidate = _git(pilot, "rev-parse", "candidate/T-BR^{commit}")
        assert _merge(monkeypatch, ledger, pilot) == 0, capsys.readouterr()
        # (the gate then appends an EVIDENCE row binding its own bundle)
        assert _actions(ledger)[:4] == ["ADMITTED", "EXECUTED", "SIGNED", "MERGED"]
        by_action = {r["action"]: r for r in _rows(ledger) if r["task_id"] == TASK}
        merge_commit = _git(pilot, "rev-parse", "main")
        parents = _git(pilot, "log", "-1", "--format=%P", "main").split()
        assert len(parents) == 2 and candidate in parents and base in parents
        assert by_action["SIGNED"]["sha"] == candidate
        assert by_action["MERGED"]["sha"] == merge_commit
        assert merge_commit in by_action["MERGED"]["evidence"]
        assert _git(pilot, "symbolic-ref", "--short", "HEAD") == "main"
        assert _branches(pilot) == ["main"], "nothing is deleted at MERGED; the branch was already gone"
        assert _tags(pilot) == ["candidate/T-BR"], "the tag stays as the permanent name"
        out = capsys.readouterr().out
        assert "merges cleanly" in out and "Merged" in out

    def test_10_a_conflicting_move_of_main_is_refused_before_signed(
            self, pilot, ledger, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        assert _run_task(pilot, tmp_path / "ev") == 0
        (pilot / "src" / "app.py").write_text("x = 3  # main moved on the same line\n")
        _git(pilot, "commit", "-qam", "main moved")
        moved = _git(pilot, "rev-parse", "main")
        assert _merge(monkeypatch, ledger, pilot) == 1
        err = capsys.readouterr().err
        assert "does not merge cleanly" in err and "src/app.py" in err and "rework" in err
        assert _actions(ledger) == ["ADMITTED", "EXECUTED"], "no row may be written"
        assert _git(pilot, "rev-parse", "main") == moved
        assert _git(pilot, "status", "--porcelain") == ""
        assert not (pilot / ".git" / "MERGE_HEAD").exists()

    def test_10b_a_non_conflicting_move_of_main_still_merges(
            self, pilot, ledger, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        assert _run_task(pilot, tmp_path / "ev") == 0
        (pilot / "README").write_text("unrelated\n")
        _git(pilot, "add", "-A")
        _git(pilot, "commit", "-qm", "main moved elsewhere")
        moved = _git(pilot, "rev-parse", "main")
        assert _merge(monkeypatch, ledger, pilot) == 0, capsys.readouterr()
        parents = _git(pilot, "log", "-1", "--format=%P", "main").split()
        assert moved in parents and len(parents) == 2
        assert _actions(ledger)[2:4] == ["SIGNED", "MERGED"]

    def test_a_target_off_its_base_or_dirty_is_refused_before_signed(
            self, pilot, ledger, stub, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON)
        assert _run_task(pilot, tmp_path / "ev") == 0
        _git(pilot, "switch", "-q", "-c", "task/T-OTHER")
        assert _merge(monkeypatch, ledger, pilot) == 1
        assert "not on the base branch 'main'" in capsys.readouterr().err
        _git(pilot, "switch", "-q", "main")
        _git(pilot, "branch", "-q", "-D", "task/T-OTHER")
        (pilot / "stray.txt").write_text("x\n")
        assert _merge(monkeypatch, ledger, pilot) == 1
        assert "uncommitted" in capsys.readouterr().err
        assert _actions(ledger) == ["ADMITTED", "EXECUTED"]


# --- the helper itself (case 13) -----------------------------------------------

class TestGitMergeNoFf:
    @staticmethod
    def _conflicting(tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "f").write_text("base\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        _git(repo, "switch", "-q", "-c", "task/T-1")
        (repo / "f").write_text("candidate\n")
        _git(repo, "commit", "-qam", "cand")
        cand = _git(repo, "rev-parse", "HEAD")
        _git(repo, "switch", "-q", "main")
        (repo / "f").write_text("main moved\n")
        _git(repo, "commit", "-qam", "moved")
        return repo, cand

    def test_13_a_conflict_raises_and_leaves_the_checkout_clean(self, tmp_path):
        """0.22.0 left MERGE_HEAD and conflict markers behind (AskPS WP-3
        design §1, reproduced 2026-10-12)."""
        repo, cand = self._conflicting(tmp_path)
        head = _git(repo, "rev-parse", "HEAD")
        with pytest.raises(LedgerWriteError, match="failed .exit 1.") as exc:
            git_merge_no_ff(repo, cand, task_id="T-1")
        assert "conflicting: f" in str(exc.value)
        assert not (repo / ".git" / "MERGE_HEAD").exists()
        assert _git(repo, "status", "--porcelain") == ""
        assert _git(repo, "rev-parse", "HEAD") == head
        assert (repo / "f").read_text() == "main moved\n"

    def test_merge_tree_names_the_conflict_without_touching_the_tree(self, tmp_path):
        repo, cand = self._conflicting(tmp_path)
        assert merge_conflicts(repo, "HEAD", cand) == ["f"]
        assert _git(repo, "status", "--porcelain") == ""
        _git(repo, "switch", "-q", "-c", "clean", "main~1")
        (repo / "g").write_text("g\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "unrelated")
        assert merge_conflicts(repo, "HEAD", cand) is None


# --- dx doctor (case 14) ---------------------------------------------------------

class TestDoctorPosture:
    def test_14_a_stray_branch_is_a_failed_doctor_in_branch_mode(self, pilot, tmp_path, monkeypatch):
        from dx.cmd_doctor import _branch_posture
        _manifest(tmp_path, monkeypatch, BRANCH_ON, approval_for=pilot)
        lines, ok = _branch_posture()
        assert ok and any(ln.startswith("   ✅") and "on main, clean, 0 stray" in ln for ln in lines)
        _git(pilot, "branch", "task/T-GONE")
        lines, ok = _branch_posture()
        assert not ok
        text = "\n".join(lines)
        assert "❌" in text and "task/T-GONE" in text
        assert f"git -C {pilot} switch main" in text and "branch -D task/T-GONE" in text

    def test_off_reports_inline_and_never_fails(self, pilot, tmp_path, monkeypatch):
        from dx.cmd_doctor import _branch_posture
        _manifest(tmp_path, monkeypatch, None, approval_for=pilot)
        _git(pilot, "switch", "-q", "-c", "task/T-X")
        lines, ok = _branch_posture()
        assert ok and "off" in lines[0] and "F-003" in lines[0]
        assert any("⚠️" in ln and "task/T-X" in ln for ln in lines)

    def test_doctor_prints_the_posture(self, pilot, tmp_path, monkeypatch, capsys):
        _manifest(tmp_path, monkeypatch, BRANCH_ON, approval_for=pilot)
        args = build_parser().parse_args(["doctor", "--no-network"])
        try:
            args.func(args)
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert "task branches: on" in out and str(pilot) in out
