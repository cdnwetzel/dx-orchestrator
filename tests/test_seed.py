"""dx.seed: a rework starts from its predecessor's recorded diff, committed as dx."""
import subprocess
from pathlib import Path

import pytest

from dx.seed import SEED_AUTHOR, SeedError, seed_from_patch


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "b.py").write_text("x = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return repo


PATCH = (
    "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n"
    "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
    "@@ -0,0 +1 @@\n+print(1)\n"
)


def test_seed_applies_commits_as_dx_and_leaves_the_tree_clean(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    patch = tmp_path / "20260922T1-loop-abc" / "artifacts" / "run-diff.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text(PATCH)
    seed = seed_from_patch(repo, patch, task_id="T-0053", from_task="T-0052", run_id="r1")
    assert seed.files == ("b.py", "new.py") and not seed.excluded
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == seed.sha != base
    assert _git(repo, "status", "--porcelain").stdout == ""
    log = _git(repo, "log", "-1", "--format=%an <%ae>%n%B").stdout
    assert log.startswith(SEED_AUTHOR)
    assert "seed T-0053 from T-0052" in log and "UNVERIFIED" in log
    assert (repo / "b.py").read_text() == "x = 1\n" and (repo / "new.py").exists()
    # base..HEAD is exactly the seed: the candidate review will show it
    assert "+x = 1" in _git(repo, "diff", base, "HEAD").stdout


def test_seed_refuses_a_dirty_tree_and_changes_nothing(tmp_path):
    repo = _repo(tmp_path)
    (repo / "b.py").write_text("dirty\n")
    patch = tmp_path / "p.patch"
    patch.write_text(PATCH)
    with pytest.raises(SeedError, match="modified tracked files"):
        seed_from_patch(repo, patch, task_id="T-1", from_task="T-0")
    assert (repo / "b.py").read_text() == "dirty\n"


def test_seed_refuses_a_patch_that_does_not_apply(tmp_path):
    repo = _repo(tmp_path)
    (repo / "b.py").write_text("y = 9\n")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "moved on")
    head = _git(repo, "rev-parse", "HEAD").stdout
    patch = tmp_path / "p.patch"
    patch.write_text(PATCH)
    with pytest.raises(SeedError, match="does not apply"):
        seed_from_patch(repo, patch, task_id="T-1", from_task="T-0")
    assert _git(repo, "rev-parse", "HEAD").stdout == head


def test_seed_refuses_an_empty_or_missing_patch(tmp_path):
    repo = _repo(tmp_path)
    empty = tmp_path / "empty.patch"
    empty.write_text("\n")
    with pytest.raises(SeedError, match="no recorded diff"):
        seed_from_patch(repo, empty, task_id="T-1", from_task="T-0")
    with pytest.raises(SeedError, match="no recorded diff"):
        seed_from_patch(repo, tmp_path / "absent.patch", task_id="T-1", from_task="T-0")


def test_a_file_the_run_left_on_disk_is_committed_as_found(tmp_path):
    """The predecessor's scratch file survived pxx's reset; the patch would
    re-create it. It is left as found, staged and committed with the rest."""
    repo = _repo(tmp_path)
    (repo / "new.py").write_text("print(1)\n")
    patch = tmp_path / "p.patch"
    patch.write_text(PATCH)
    seed = seed_from_patch(repo, patch, task_id="T-1", from_task="T-0")
    assert seed.excluded == ("new.py",)
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert "new.py" in _git(repo, "show", "--stat", "--format=", "HEAD").stdout
