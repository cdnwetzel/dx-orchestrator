"""Recover work pxx discarded when a run ended on a refused action.

WHY THIS EXISTS

pxx ties a safety net at session start — a ``pxx-pre/<ts>`` tag plus a stash —
and on any terminal outcome that is not COMPLETED it calls
``restore_safety_net``, which does ``git reset --hard <tag>``. That is correct
for a run that went wrong. It is wrong for a run that went *right* and then
touched one thing the governance kernel refuses.

Those are the same outcome to pxx and they are not the same thing. psguard is
expected to refuse: the pxx role card grants ``file.read``/``file.write`` and,
since 2026-09-21, ``shell.exec`` for pytest alone. An agent that finishes its
edits and then reaches for ``ls`` gets a fail-closed DENY, pxx scores the run a
failure, and the reset destroys finished work.

On 2026-09-21 that cost T-0022 156 lines across ``SPEC.md`` and
``src/sqlinv.py`` — a correct implementation of five review findings, gone
because the agent tried to run its own tests. It was recovered by hand from the
run directory. This module does that automatically, because the next one will
not be noticed.

WHAT MAKES IT SAFE

``session.py`` writes the run's ``diff.patch`` in ``_close_run_dir`` **before**
``restore_safety_net`` runs — deliberately, so a discarded run stays
inspectable. So the work is never actually lost; it is merely not in the
worktree. This reads that artifact back.

It refuses rather than guesses:

  * only when pxx exited non-zero — a successful run needs no salvage
  * only when the scope tree is clean, so nothing of the user's is overwritten
  * only when ``git apply --check`` passes
  * it never commits, and never changes the exit code

The result is left in the worktree, uncommitted and unverified, and the caller
says so out loud. Recovering work is not the same as vouching for it: the tests
did not run, which is usually the reason the run was refused in the first
place.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Anything smaller cannot be a real change and is not worth reporting.
_MIN_PATCH_BYTES = 1


@dataclass(frozen=True)
class Salvage:
    """What was recovered, or why nothing was."""

    patch: Path | None
    files: tuple[str, ...]
    lines: int
    reason: str

    @property
    def recovered(self) -> bool:
        return self.patch is not None


def _git(scope: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(scope), *args],
        capture_output=True, text=True, timeout=60,
    )


def pxx_runs_dir() -> Path | None:
    """pxx's own ``state_dir/runs``, asked of pxx rather than assumed.

    Hardcoding ``~/.local/state/pxx`` is the mistake that left the sandbox
    without a writable state dir in September: a second copy of a fact that
    already lives somewhere authoritative.
    """
    try:
        from pxx.config import Settings  # noqa: PLC0415

        runs = Path(Settings().state_dir) / "runs"
    except Exception:  # noqa: BLE001
        return None
    return runs if runs.is_dir() else None


def find_run_dir(started_at: float, runs: Path | None = None) -> Path | None:
    """The run directory pxx created for a run that began at ``started_at``.

    Matched by modification time rather than by parsing pxx's stdout, because
    dx hands the child an inherited fd — there is no captured output to parse,
    and adding capture would stop a long run printing progress as it happens.
    """
    runs = runs or pxx_runs_dir()
    # is_dir() here, not only in pxx_runs_dir(): a caller-supplied path is not
    # guaranteed to exist, and iterdir() on a missing directory raises — which
    # would turn a best-effort salvage into the crash it exists to avoid.
    if runs is None or not runs.is_dir():
        return None
    # A second of slack: the directory is created at session start, which is
    # marginally before the parent records its own timestamp on a loaded box.
    candidates = [
        d for d in runs.iterdir()
        if d.is_dir() and d.stat().st_mtime >= started_at - 1.0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def _patch_files(patch: Path) -> tuple[str, ...]:
    out = []
    for line in patch.read_text(errors="replace").splitlines():
        if line.startswith("+++ b/"):
            out.append(line[6:])
    return tuple(dict.fromkeys(out))


def salvage_discarded_work(
    scope: Path, started_at: float, runs: Path | None = None,
) -> Salvage:
    """Re-apply the diff pxx captured and then reset away. Never commits."""
    run_dir = find_run_dir(started_at, runs)
    if run_dir is None:
        return Salvage(None, (), 0, "no pxx run directory for this run")

    patch = run_dir / "diff.patch"
    if not patch.is_file() or patch.stat().st_size < _MIN_PATCH_BYTES:
        return Salvage(None, (), 0, "the run recorded no changes to recover")

    # If TRACKED files are modified the reset did not happen, or something
    # else wrote here; either way this is not ours to overwrite. Untracked
    # files are not a reason to refuse: pxx's reset leaves them behind (the
    # scratch script an agent wrote beside its real edits), a patch that does
    # not touch them cannot harm them, and one that would create them fails
    # `git apply --check` below. On 2026-09-22 a leftover debug_bytes.py made
    # this refuse to recover the nine test edits that were the run's actual
    # work.
    status = _git(scope, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return Salvage(None, (), 0, f"{scope} is not a readable git repository")
    if status.stdout.strip():
        return Salvage(None, (), 0,
                       "the scope has modified tracked files — left untouched")

    # Files the run CREATED survive pxx's reset (git reset --hard leaves
    # untracked files alone), so a patch that re-creates them fails
    # `--check` with "already exists in working directory". Those paths are
    # the run's own files, already on disk in the run's version: exclude
    # them and recover the rest. T-0052 (2026-09-22): a leftover
    # debug_bytes.py refused the 181-line loop diff that held the real work.
    excluded: list[str] = []
    check = _git(scope, "apply", "--check", str(patch))
    if check.returncode != 0:
        excluded = [
            m.group(1) for m in re.finditer(
                r"^error: (.+?): already exists in working directory$",
                check.stderr or "", re.MULTILINE)
            if (scope / m.group(1)).is_file()
        ]
        if excluded:
            check = _git(scope, "apply", "--check",
                         *(f"--exclude={p}" for p in excluded), str(patch))
    if check.returncode != 0:
        return Salvage(
            None, (), 0,
            f"the recorded patch does not apply to HEAD: "
            f"{(check.stderr or '').strip()[:120]}")

    applied = _git(scope, "apply", *(f"--exclude={p}" for p in excluded), str(patch))
    if applied.returncode != 0:
        return Salvage(None, (), 0,
                       f"apply failed: {(applied.stderr or '').strip()[:120]}")

    files = tuple(f for f in _patch_files(patch) if f not in excluded)
    lines = sum(
        1 for line in patch.read_text(errors="replace").splitlines()
        if (line.startswith(("+", "-"))
            and not line.startswith(("+++", "---")))
    )
    reason = "recovered"
    if excluded:
        reason += (" (already on disk from the run, left as found: "
                   + ", ".join(excluded) + ")")
    return Salvage(patch, files, lines, reason)


def report(s: Salvage) -> str:
    """What to tell a human. Deliberately not reassuring."""
    if not s.recovered:
        return f"   (nothing to recover: {s.reason})"
    return "\n".join([
        f"♻️  Recovered {s.lines} changed line(s) in {len(s.files)} file(s) that "
        f"pxx discarded when the run was refused:",
        *(f"      {f}" for f in s.files),
        f"   From {s.patch}",
        *([f"   {s.reason[len('recovered '):]}"] if s.reason != "recovered" else []),
        "   They are in the working tree, UNCOMMITTED and UNVERIFIED — the "
        "tests did not run, which is usually why the run was refused. Read "
        "them before you trust them.",
    ])


__all__ = ["Salvage", "salvage_discarded_work", "find_run_dir",
           "pxx_runs_dir", "report"]
