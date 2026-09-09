"""Appending to the ledger — ROADMAP §1.2.

Admission record: `docs/admissions/T-1102-ledger-append.md`.

This is the first `dx` code that mutates shared state, so the tests are about
the ways it could corrupt that state rather than the happy path: a row that
breaks the chain, a stale `prev_hash` from reusing a head after an append, a
lock taken by someone else, a merge into a dirty tree.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dx.ledger_writer import (
    LedgerWriteError,
    MergeLock,
    append_row,
    build_row,
    canonical,
    git_merge_no_ff,
    read_head,
    row_hash,
)

REF_LEDGER = Path("~/ai/devswarm-ledger-reference").expanduser()


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.invalid", "-c", "user.name=t", *args],
        check=True, capture_output=True, timeout=30,
    )


@pytest.fixture
def ledger(tmp_path):
    """A minimal two-row ledger in a git repo, built the way the real one is."""
    repo = tmp_path / "led"
    (repo / "queue").mkdir(parents=True)
    rows = []
    prev = "0" * 64
    for action, task in (("GENESIS", "GENESIS"), ("ADMITTED", "T-1")):
        row = build_row(
            action=action, task_id=task, prev_hash=prev,
            author_human="Ada Author", evidence=f"{action} row", ts="2026-01-01T00:00:00Z",
        )
        rows.append(row)
        prev = row_hash(row)
    (repo / "ledger.jsonl").write_text("".join(canonical(r) + "\n" for r in rows), encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


class TestCanonicalFormMatchesTheVerifier:
    """`verify_chain.py` is the contract. Writing reimplements its canonical
    form because the verifier ships in another repository and is not importable;
    this is the test that keeps the two from drifting apart silently."""

    @pytest.mark.skipif(
        not (REF_LEDGER / "tools" / "verify_chain.py").exists(),
        reason="devswarm-ledger-reference not cloned; CI clones it",
    )
    def test_it_is_byte_identical_to_verify_chain(self, tmp_path):
        row = build_row(
            action="SIGNED", task_id="T-9", prev_hash="a" * 64,
            evidence="unicode ✅ and \"quotes\"", ts="2026-01-01T00:00:00Z",
        )
        # Copied out rather than imported in place: importing from the
        # operator's clone leaves __pycache__ in it, and a test has no business
        # writing anything into the ledger repository.
        import shutil

        shutil.copy(REF_LEDGER / "tools" / "verify_chain.py", tmp_path / "verify_chain.py")
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import json,sys\n"
            f"sys.path.insert(0, {str(tmp_path)!r})\n"
            "from verify_chain import canonical\n"
            "print(canonical(json.load(sys.stdin)))\n",
            encoding="utf-8",
        )
        theirs = subprocess.run(
            [sys.executable, str(probe)], input=json.dumps(row),
            capture_output=True, text=True, timeout=30,
        )
        assert theirs.returncode == 0, theirs.stderr
        assert theirs.stdout.strip() == canonical(row), (
            "the writer's canonical form has drifted from the verifier's — every "
            "row written from now on would break the chain"
        )


class TestAppendRefusesToCorrupt:
    def test_a_row_with_the_wrong_prev_hash_is_refused(self, ledger):
        bad = build_row(action="SIGNED", task_id="T-1", prev_hash="b" * 64, evidence="x")
        with pytest.raises(LedgerWriteError, match="breaks the chain"):
            append_row(ledger, bad, commit=False)

    def test_the_refused_row_is_not_written(self, ledger):
        before = (ledger / "ledger.jsonl").read_text()
        with pytest.raises(LedgerWriteError):
            append_row(ledger, build_row(action="X", task_id="T", prev_hash="c" * 64, evidence="x"),
                       commit=False)
        assert (ledger / "ledger.jsonl").read_text() == before

    def test_a_row_missing_schema_fields_is_refused(self, ledger):
        head = read_head(ledger / "ledger.jsonl")
        with pytest.raises(LedgerWriteError, match="missing SCHEMA.md field"):
            append_row(ledger, {"action": "SIGNED", "prev_hash": head}, commit=False)

    def test_a_good_row_appends_and_moves_the_head(self, ledger):
        head = read_head(ledger / "ledger.jsonl")
        new = append_row(
            ledger,
            build_row(action="SIGNED", task_id="T-1", prev_hash=head, evidence="ok"),
            commit=False,
        )
        assert new != head
        assert read_head(ledger / "ledger.jsonl") == new


class TestTheSequencingTrap:
    """ROADMAP §1.2's named trap. Appending moves the head an approval binds to,
    so the second append must re-read it. Reusing the pre-append head writes a
    row whose prev_hash is a row stale — and breaks the chain the gate exists to
    protect."""

    def test_reusing_the_pre_append_head_is_refused(self, ledger):
        head = read_head(ledger / "ledger.jsonl")
        append_row(ledger, build_row(action="SIGNED", task_id="T-1", prev_hash=head,
                                     evidence="signed"), commit=False)
        with pytest.raises(LedgerWriteError, match="breaks the chain"):
            append_row(ledger, build_row(action="MERGED", task_id="T-1", prev_hash=head,
                                         evidence="merged"), commit=False)

    def test_re_reading_between_appends_keeps_the_chain_intact(self, ledger):
        head = read_head(ledger / "ledger.jsonl")
        after_signed = append_row(
            ledger, build_row(action="SIGNED", task_id="T-1", prev_hash=head, evidence="s"),
            commit=False,
        )
        append_row(
            ledger,
            build_row(action="MERGED", task_id="T-1", prev_hash=after_signed, evidence="m"),
            commit=False,
        )
        prev = "0" * 64
        for line in (ledger / "ledger.jsonl").read_text().splitlines():
            row = json.loads(line)
            assert row["prev_hash"] == prev, "chain broken"
            prev = row_hash(row)


class TestMergeLock:
    def test_it_refuses_a_lock_held_by_another_task(self, ledger):
        MergeLock(ledger, "T-1").acquire()
        with pytest.raises(LedgerWriteError, match="held by"):
            MergeLock(ledger, "T-2").acquire()

    def test_reacquiring_your_own_lock_is_not_an_error(self, ledger):
        MergeLock(ledger, "T-1").acquire()
        MergeLock(ledger, "T-1").acquire()  # must not raise

    def test_the_context_manager_releases(self, ledger):
        lock = MergeLock(ledger, "T-1")
        with lock:
            assert lock.path.exists()
        assert not lock.path.exists()

    def test_it_releases_even_when_the_body_raises(self, ledger):
        lock = MergeLock(ledger, "T-1")
        with pytest.raises(ValueError), lock:
            raise ValueError("merge blew up")
        assert not lock.path.exists(), "a crashed merge left the ledger locked"


class TestGitMergeNoFF:
    @pytest.fixture
    def work(self, tmp_path):
        repo = tmp_path / "work"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "f.txt").write_text("base\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        _git(repo, "checkout", "-qb", "feature")
        (repo / "f.txt").write_text("base\nwork\n")
        _git(repo, "commit", "-qam", "work")
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        _git(repo, "checkout", "-q", "main")
        return repo, sha

    def test_it_creates_a_real_merge_commit(self, work):
        repo, sha = work
        head = git_merge_no_ff(repo, sha, task_id="T-1")
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H %p"],
            capture_output=True, text=True, timeout=30,
        ).stdout.split()
        assert head == log[0], "the returned head is not the repo's HEAD"
        assert len(log[1:]) == 2, "--no-ff must produce a two-parent merge commit"
        assert sha.startswith(log[2]) or log[2].startswith(sha[:7]), (
            "the merged commit is not the task's sha"
        )

    def test_it_refuses_a_dirty_tree(self, work):
        repo, sha = work
        (repo / "untracked.txt").write_text("oops\n")
        with pytest.raises(LedgerWriteError, match="dirty working tree"):
            git_merge_no_ff(repo, sha, task_id="T-1")

    def test_it_refuses_a_non_repository(self, tmp_path):
        with pytest.raises(LedgerWriteError, match="not a git repository"):
            git_merge_no_ff(tmp_path, "abc123", task_id="T-1")
