"""`dx merge` — the task-state and candidate checks added 2026-09-21.

Before these, the gate verified a chain, a signature, a payload and
separation of duties, and then appended SIGNED — without asking whether the
task was fit to approve or which commit it was approving. A signature over a
redlined task merged. The candidate was read from the queue file alone, under
a field name the bridge path never wrote, and read only AFTER SIGNED had been
appended: every bridge task would have been signed and then refused.

Every refusal asserted here leaves the ledger byte-identical.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dx.cli import build_parser
from dx.ledger_utils import SignerIdentity
from dx.ledger_writer import GENESIS_PREV, canonical, row_hash

REVIEWER = SignerIdentity(fingerprint="F" * 40, uid="Bob Reviewer <bob@example.invalid>")
FAKE_SHA = "e" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                           "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True,
                          timeout=30).stdout.strip()


def _row(task: str, action: str, **extra) -> dict:
    row = {"ts": "2026-09-01T00:00:00Z", "task_id": task, "action": action,
           "author_human": "Alice Author", "author_seat": "S4"}
    row.update(extra)
    return row


def _make_ledger(path: Path, rows: list[dict], queue: dict) -> str:
    """A ledger repo whose verifier reports the head these rows produce."""
    for d in ("tools", "queue", "approvals", "docs/keys"):
        (path / d).mkdir(parents=True, exist_ok=True)
    prev = GENESIS_PREV
    lines = []
    for row in rows:
        full = dict(row, prev_hash=prev)
        lines.append(canonical(full))
        prev = row_hash(full)
    head = prev
    (path / "ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "tools" / "verify_chain.py").write_text(
        f"print('Chain OK: {len(rows)} rows')\nprint('Ledger head hash: {head}')\n",
        encoding="utf-8")
    role = queue.get("approve_role", "code_review")
    task = queue["task_id"]
    (path / "queue" / f"{task}.json").write_text(json.dumps(queue), encoding="utf-8")
    (path / "approvals" / f"{task}.{role}.msg").write_text(
        f"{task}{head}{role}", encoding="utf-8")
    (path / "approvals" / f"{task}.{role}.asc").write_text("(stub)\n", encoding="utf-8")
    if not (path / ".git").exists():
        _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "seed")
    return head


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """Run `dx merge` against a ledger built from the given rows, signature stubbed."""
    ledger = tmp_path / "devswarm-ledger"

    def _run(rows, queue, *argv, ledger_dir: Path | None = None):
        led = ledger_dir or ledger
        _make_ledger(led, rows, queue)
        before = (led / "ledger.jsonl").read_bytes()
        monkeypatch.setenv("DX_LEDGER_REPO", str(led))
        monkeypatch.setattr("dx.cmd_merge.verify_detached_signature",
                            lambda sig, msg, repo: REVIEWER)
        args = build_parser().parse_args(["merge", queue["task_id"], "--no-evidence", *argv])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        after = (led / "ledger.jsonl").read_bytes()
        return exc.value.code, before, after

    _run.ledger = ledger
    return _run


def _actions(ledger: Path) -> list[str]:
    return [json.loads(line)["action"] for line in
            (ledger / "ledger.jsonl").read_text().splitlines() if line.strip()]


class TestRefusalsWriteNothing:
    def test_a_redlined_task_is_refused(self, gate, capsys):
        code, before, after = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=FAKE_SHA),
             _row("T-1", "REDLINE", author_human="Frank Diaz")],
            {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA})
        assert code == 1
        err = capsys.readouterr().err
        assert "REDLINE" in err and "Frank Diaz" in err
        assert after == before, "a refusal wrote to the ledger"

    def test_an_incomplete_task_is_refused(self, gate, capsys):
        code, before, after = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "INCOMPLETE")],
            {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA})
        assert code == 1 and after == before
        assert "INCOMPLETE" in capsys.readouterr().err

    def test_a_task_that_never_executed_is_refused(self, gate, capsys):
        """T-0009..T-0018's shape: nothing to approve."""
        code, before, after = gate(
            [_row("T-1", "ADMITTED")],
            {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA})
        assert code == 1 and after == before
        assert "no EXECUTED row" in capsys.readouterr().err

    def test_an_executed_row_without_a_commit_is_refused(self, gate, capsys):
        code, before, after = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED")],
            {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA})
        assert code == 1 and after == before
        assert "no EXECUTED row with a commit" in capsys.readouterr().err

    def test_the_legacy_queue_field_is_refused_and_named(self, gate, capsys):
        """Every bridge task's queue file on 2026-09-21 looked like this."""
        code, before, after = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=FAKE_SHA)],
            {"task_id": "T-1", "approve_role": "code_review", "executed_sha": FAKE_SHA})
        assert code == 1 and after == before
        assert "executed_sha" in capsys.readouterr().err

    def test_a_queue_that_disagrees_with_the_ledger_is_refused(self, gate, capsys):
        code, before, after = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=FAKE_SHA)],
            {"task_id": "T-1", "approve_role": "code_review", "sha": "f" * 40})
        assert code == 1 and after == before
        assert "ledger is the record" in capsys.readouterr().err

    def test_an_already_signed_task_is_refused(self, gate, capsys):
        code, before, after = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=FAKE_SHA),
             _row("T-1", "SIGNED", sha=FAKE_SHA)],
            {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA})
        assert code == 1 and after == before
        assert "already SIGNED" in capsys.readouterr().err

    def test_a_candidate_absent_from_the_repo_is_refused_before_signed(
        self, gate, tmp_path, capsys,
    ):
        """The old code appended SIGNED and THEN discovered there was nothing
        to merge. Now the repo is asked first."""
        repo = tmp_path / "work"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "a").write_text("a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        code, before, after = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=FAKE_SHA)],
            {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA},
            "--repo", str(repo))
        assert code == 1
        assert after == before, "SIGNED was written for a candidate that does not exist"
        assert "not a commit in" in capsys.readouterr().err


