"""dx.executed — the row that says work happened, bound to its commit.

Before this, ADMITTED was the last row a task ever received, so "work was done,
awaiting review" and "nothing happened" were the same state in the record.
Fourteen tasks sat in that ambiguity on 2026-09-21.

The risk in writing it automatically is the opposite one: a row that claims
work exists when it does not, or a second row that quietly rewrites a task's
history. Most of what is asserted here is what record_executed refuses.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dx.executed import CLOSED_ACTIONS, ExecutedError, record_executed
from dx.ledger_writer import (
    GENESIS_PREV,
    append_row,
    build_row,
    canonical,
    read_head,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


@pytest.fixture()
def scope(tmp_path: Path) -> Path:
    s = tmp_path / "scope"
    s.mkdir()
    _git(s, "init", "-q", "-b", "main")
    _git(s, "config", "user.email", "t@t")
    _git(s, "config", "user.name", "t")
    (s / "a.py").write_text("x\n")
    (s / "b.py").write_text("y\n")
    _git(s, "add", "-A")
    _git(s, "commit", "-qm", "the work the agent did")
    return s


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    led = tmp_path / "ledger"
    (led / "queue").mkdir(parents=True)
    _git(led, "init", "-q", "-b", "main")
    _git(led, "config", "user.email", "t@t")
    _git(led, "config", "user.name", "t")
    # A real ledger always opens with GENESIS; ledger_writer refuses an empty
    # file, and a fixture that skips it is testing a shape that cannot exist.
    genesis = build_row(action="GENESIS", task_id="GENESIS",
                        prev_hash=GENESIS_PREV, author_human="Ada Author",
                        evidence="genesis", ts="2026-01-01T00:00:00Z")
    (led / "ledger.jsonl").write_text(canonical(genesis) + "\n", encoding="utf-8")
    _git(led, "add", "-A")
    _git(led, "commit", "-qm", "genesis")
    return led


def _admit(ledger: Path, task_id: str, action: str = "ADMITTED") -> None:
    append_row(ledger, {
        "ts": "2026-09-21T00:00:00Z", "task_id": task_id,
        "author_seat": "S-Lead", "author_human": "Chris Wetzel",
        "reviewer_seat": None, "action": action, "sha": "",
        "evidence": f"mode=TEST {action}",
        "prev_hash": read_head(ledger / "ledger.jsonl"),
    })


def _last(ledger: Path) -> dict:
    lines = [l for l in (ledger / "ledger.jsonl").read_text().splitlines() if l.strip()]
    return json.loads(lines[-1])


class TestRecording:
    def test_it_records_the_commit_and_the_files(self, ledger: Path, scope: Path):
        _admit(ledger, "T-0100")

        got = record_executed(ledger, scope, "T-0100")

        row = _last(ledger)
        assert row["action"] == "EXECUTED"
        assert row["sha"].startswith(got)
        # The commit is the point: an EXECUTED row a reader cannot check is
        # the ambiguity it exists to remove.
        assert "commit=" in row["evidence"]
        assert "files=2" in row["evidence"]
        assert "the work the agent did" in row["evidence"]

    def test_the_author_comes_from_the_task_not_the_caller(
        self, ledger: Path, scope: Path,
    ):
        """A runner that could name someone else can forge provenance."""
        _admit(ledger, "T-0101")

        record_executed(ledger, scope, "T-0101")

        row = _last(ledger)
        assert row["author_human"] == "Chris Wetzel"
        assert row["author_seat"] == "S-Lead"
        # It records; it does not review. A reviewer seat here would be a
        # machine asserting someone looked at the work.
        assert row["reviewer_seat"] is None

    def test_the_chain_stays_intact(self, ledger: Path, scope: Path):
        _admit(ledger, "T-0102")
        before = read_head(ledger / "ledger.jsonl")

        record_executed(ledger, scope, "T-0102")

        assert _last(ledger)["prev_hash"] == before

    def test_the_queue_file_follows(self, ledger: Path, scope: Path):
        _admit(ledger, "T-0103")
        qf = ledger / "queue" / "T-0103.json"
        qf.write_text(json.dumps({"task_id": "T-0103", "state": "ADMITTED"}) + "\n")

        record_executed(ledger, scope, "T-0103")

        q = json.loads(qf.read_text())
        assert q["state"] == "EXECUTED"
        assert q["executed_sha"]


class TestRefusals:
    def test_a_task_with_no_rows_is_refused(self, ledger: Path, scope: Path):
        with pytest.raises(ExecutedError, match="no rows"):
            record_executed(ledger, scope, "T-9999")

    @pytest.mark.parametrize("closing", CLOSED_ACTIONS)
    def test_a_task_already_closed_is_refused(
        self, ledger: Path, scope: Path, closing: str,
    ):
        """A second EXECUTED, or one after SIGNED/MERGED, rewrites history."""
        _admit(ledger, "T-0200")
        _admit(ledger, "T-0200", action=closing)
        before = (ledger / "ledger.jsonl").read_text()

        with pytest.raises(ExecutedError, match="CORRECTION"):
            record_executed(ledger, scope, "T-0200")

        assert (ledger / "ledger.jsonl").read_text() == before, "wrote anyway"

    def test_a_task_with_no_author_is_refused(self, ledger: Path, scope: Path):
        append_row(ledger, {
            "ts": "2026-09-21T00:00:00Z", "task_id": "T-0201",
            "author_seat": None, "author_human": None, "reviewer_seat": None,
            "action": "ADMITTED", "sha": "", "evidence": "anonymous",
            "prev_hash": read_head(ledger / "ledger.jsonl"),
        })

        with pytest.raises(ExecutedError, match="author"):
            record_executed(ledger, scope, "T-0201")

    def test_a_scope_with_no_head_is_refused(self, ledger: Path, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        _git(empty, "init", "-q", "-b", "main")
        _admit(ledger, "T-0202")

        with pytest.raises(ExecutedError, match="no HEAD"):
            record_executed(ledger, empty, "T-0202")

    def test_it_never_writes_SIGNED_or_MERGED(self, ledger: Path, scope: Path):
        """The action is fixed. This path grants nothing and cannot be asked to."""
        _admit(ledger, "T-0203")
        record_executed(ledger, scope, "T-0203")
        actions = {
            json.loads(l)["action"]
            for l in (ledger / "ledger.jsonl").read_text().splitlines() if l.strip()
        }
        assert "SIGNED" not in actions
        assert "MERGED" not in actions
