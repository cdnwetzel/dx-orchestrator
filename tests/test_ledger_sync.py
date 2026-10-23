"""A shared ledger: two writer clones syncing through one bare hub.

The ledger's own design (ledger_writer.MergeLock) is that two writers racing
for one chain "collide as a git conflict on push and the loser backs off".
These tests are about the ways that can go wrong: a stale head re-pointed, a
lost race leaving a fork, a diverged clone merged, a single-writer clone
suddenly touching a network.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import dx.ledger_writer as lw
from dx.ledger_writer import (
    LedgerSyncError,
    LedgerWriteError,
    MergeLock,
    append_row,
    build_row,
    canonical,
    pull_ledger,
    read_head,
    row_hash,
)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def _row(repo: Path, task: str, action: str = "ADMITTED") -> dict:
    return build_row(action=action, task_id=task, evidence=f"{action} {task}",
                     prev_hash=read_head(repo / "ledger.jsonl"), author_human="Ada Author")


def _rows(repo: Path) -> list[str]:
    return [ln for ln in (repo / "ledger.jsonl").read_text().splitlines() if ln.strip()]


@pytest.fixture
def hub(tmp_path):
    """A bare hub seeded with a two-row chain, and two shared writer clones."""
    seed = tmp_path / "seed"
    (seed / "queue").mkdir(parents=True)
    prev, lines = "0" * 64, []
    for action, task in (("GENESIS", "GENESIS"), ("ADMITTED", "T-1")):
        row = build_row(action=action, task_id=task, prev_hash=prev, author_human="Ada Author",
                        evidence=f"{action} row", ts="2026-01-01T00:00:00Z")
        lines.append(canonical(row))
        prev = row_hash(row)
    (seed / "ledger.jsonl").write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    (seed / "queue" / ".keep").write_text("")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    bare = tmp_path / "hub.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, timeout=30)
    clones = []
    for name in ("a", "b"):
        c = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(bare), str(c)], check=True, timeout=30)
        _git(c, "config", lw.SYNC_REMOTE_KEY, "origin")
        clones.append(c)
    return bare, clones[0], clones[1]


def _hub_rows(bare: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(bare), "show", "main:ledger.jsonl"],
                         check=True, capture_output=True, text=True, timeout=30).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


class TestPublishing:
    def test_a_write_reaches_the_hub_and_the_other_writer(self, hub):
        bare, a, b = hub
        append_row(a, _row(a, "T-2"))
        assert len(_hub_rows(bare)) == 3
        pull_ledger(b)
        assert _rows(b) == _rows(a)

    def test_a_clone_without_the_setting_never_touches_the_remote(self, hub):
        bare, a, _b = hub
        _git(a, "config", "--unset", lw.SYNC_REMOTE_KEY)
        append_row(a, _row(a, "T-2"))
        assert len(_hub_rows(bare)) == 2, "a single-writer clone stays local"
        assert len(_rows(a)) == 3


class TestARaceIsARefusedWriteNeverAFork:
    def test_a_row_built_on_a_moved_head_is_refused_not_repointed(self, hub):
        bare, a, b = hub
        stale = _row(b, "T-3")            # b read its head
        append_row(a, _row(a, "T-2"))     # a moves the shared head
        with pytest.raises(LedgerWriteError, match="breaks the chain"):
            append_row(b, stale)          # b's write fast-forwards first, then refuses
        assert len(_hub_rows(bare)) == 3 and len(_rows(b)) == 3

    def test_a_lost_push_race_leaves_nothing_recorded(self, hub, monkeypatch):
        bare, a, b = hub
        row = _row(b, "T-3")
        before = _git(b, "rev-parse", "HEAD")
        append_row(a, _row(a, "T-2"))     # a wins
        # b's fast-forward happened "before" a's push: skip it, so b commits on
        # the old head and loses the push race.
        monkeypatch.setattr(lw, "pull_ledger", lambda repo: None)
        with pytest.raises(LedgerSyncError, match="nothing was recorded"):
            append_row(b, row)
        assert _git(b, "rev-parse", "HEAD") == before, "b is back where the write found it"
        assert len(_rows(b)) == 2
        kept = _git(b, "for-each-ref", "--format=%(refname)", "refs/dx/unpublished/")
        assert kept, "the lost row is kept for inspection"
        assert len(_hub_rows(bare)) == 3, "the hub holds only the winner"

    def test_a_failed_push_of_any_kind_records_nothing(self, hub):
        bare, a, _b = hub
        hook = bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'hub refuses' >&2\nexit 1\n")
        hook.chmod(0o755)
        before = _git(a, "rev-parse", "HEAD")
        with pytest.raises(LedgerSyncError, match="hub refuses"):
            append_row(a, _row(a, "T-2"))
        assert _git(a, "rev-parse", "HEAD") == before and len(_rows(a)) == 2


class TestAnAmbiguousPush:
    def _fail_push(self, monkeypatch, *, then_accept: bool, readable: bool = True):
        real = lw._git

        def fake(repo, *args, check=True):
            if args and args[0] == "push":
                if then_accept:                      # the hub took it; the reply was lost
                    real(repo, *args, check=False)
                raise LedgerWriteError("git push timed out")
            if args and args[0] == "fetch" and not readable:
                raise LedgerWriteError("git fetch timed out")
            return real(repo, *args, check=check)

        monkeypatch.setattr(lw, "_git", fake)

    def test_accepted_with_a_lost_reply_is_success(self, hub, monkeypatch):
        bare, a, _b = hub
        self._fail_push(monkeypatch, then_accept=True)
        append_row(a, _row(a, "T-2"))
        assert len(_hub_rows(bare)) == 3 and len(_rows(a)) == 3

    def test_a_timed_out_push_is_unknown_not_nothing_recorded(self, hub, monkeypatch):
        bare, a, _b = hub
        before = _git(a, "rev-parse", "HEAD")
        self._fail_push(monkeypatch, then_accept=False)
        with pytest.raises(LedgerSyncError, match="UNKNOWN"):
            append_row(a, _row(a, "T-2"))
        assert _git(a, "rev-parse", "HEAD") == before and len(_hub_rows(bare)) == 2

    def test_a_push_that_lands_after_the_read_back_is_not_duplicated(self, hub, monkeypatch):
        # Codex review: the killed push lands AFTER dx looked. dx must have
        # said UNKNOWN (never "nothing recorded"), and the late row is then
        # simply fast-forwarded in, not written twice.
        bare, a, _b = hub
        self._fail_push(monkeypatch, then_accept=False)
        with pytest.raises(LedgerSyncError, match="UNKNOWN") as err:
            append_row(a, _row(a, "T-2"))
        assert "nothing was recorded" not in str(err.value)
        monkeypatch.undo()
        late = _git(a, "for-each-ref", "--format=%(objectname)", "refs/dx/unpublished/")
        _git(a, "push", "-q", "origin", f"{late}:refs/heads/main")   # the late landing
        pull_ledger(a)
        assert [r for r in _rows(a) if '"T-2"' in r] and len(_rows(a)) == 3

    def test_an_unreadable_hub_is_reported_as_unknown(self, hub, monkeypatch):
        _bare, a, _b = hub
        before = _git(a, "rev-parse", "HEAD")
        self._fail_push(monkeypatch, then_accept=False, readable=False)
        with (a / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(canonical(_row(a, "T-2")) + "\n")
        with pytest.raises(LedgerSyncError, match="UNKNOWN"):
            lw._commit_shared(a, ["ledger.jsonl"], "ADMITTED T-2")  # past the pull
        assert _git(a, "rev-parse", "HEAD") == before


class TestPulling:
    def test_a_diverged_clone_is_refused_never_merged(self, hub):
        _bare, a, b = hub
        append_row(a, _row(a, "T-2"))
        # b gets a local-only commit of its own (as if synced off by hand)
        _git(b, "config", "--unset", lw.SYNC_REMOTE_KEY)
        append_row(b, _row(b, "T-9"))
        _git(b, "config", lw.SYNC_REMOTE_KEY, "origin")
        local = _git(b, "rev-parse", "HEAD")
        with pytest.raises(LedgerSyncError, match="diverged"):
            pull_ledger(b)
        assert _git(b, "rev-parse", "HEAD") == local

    def test_a_commit_left_unpublished_is_quarantined_never_published(self, hub):
        # Codex review: an interrupted write never reported success, so
        # publishing it later could record an operation twice.
        bare, _a, b = hub
        _git(b, "config", "--unset", lw.SYNC_REMOTE_KEY)
        append_row(b, _row(b, "T-2"))     # committed, never pushed (a crash, say)
        _git(b, "config", lw.SYNC_REMOTE_KEY, "origin")
        with pytest.raises(LedgerSyncError, match="never reached"):
            pull_ledger(b)
        assert len(_hub_rows(bare)) == 2 and len(_rows(b)) == 2
        assert _git(b, "for-each-ref", "--format=%(refname)", "refs/dx/unpublished/")

    def test_a_changed_queue_view_does_not_block_the_next_write(self, hub):
        # Codex review: dx updates queue/<task>.json after a row without
        # committing it; that is host-local state, not the chain.
        bare, a, _b = hub
        q = a / "queue" / "T-1.json"
        q.write_text('{"state": "ADMITTED"}\n')
        _git(a, "config", "--unset", lw.SYNC_REMOTE_KEY)
        _git(a, "add", "queue/T-1.json")
        _git(a, "commit", "-qm", "track a queue view")
        _git(a, "push", "-q", "origin", "HEAD:main")
        _git(a, "config", lw.SYNC_REMOTE_KEY, "origin")
        q.write_text('{"state": "EXECUTED"}\n')      # dirty, tracked
        append_row(a, _row(a, "T-2"))
        with MergeLock(a, "T-2"):
            q.write_text('{"state": "MERGED"}\n')    # updated inside the lock
        assert len(_hub_rows(bare)) == 3
        assert "UNLOCK T-2" in _git(bare, "log", "-1", "--format=%s", "main")
        assert q.read_text() == '{"state": "MERGED"}\n', "the local view is left alone"

    def test_a_dirty_clone_is_refused(self, hub):
        _bare, a, _b = hub
        with (a / "ledger.jsonl").open("a") as fh:
            fh.write("{}\n")
        with pytest.raises(LedgerSyncError, match="uncommitted"):
            pull_ledger(a)

    def test_a_detached_clone_is_refused(self, hub):
        _bare, a, _b = hub
        _git(a, "checkout", "-q", "--detach")
        with pytest.raises(LedgerSyncError, match="not on a branch"):
            pull_ledger(a)


class TestTheMergeLockAcrossWriters:
    def test_a_lock_taken_on_one_writer_is_seen_by_the_other(self, hub):
        bare, a, b = hub
        with MergeLock(a, "T-1"):
            with pytest.raises(LedgerWriteError, match="held by 'T-1'"):
                MergeLock(b, "T-2").acquire()
        pull_ledger(b)
        assert not (b / "queue" / "MERGE_LOCK.json").exists(), "the release was published too"
        assert "UNLOCK T-1" in _git(bare, "log", "-1", "--format=%s", "main")
