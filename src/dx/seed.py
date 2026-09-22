"""Start a rework from its predecessor's recorded work.

WHY. pxx stashes a dirty tree at session start and restores it at the end,
and its auto-commit takes only the session's own delta. So work salvaged
into the worktree is invisible to the next run, and the rework redoes the
whole task from a description of it. On 2026-09-22 attempts 13-19 of one
task each redid the previous one's edits from the review text and each
slipped once; attempt 15's 271-line diff -- exactly what was asked -- sat in
its run directory while the tree showed an older attempt's leftovers.

WHAT. Apply the predecessor's recorded diff (the bundle's run-diff.patch, as
pxx wrote it before its safety net reset the tree) to the clean tree and
commit it on the task line, authored by dx and labelled unverified. The
task's base_sha was recorded at ADMITTED, before this commit, so the
candidate review (base..candidate) covers every seeded line. Nobody but dx
authors this commit; the run's own commit follows it.

REFUSES rather than guesses: a dirty tree (modified tracked files), a patch
that does not apply, or an empty patch. A rework told to start from its
predecessor and unable to must not silently start from scratch.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .salvage import apply_patch_tolerant

SEED_AUTHOR = "dx <dx@devswarm.local>"


class SeedError(Exception):
    """The seed could not be applied; nothing was changed."""


@dataclass(frozen=True)
class Seed:
    sha: str
    files: tuple[str, ...]
    excluded: tuple[str, ...]


def _git(scope: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(scope), *args], capture_output=True, text=True, timeout=60,
    )


def _patch_paths(patch: Path) -> tuple[str, ...]:
    out = []
    for line in patch.read_text(errors="replace").splitlines():
        if line.startswith("+++ b/"):
            out.append(line[6:])
    return tuple(dict.fromkeys(out))


def seed_from_patch(scope: Path, patch: Path, *, task_id: str, from_task: str,
                    run_id: str = "") -> Seed:
    """Apply ``patch`` to a clean ``scope`` and commit it as dx. Raises SeedError."""
    if not patch.is_file() or not patch.read_text(errors="replace").strip():
        raise SeedError(f"no recorded diff to seed from at {patch}")
    status = _git(scope, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        raise SeedError(f"{scope} is not a readable git repository")
    if status.stdout.strip():
        raise SeedError("the scope has modified tracked files; refusing to seed over them")
    paths = _patch_paths(patch)
    if not paths:
        raise SeedError(f"{patch} names no files")
    excluded, err = apply_patch_tolerant(scope, patch)
    if err:
        raise SeedError(err)
    # Stage exactly the patch's paths -- including any the run had left on
    # disk (excluded from apply, but the run's files all the same) -- never
    # `add -A`, which would sweep in whatever else is untracked.
    add = _git(scope, "add", "--", *paths)
    if add.returncode != 0:
        raise SeedError(f"git add failed: {(add.stderr or '').strip()[:120]}")
    msg = (
        f"dx: seed {task_id} from {from_task}'s recorded run"
        + (f" {run_id}" if run_id else "")
        + " — UNVERIFIED\n\n"
        f"The predecessor's diff as pxx recorded it before its safety net reset\n"
        f"the tree, applied by dx so the rework starts from it rather than from a\n"
        f"description of it. Not reviewed, not tested here: the candidate review\n"
        f"of {task_id} (base..candidate) covers every line of this commit.\n"
        + (f"\nAlready on disk from the run, left as found: {', '.join(excluded)}\n"
           if excluded else "")
    )
    commit = _git(scope, "-c", f"user.name={SEED_AUTHOR.split(' <')[0]}",
                  "-c", f"user.email={SEED_AUTHOR.split('<')[1].rstrip('>')}",
                  "commit", "-q", "--no-verify", "-m", msg, "--", *paths)
    if commit.returncode != 0:
        raise SeedError(f"git commit failed: {(commit.stderr or '').strip()[:160]}")
    sha = (_git(scope, "rev-parse", "HEAD").stdout or "").strip()
    return Seed(sha=sha, files=paths, excluded=tuple(excluded))


__all__ = ["Seed", "SeedError", "seed_from_patch", "SEED_AUTHOR"]