class TestTheGreenPath:
    def test_signed_and_merged_bind_the_candidate_and_the_merge_commit(
        self, gate, tmp_path,
    ):
        repo = tmp_path / "work"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "a").write_text("a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        _git(repo, "checkout", "-qb", "task/T-1")
        (repo / "b").write_text("b\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "the work")
        candidate = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "main")

        code, _, _ = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=candidate)],
            {"task_id": "T-1", "approve_role": "code_review", "sha": candidate},
            "--repo", str(repo))
        assert code == 0

        rows = [json.loads(line) for line in
                (gate.ledger / "ledger.jsonl").read_text().splitlines() if line.strip()]
        assert [r["action"] for r in rows[-2:]] == ["SIGNED", "MERGED"]
        assert rows[-2]["sha"] == candidate
        merge_commit = _git(repo, "rev-parse", "HEAD")
        assert rows[-1]["sha"] == merge_commit
        parents = _git(repo, "log", "-1", "--format=%P").split()
        assert candidate in parents, "main was not merged from the candidate"
        assert (repo / "b").is_file()

    def test_without_a_repo_the_approval_is_recorded_and_says_so(self, gate, capsys):
        code, _, _ = gate(
            [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=FAKE_SHA)],
            {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA})
        assert code == 0
        assert _actions(gate.ledger)[-2:] == ["SIGNED", "MERGED"]
        assert "no git merge was performed" in capsys.readouterr().err


class TestReferenceLedger:
    ROWS = [_row("T-1", "ADMITTED"), _row("T-1", "EXECUTED", sha=FAKE_SHA)]
    QUEUE = {"task_id": "T-1", "approve_role": "code_review", "sha": FAKE_SHA}

    def test_the_reference_ledger_is_refused_by_default(self, gate, tmp_path, capsys):
        """dx falls back to it when nothing is configured; a merge gated
        against synthetic rows is green over nothing."""
        ref = tmp_path / "devswarm-ledger-reference"
        code, before, after = gate(self.ROWS, self.QUEUE, ledger_dir=ref)
        assert code == 1 and after == before
        assert "reference ledger" in capsys.readouterr().err

    def test_the_flag_allows_it_deliberately(self, gate, tmp_path):
        ref = tmp_path / "devswarm-ledger-reference"
        code, _, _ = gate(self.ROWS, self.QUEUE, "--allow-reference-ledger",
                          ledger_dir=ref)
        assert code == 0

    def test_the_refusal_precedes_every_ledger_read(self, gate, tmp_path, capsys):
        """Refused before the chain is verified — no verifier output at all."""
        ref = tmp_path / "devswarm-ledger-reference"
        gate(self.ROWS, self.QUEUE, ledger_dir=ref)
        out = capsys.readouterr().out
        assert "Ledger chain verifies" not in out
