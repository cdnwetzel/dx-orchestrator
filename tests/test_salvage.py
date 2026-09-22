"""dx.salvage — recovering work pxx discarded after a refused action.

The scenario these cover, from 2026-09-21: a governed run wrote 156 correct
lines across two files, then called a shell command the kernel refuses. pxx
scored the run a failure and `restore_safety_net` reset --hard to its pxx-pre
tag. The work only survived because `_close_run_dir` writes diff.patch first.

The risk in automating that recovery is the opposite of losing work: silently
overwriting something. So most of what is asserted here is what salvage
REFUSES to do.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from dx.salvage import (
    Salvage,
    find_run_dir,
    report,
    salvage_discarded_work,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "scope"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "src.py").write_text("original\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _run_dir_with_patch(tmp_path: Path, repo: Path, new_body: str) -> Path:
    """A pxx-shaped run directory whose diff.patch holds an unapplied edit."""
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    d = runs / "20260921T182259Z-deadbeef"
    d.mkdir()
    (repo / "src.py").write_text(new_body)
    patch = _git(repo, "diff").stdout
    _git(repo, "checkout", "--", ".")          # the reset pxx would have done
    (d / "diff.patch").write_text(patch)
    return runs


class TestRecovery:
    def test_work_reset_away_is_restored(self, tmp_path: Path, repo: Path):
        started = time.time()
        runs = _run_dir_with_patch(tmp_path, repo, "the agent's work\n")
        assert (repo / "src.py").read_text() == "original\n", "precondition"

        s = salvage_discarded_work(repo, started, runs)

        assert s.recovered, s.reason
        assert (repo / "src.py").read_text() == "the agent's work\n"
        assert s.files == ("src.py",)
        assert s.lines == 2          # one - and one +

    def test_it_does_not_commit(self, tmp_path: Path, repo: Path):
        """Recovered work is for a human to read, not something dx vouches for."""
        started = time.time()
        head_before = _git(repo, "rev-parse", "HEAD").stdout
        runs = _run_dir_with_patch(tmp_path, repo, "work\n")

        salvage_discarded_work(repo, started, runs)

        assert _git(repo, "rev-parse", "HEAD").stdout == head_before
        assert _git(repo, "status", "--porcelain").stdout.strip(), "left dirty"


class TestRefusals:
    """Each of these is a way automatic recovery could destroy something."""

    def test_an_untracked_leftover_does_not_block_salvage(self, tmp_path: Path, repo: Path):
        """pxx's reset leaves untracked files behind -- the scratch script an
        agent wrote beside its real edits. On 2026-09-22 a debug_bytes.py made
        salvage refuse to recover the nine test edits that were the run's
        actual work. A patch that does not touch the file cannot harm it."""
        runs = tmp_path / "runs"
        run_dir = runs / "20260922T162541Z-abcd"
        run_dir.mkdir(parents=True)
        (repo / "a.py").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        (repo / "a.py").write_text("x\ny\n")
        patch = _git(repo, "diff").stdout
        _git(repo, "checkout", "--", "a.py")
        (run_dir / "diff.patch").write_text(patch)
        (repo / "debug_bytes.py").write_text("print('scratch')\n")   # untracked, stays

        s = salvage_discarded_work(repo, time.time() - 5, runs)

        assert s.recovered, s.reason
        assert (repo / "a.py").read_text() == "x\ny\n"
        assert (repo / "debug_bytes.py").is_file(), "the leftover is not ours to remove"

    def test_a_dirty_scope_is_left_alone(self, tmp_path: Path, repo: Path):
        started = time.time()
        runs = _run_dir_with_patch(tmp_path, repo, "work\n")
        (repo / "src.py").write_text("SOMEONE ELSE WAS HERE\n")

        s = salvage_discarded_work(repo, started, runs)

        assert not s.recovered
        assert "modified tracked files" in s.reason
        assert (repo / "src.py").read_text() == "SOMEONE ELSE WAS HERE\n"

    def test_a_patch_that_does_not_apply_is_refused(
        self, tmp_path: Path, repo: Path,
    ):
        started = time.time()
        runs = _run_dir_with_patch(tmp_path, repo, "work\n")
        # HEAD moves on, so the recorded patch no longer fits.
        (repo / "src.py").write_text("a different base entirely\n")
        _git(repo, "commit", "-qam", "moved on")

        s = salvage_discarded_work(repo, started, runs)

        assert not s.recovered
        assert "does not apply" in s.reason

    def test_an_empty_patch_recovers_nothing(self, tmp_path: Path, repo: Path):
        started = time.time()
        runs = tmp_path / "runs"
        (runs / "20260921T000000Z-empty").mkdir(parents=True)
        (runs / "20260921T000000Z-empty" / "diff.patch").write_text("")

        s = salvage_discarded_work(repo, started, runs)

        assert not s.recovered
        assert "no changes" in s.reason

    def test_a_run_directory_from_BEFORE_this_run_is_ignored(
        self, tmp_path: Path, repo: Path,
    ):
        """The dangerous one: re-applying some earlier run's diff.

        Matching is by mtime, so a stale directory must not be picked up — that
        would resurrect work from a different task into this scope.
        """
        runs = _run_dir_with_patch(tmp_path, repo, "work from an older run\n")
        old = next(runs.iterdir())
        import os
        os.utime(old, (time.time() - 3600, time.time() - 3600))

        started = time.time()          # this run began AFTER that directory
        s = salvage_discarded_work(repo, started, runs)

        assert not s.recovered
        assert "no pxx run directory" in s.reason
        assert (repo / "src.py").read_text() == "original\n"

    def test_a_missing_runs_dir_is_not_an_error(self, tmp_path: Path, repo: Path):
        s = salvage_discarded_work(repo, time.time(), tmp_path / "nope")
        assert not s.recovered

    def test_a_non_repo_scope_is_refused(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        runs = tmp_path / "runs"
        (runs / "20260921T000000Z-x").mkdir(parents=True)
        (runs / "20260921T000000Z-x" / "diff.patch").write_text("diff --git\n")

        s = salvage_discarded_work(plain, time.time() - 5, runs)

        assert not s.recovered
        assert "git repository" in s.reason


class TestReport:
    def test_it_says_the_work_is_unverified(self, tmp_path: Path, repo: Path):
        started = time.time()
        runs = _run_dir_with_patch(tmp_path, repo, "work\n")
        s = salvage_discarded_work(repo, started, runs)

        text = report(s)

        # Recovering is not vouching. The tests did not run — that is usually
        # the very reason the run was refused.
        assert "UNVERIFIED" in text
        assert "UNCOMMITTED" in text
        assert "src.py" in text

    def test_nothing_recovered_reads_as_nothing_recovered(self):
        assert "nothing to recover" in report(Salvage(None, (), 0, "because"))


class TestFindRunDir:
    def test_newest_matching_directory_wins(self, tmp_path: Path):
        runs = tmp_path / "runs"
        runs.mkdir()
        started = time.time()
        for name in ("20260921T000001Z-a", "20260921T000002Z-b"):
            (runs / name).mkdir()
            time.sleep(0.01)

        found = find_run_dir(started, runs)

        assert found is not None
        assert found.name == "20260921T000002Z-b"


def test_a_file_the_run_left_behind_does_not_block_the_rest(tmp_path):
    """git reset --hard leaves untracked files; a loop diff that re-creates
    one used to fail --check outright (T-0052). It is excluded and reported."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "b.py").write_text("x = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    runs = tmp_path / "runs"
    run = runs / "20260922T182225Z-loop-abc"
    run.mkdir(parents=True)
    patch = (
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n"
        "diff --git a/debug.py b/debug.py\nnew file mode 100644\n--- /dev/null\n+++ b/debug.py\n"
        "@@ -0,0 +1 @@\n+print(1)\n"
    )
    (run / "diff.patch").write_text(patch)
    (repo / "debug.py").write_text("print(1)\n")   # the run's leftover, untracked
    s = salvage_discarded_work(repo, started_at=0.0, runs=runs)
    assert s.recovered, s.reason
    assert (repo / "b.py").read_text() == "x = 1\n"
    assert s.files == ("b.py",) and "debug.py" in s.reason
    assert "left as found" in report(s)
