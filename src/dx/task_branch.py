"""Task branches: the git operations `dx run` and `dx merge` perform in
branch mode, each fail-closed.

WHY

Through 0.22.0 pxx committed every run on whatever branch the scope had
checked out — the pilot's `main` — so the work was on `main` before anyone
reviewed or signed it, and `dx merge` found the candidate already reachable
and recorded MERGED over a merge that never happened (AskPS finding F-003).
Plan decision D-3 (2026-10-08) makes sign-off a real gate: each run lives on
`<prefix><task_id>` created at the task's recorded base, the candidate is
pinned by a tag the moment it is recorded, the checkout always returns to the
base branch, and the merge at sign-off is a two-parent merge.

WHAT IS HERE AND WHAT IS NOT

Every function here raises :class:`BranchError` on any git failure; nothing
is best-effort. `cmd_run._git` stays best-effort for evidence collection by
design and is not used for these. pxx is untouched: its safety net (stash,
`pxx-pre/<ts>` tag, reset on a non-COMPLETED outcome) is branch-independent,
and dx never checks out anything while pxx runs.

Design: AskPS `docs/general/ps-coding-wp3-design.md` (Kimi design review
2026-10-12, PASS).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config_loader import BranchesConfig

GIT_TIMEOUT_S = 60
#: Tags that pin candidates: `candidate/<task_id>`, `-2`, `-3`… on a
#: re-admitted id, the convention pxx's `pxx-pre/` tags follow.
CANDIDATE_TAG_PREFIX = "candidate/"


class BranchError(Exception):
    """Refused, with the git facts a person needs. The message says what, if
    anything, was changed before the refusal."""


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stdout, stripped. Raises BranchError on
    a non-zero exit, a missing binary or a timeout: a branch operation that
    cannot be confirmed did not happen, as far as the caller may assume."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BranchError(f"git {' '.join(args)} in {repo}: {exc}") from exc
    if result.returncode != 0:
        raise BranchError(
            f"git {' '.join(args)} in {repo} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )
    return result.stdout.strip()


def current_branch(repo: Path) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    try:
        return git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    except BranchError:
        # symbolic-ref exits 1 on a detached HEAD; anything else (not a repo)
        # surfaces on the next call, which does not swallow.
        git(repo, "rev-parse", "--git-dir")
        return None


def status_porcelain(repo: Path) -> str:
    """`git status --porcelain`: modified tracked files and untracked files,
    ignored files excluded. Empty means clean."""
    return git(repo, "status", "--porcelain")


def is_commit(repo: Path, sha: str) -> bool:
    try:
        return git(repo, "cat-file", "-t", sha) == "commit"
    except BranchError:
        return False


def branch_exists(repo: Path, name: str) -> bool:
    try:
        git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
    except BranchError:
        return False
    return True


def stray_task_branches(repo: Path, cfg: BranchesConfig) -> list[str]:
    """Task branches that exist: each one is a run that did not return the
    checkout (§3.3 deletes the branch on every return path)."""
    out = git(repo, "for-each-ref", "--format=%(refname:short)",
              f"refs/heads/{cfg.prefix}")
    return [ln for ln in out.splitlines() if ln.strip()]


def candidate_tags(repo: Path) -> list[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)",
              f"refs/tags/{CANDIDATE_TAG_PREFIX}")
    return [ln for ln in out.splitlines() if ln.strip()]


def preflight(repo: Path, cfg: BranchesConfig) -> str:
    """The checkout is on the base branch and clean. Returns the base HEAD.

    Refused otherwise, naming the branch found: a HEAD read on a stray task
    branch would record the wrong base at admission, and a `switch -c` from
    a dirty tree would carry uncommitted work onto the task branch.
    """
    if not (repo / ".git").exists():
        raise BranchError(f"{repo} is not a git repository")
    on = current_branch(repo)
    if on != cfg.base:
        where = f"on branch {on!r}" if on else "on a detached HEAD"
        hint = (" — a previous run did not return the checkout; see `dx doctor` "
                "for the two commands that return it"
                if on and on.startswith(cfg.prefix) else "")
        raise BranchError(
            f"{repo} is {where}, not on the base branch {cfg.base!r}{hint}")
    dirty = status_porcelain(repo)
    if dirty:
        n = len(dirty.splitlines())
        raise BranchError(
            f"{repo} has {n} uncommitted path(s) on {cfg.base!r}; a run must "
            f"start from a clean checkout. First lines:\n{_head(dirty)}")
    return git(repo, "rev-parse", "HEAD")


def create_task_branch(repo: Path, cfg: BranchesConfig, task_id: str, base_sha: str) -> str:
    """`git switch -c <prefix><task_id> <base_sha>`. Returns the branch name.

    Refused when the branch exists (a previous run of this id did not
    return) or when ``base_sha`` is not a commit here (the recorded base is
    not where the run would happen)."""
    name = cfg.branch_for(task_id)
    if not is_commit(repo, base_sha):
        raise BranchError(
            f"the recorded base {base_sha[:12]} is not a commit in {repo}; "
            f"the run has no branch point")
    if branch_exists(repo, name):
        raise BranchError(
            f"branch {name!r} already exists in {repo}: a previous run of "
            f"{task_id} did not return the checkout. Look at it, then "
            f"`git -C {repo} branch -D {name}` to clear it.")
    git(repo, "switch", "-q", "-c", name, base_sha)
    return name


def tag_candidate(repo: Path, task_id: str, sha: str) -> str:
    """Pin ``sha`` as `candidate/<task_id>` (or `-2`, `-3`… when the name is
    taken by a different commit). Returns the tag name.

    Fail-closed: the tag is read back and must resolve to ``sha``; a tag
    that cannot be confirmed is reported as none, and the caller writes no
    EXECUTED row and keeps the branch (§3.3 step 6)."""
    taken = set(candidate_tags(repo))
    name = f"{CANDIDATE_TAG_PREFIX}{task_id}"
    n = 1
    while name in taken:
        if git(repo, "rev-parse", f"refs/tags/{name}^{{commit}}") == sha:
            return name  # already pinned by this very tag
        n += 1
        name = f"{CANDIDATE_TAG_PREFIX}{task_id}-{n}"
    git(repo, "tag", name, sha)
    resolved = git(repo, "rev-parse", f"refs/tags/{name}^{{commit}}")
    if resolved != sha:
        raise BranchError(
            f"tag {name} was written but resolves to {resolved[:12]}, not "
            f"{sha[:12]}; the candidate is not pinned")
    return name


def return_to_base(repo: Path, cfg: BranchesConfig, branch: str) -> None:
    """`git switch <base>` then `git branch -D <branch>`. Requires a clean
    tree; the caller decides what a dirty one means (§3.3 step 8: park)."""
    dirty = status_porcelain(repo)
    if dirty:
        raise BranchError(
            f"{repo} has uncommitted changes on {branch!r}; not switching "
            f"(switching would carry them onto {cfg.base!r} or fail half-way)")
    git(repo, "switch", "-q", cfg.base)
    if branch_exists(repo, branch):
        git(repo, "branch", "-q", "-D", branch)


def merge_conflicts(repo: Path, ours: str, theirs: str) -> list[str] | None:
    """Would merging ``theirs`` into ``ours`` conflict? Tested without
    touching the tree (`git merge-tree --write-tree`, git ≥ 2.38).

    Returns None for a clean merge, else the conflicting paths (possibly
    empty when git reported a conflict without naming files). Any other
    failure raises: a merge that cannot be tested is not assumed clean."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-tree", "--write-tree", "--name-only",
             ours, theirs],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BranchError(f"git merge-tree in {repo}: {exc}") from exc
    if result.returncode == 0:
        return None
    if result.returncode == 1:
        # The tree id, then the conflicted paths (--name-only), then a blank
        # line and git's informational messages, which are not paths.
        names: list[str] = []
        for ln in result.stdout.splitlines()[1:]:
            if not ln.strip():
                break
            names.append(ln.strip())
        return names
    raise BranchError(
        f"git merge-tree --write-tree in {repo} failed (exit {result.returncode}; "
        f"git ≥ 2.38 is required): {(result.stderr or result.stdout).strip()[:300]}")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise BranchError(
        f"git merge-base --is-ancestor in {repo} failed (exit {result.returncode}): "
        f"{result.stderr.strip()[:300]}")


@dataclass
class BranchRun:
    """What one branch-mode run did, for the bundle's `artifacts/branch.json`
    and the exit code."""
    base_branch: str
    base_sha: str
    branch: str
    candidate_tag: str | None = None
    head_after: str | None = None
    #: step 7 completed: checkout back on the base, task branch deleted
    restored: bool = False
    #: why the checkout was not restored, when it was not
    parked: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "mode": "branch",
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "candidate_tag": self.candidate_tag,
            "head_after": self.head_after,
            "restored": self.restored,
            "parked": self.parked,
        }


def _head(text: str, n: int = 5) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:n]) + (f"\n… {len(lines) - n} more" if len(lines) > n else "")


__all__ = [
    "CANDIDATE_TAG_PREFIX", "BranchError", "BranchRun", "branch_exists",
    "candidate_tags", "create_task_branch", "current_branch", "git",
    "is_ancestor", "is_commit", "merge_conflicts", "preflight",
    "return_to_base", "status_porcelain", "stray_task_branches", "tag_candidate",
]
