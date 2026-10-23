"""Appending rows to a devswarm-ledger, and the merge lock that serialises it.

This is the first code in `dx` that writes to shared state, so it carries the
ledger's invariants rather than the CLI's:

**RL-009 — append only.** Rows are appended and committed. Nothing here rewrites,
amends, reorders or deletes. A wrong row is corrected by appending a
``CORRECTION`` row that names it by hash; that is a human decision, not something
this module does on its own.

**Canonical form is the verifier's, not ours.** :func:`canonical` reimplements
``tools/verify_chain.py``'s function because writing needs it too, and
``verify_chain.py`` is the contract rather than an importable library — it ships
in a separate repository that may not be on ``sys.path``. A test asserts the two
produce byte-identical output for the same row. If they ever diverge that test
fails, instead of the chain breaking where somebody has to debug it.

**A shared chain syncs through one hub, opt-in per clone.** A clone whose git
config sets ``dx.ledgerRemote`` (e.g. ``origin``) is one of several writers of
one chain: every write first fast-forwards from that remote and afterwards
pushes to it. The hub is never a writer's working repository. A push that
fails for ANY reason leaves the write unrecorded: the row is kept under
``refs/dx/unpublished/`` and the clone goes back to where the write found it,
so a lost race is a refused write, never a fork. Nothing here merges or
rebases: an append-only chain that diverged is a person's problem. A clone
without the setting (the reference ledger, a single-writer box) behaves as
before.

**The head moves under you.** Appending a row changes the ledger head — which is
the value an approval signature binds to. So verification happens *before* any
append, and nothing re-verifies an old signature afterwards. The ``SIGNED`` row
records the head the signature was actually checked against, so the sequence
stays auditable after the head has moved on.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS_PREV = "0" * 64

#: SCHEMA.md's row fields. Every row carries all of them; absent values are
#: null rather than omitted, so the canonical form is stable across writers.
ROW_FIELDS = (
    "ts",
    "task_id",
    "author_seat",
    "author_human",
    "reviewer_seat",
    "action",
    "sha",
    "evidence",
    "prev_hash",
)

GIT_TIMEOUT_S = 60


class LedgerWriteError(RuntimeError):
    """A ledger write was refused, or could not be completed safely."""


class LedgerSyncError(LedgerWriteError):
    """The shared ledger could not be synced; nothing was recorded."""


#: The ledger clone's own git config key naming the remote it syncs with.
SYNC_REMOTE_KEY = "dx.ledgerRemote"

#: What the sync commits, and so what must be clean before it fast-forwards.
#: The queue/<task>.json views and approvals/ are the executing host's local
#: state (a task lives on the host that runs it); git's own fast-forward still
#: refuses to overwrite a local change there.
SYNCED_PATHS = ("ledger.jsonl", "queue/MERGE_LOCK.json")


def canonical(row: dict[str, Any]) -> str:
    """SCHEMA.md canonical form: key-sorted, compact separators, ASCII-escaped."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()


def read_head(ledger: Path) -> str:
    """The hash of the last row — the value approvals bind to.

    Deliberately does not verify the chain: ``dx merge`` has already delegated
    that to ``tools/verify_chain.py``, and a second, subtly different verifier
    living here is how two implementations drift apart.
    """
    if not ledger.exists():
        raise LedgerWriteError(f"ledger.jsonl not found at {ledger}")
    last: str | None = None
    with ledger.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last = line.rstrip("\n")
    if last is None:
        raise LedgerWriteError(f"{ledger} is empty — even a fresh ledger has a genesis row")
    try:
        return row_hash(json.loads(last))
    except json.JSONDecodeError as exc:
        raise LedgerWriteError(f"{ledger}: last row is not valid JSON: {exc}") from exc


def build_row(
    *,
    action: str,
    task_id: str,
    evidence: str,
    prev_hash: str,
    author_seat: str | None = None,
    author_human: str | None = None,
    reviewer_seat: str | None = None,
    sha: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """A SCHEMA.md row with every field present."""
    return {
        "ts": ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_id": task_id,
        "author_seat": author_seat,
        "author_human": author_human,
        "reviewer_seat": reviewer_seat,
        "action": action,
        "sha": sha,
        "evidence": evidence,
        "prev_hash": prev_hash,
    }


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerWriteError(f"git {' '.join(args)} failed in {repo}: {exc}") from exc
    if check and result.returncode != 0:
        err = result.stderr.strip()
        if "not a git repository" in err:
            # Deliberately NOT auto-`git init`. The ledger's durability IS its
            # git history -- append-only, hash-chained, committed -- so creating
            # an empty repository here would make "no history" and "valid but
            # empty" indistinguishable, and would do it silently, mid-merge.
            #
            # The likeliest cause is a ledger path pointing somewhere it should
            # not, and quietly initialising that directory turns a
            # misconfiguration into a plausible-looking ledger. Say what is
            # wrong and let a person decide.
            raise LedgerWriteError(
                f"{repo} is not a git repository.\n"
                f"The ledger's durability depends on its git history, so this is "
                f"not created automatically.\n"
                f"  * if the path is wrong, fix DX_LEDGER_REPO or `ledger.repo` "
                f"in the hardware manifest;\n"
                f"  * if this really is a new ledger, run `git init` there "
                f"yourself, deliberately."
            )
        raise LedgerWriteError(
            f"git {' '.join(args)} failed in {repo} (exit {result.returncode}): {err}"
        )
    return result


def sync_remote(repo: Path) -> str | None:
    """The remote this clone syncs with, or None when it is not shared."""
    got = _git(repo, "config", "--get", SYNC_REMOTE_KEY, check=False)
    value = got.stdout.strip() if got.returncode == 0 else ""
    return value or None


def _sync_branch(repo: Path) -> str:
    got = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if got.returncode != 0 or not got.stdout.strip():
        raise LedgerSyncError(f"{repo} is not on a branch; a shared ledger syncs a branch")
    return got.stdout.strip()


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _quarantine(repo: Path, before: str) -> str:
    """Keep HEAD's commits under refs/dx/unpublished/ and return the clone to
    ``before``. --keep, not --hard: only those commits are undone, and git
    refuses rather than overwrite anything else in the tree."""
    head = _rev(repo, "HEAD")
    keep = f"refs/dx/unpublished/{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{head[:12]}"
    _git(repo, "update-ref", keep, head)
    _git(repo, "reset", "--keep", before)
    return keep


def _hub_has(repo: Path, remote: str, branch: str, commit: str) -> bool | None:
    """Whether the hub's branch contains ``commit``; None when it cannot be read."""
    try:
        fetched = _git(repo, "fetch", "--quiet", remote, f"refs/heads/{branch}", check=False)
    except LedgerWriteError:
        return None
    if fetched.returncode != 0:
        return None
    return _git(repo, "merge-base", "--is-ancestor", commit, "FETCH_HEAD",
                check=False).returncode == 0


def _publish(repo: Path, remote: str, branch: str, before: str) -> None:
    """Push HEAD. A push that errors or times out is reconciled against the
    hub: accepted after all is success; otherwise the commits are quarantined
    (:func:`_quarantine`) and this raises -- the write is not recorded."""
    head = _rev(repo, "HEAD")
    finished = True  # git push ran to completion and reported its result
    try:
        pushed = _git(repo, "push", "--porcelain", remote, f"HEAD:refs/heads/{branch}",
                      check=False)
        detail = (pushed.stderr or pushed.stdout).strip()[-300:]
        if pushed.returncode == 0:
            return
    except LedgerWriteError as exc:  # a timeout or a git that would not start
        detail = str(exc)[-300:]
        finished = False
    accepted = _hub_has(repo, remote, branch, head)
    if accepted:
        return
    keep = _quarantine(repo, before)
    # Only a push git finished and rejected is "nothing recorded". A killed
    # push may still land after the read-back (Codex review), so its absence
    # proves nothing.
    if accepted is None or not finished:
        raise LedgerSyncError(
            f"the push to {remote}/{branch} did not complete or the hub could not "
            f"be read back, so whether it took is UNKNOWN. The commit is kept at {keep} "
            f"and this clone is back where the write found it. Before re-running, "
            f"pull and check whether the row is already on the hub: {detail}"
        )
    raise LedgerSyncError(
        f"could not publish to {remote}/{branch}, so nothing was recorded "
        f"(the commit is kept at {keep}): {detail}\n"
        f"Another writer probably appended first. Re-run the operation."
    )


def pull_ledger(repo: Path) -> None:
    """Fast-forward a shared clone from its remote (no-op when not shared).

    Refuses uncommitted changes to the synced files and a diverged history.
    Commits that are only local (an interrupted write) are quarantined, never
    published: their operation never reported success."""
    remote = sync_remote(repo)
    if remote is None:
        return
    branch = _sync_branch(repo)
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no", "--",
                 *SYNCED_PATHS).stdout.strip()
    if dirty:
        raise LedgerSyncError(
            f"{repo} has uncommitted changes to {', '.join(SYNCED_PATHS)}; refusing to sync")
    _git(repo, "fetch", "--quiet", remote, f"refs/heads/{branch}")
    local, upstream = _rev(repo, "HEAD"), _rev(repo, "FETCH_HEAD")
    if local == upstream:
        return
    if _git(repo, "merge-base", "--is-ancestor", local, upstream, check=False).returncode == 0:
        _git(repo, "merge", "--ff-only", "--quiet", upstream)
        return
    if _git(repo, "merge-base", "--is-ancestor", upstream, local, check=False).returncode == 0:
        keep = _quarantine(repo, upstream)
        raise LedgerSyncError(
            f"{repo} had commits that never reached {remote}/{branch} (an interrupted "
            f"write?). They are kept at {keep}, not published, and the clone now "
            f"matches the hub. Check whether that operation needs re-running."
        )
    raise LedgerSyncError(
        f"{repo} and {remote}/{branch} have diverged. An append-only chain is "
        f"never merged or rebased automatically: compare the two and resolve it "
        f"deliberately."
    )


def _commit(repo: Path, paths: list[str], message: str, *, add_all: bool = False) -> None:
    _git(repo, "add", *(["-A"] if add_all else []), *paths)
    _git(repo, "-c", "user.name=dx", "-c", "user.email=dx@localhost", "commit", "-m", message)


def _commit_shared(repo: Path, paths: list[str], message: str, *, add_all: bool = False) -> None:
    """Commit, then publish when the clone is shared (after :func:`pull_ledger`)."""
    remote = sync_remote(repo)
    if remote is None:  # not shared: exactly the old behaviour (an unborn repo too)
        _commit(repo, paths, message, add_all=add_all)
        return
    before = _rev(repo, "HEAD")
    _commit(repo, paths, message, add_all=add_all)
    _publish(repo, remote, _sync_branch(repo), before)


def append_row(ledger_repo: Path, row: dict[str, Any], *, commit: bool = True) -> str:
    """Append ``row`` and return the new head hash.

    The row must already carry the correct ``prev_hash``; this refuses to write
    one that would break the chain rather than appending it and leaving the
    repository in a state whose verifier fails. On a shared clone the check
    runs AFTER fast-forwarding, so a row built on a head another writer has
    since moved is refused: its author re-reads, it is never re-pointed (a
    SIGNED row's signature binds to the head it was verified against).
    """
    ledger = ledger_repo / "ledger.jsonl"
    if commit:
        pull_ledger(ledger_repo)
    current = read_head(ledger)
    if row.get("prev_hash") != current:
        raise LedgerWriteError(
            f"refusing to append a row that breaks the chain: prev_hash is "
            f"{str(row.get('prev_hash'))[:16]}… but the current head is "
            f"{current[:16]}…. Re-read the head and rebuild the row."
        )
    missing = [f for f in ROW_FIELDS if f not in row]
    if missing:
        raise LedgerWriteError(f"row is missing SCHEMA.md field(s): {missing}")

    line = canonical(row)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    if commit:
        _commit_shared(ledger_repo, ["ledger.jsonl"], f"{row['action']} {row['task_id']}")
    return row_hash(row)


@dataclass
class MergeLock:
    """`queue/MERGE_LOCK.json`, held for the duration of one merge.

    Acquisition is a commit, so two runners racing for the same ledger collide
    as a git conflict on push and the loser backs off — that is the ledger's
    design, not something reimplemented here. Locally this also refuses a lock
    already held by a different task, which catches the far more common case of
    one operator running two merges at once.
    """

    ledger_repo: Path
    task_id: str
    _path: Path | None = None

    @property
    def path(self) -> Path:
        return self.ledger_repo / "queue" / "MERGE_LOCK.json"

    def acquire(self) -> None:
        # Shared: see the other writers' lock first; publishing ours is the race.
        pull_ledger(self.ledger_repo)
        if self.path.exists():
            try:
                held = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                held = {}
            holder = held.get("task_id")
            # A lock that says it was RELEASED is not held. dx's own release()
            # deletes the file, but the ledger predates dx and its tooling
            # marks state instead -- devswarm-ledger has carried
            # {"state": "released", "task_id": "T-0004"} since August. Reading
            # only task_id meant the FIRST successful merge locked the ledger
            # permanently: every later task saw a stale holder and refused, with
            # a message telling the operator not to delete a lock they did not
            # take. Correct advice, wrong situation.
            if str(held.get("state", "")).lower() == "released":
                holder = None
            if holder is not None and holder != self.task_id:
                raise LedgerWriteError(
                    f"MERGE_LOCK.json is held by {holder!r}. Another merge is in "
                    f"progress, or a previous one did not release. Resolve it "
                    f"deliberately — deleting a lock you did not take is how two "
                    f"merges end up interleaved."
                )
            if holder == self.task_id:
                return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "task_id": self.task_id,
                    "acquired_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _commit_shared(self.ledger_repo, ["queue/MERGE_LOCK.json"], f"LOCK {self.task_id}")
        self._path = self.path

    def release(self) -> None:
        pull_ledger(self.ledger_repo)
        if not self.path.exists():
            return
        self.path.unlink()
        _commit_shared(
            self.ledger_repo, ["queue/MERGE_LOCK.json"], f"UNLOCK {self.task_id}", add_all=True
        )

    def __enter__(self) -> MergeLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def git_merge_no_ff(repo: Path, sha: str, *, task_id: str) -> str:
    """`git merge --no-ff <sha>` in ``repo``. Returns the resulting HEAD.

    ``--no-ff`` is not a style preference: a fast-forward leaves no merge commit,
    and the ledger's ``MERGED`` row would then point at a commit that carries no
    record of having been merged under a gate.
    """
    if not (repo / ".git").exists():
        raise LedgerWriteError(f"{repo} is not a git repository")
    dirty = _git(repo, "status", "--porcelain").stdout.strip()
    if dirty:
        raise LedgerWriteError(
            f"refusing to merge into a dirty working tree at {repo}. "
            f"Uncommitted changes would be swept into the merge commit."
        )
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    merge = _git(
        repo,
        "-c",
        "user.name=dx",
        "-c",
        "user.email=dx@localhost",
        "merge",
        "--no-ff",
        sha,
        "-m",
        f"Merge {task_id} ({sha[:12]}) under dx merge gate",
        check=False,
    )
    if merge.returncode != 0:
        # A conflicting merge exits 1 with HEAD unchanged and the checkout
        # mid-merge (MERGE_HEAD, conflict markers). Through 0.22.0 the error
        # propagated with the tree left that way; the next `dx run` would
        # have found a dirty checkout and refused, and a person had to work
        # out why. Abort first, so a failed merge leaves the checkout exactly
        # as it was, then say what git said. `dx merge` tests the merge with
        # `merge-tree` before any row is written, so reaching here means the
        # tree changed in between or the merge failed for another reason.
        conflicts = _git(repo, "diff", "--name-only", "--diff-filter=U", check=False)
        abort = _git(repo, "merge", "--abort", check=False)
        left = "" if abort.returncode == 0 else (
            f" (and `git merge --abort` failed: {abort.stderr.strip()[:200]}; "
            f"the checkout is mid-merge)")
        raise LedgerWriteError(
            f"git merge --no-ff {sha[:12]} into {repo} failed (exit "
            f"{merge.returncode}): {(merge.stdout + merge.stderr).strip()[:300]}"
            f"{left}. No merge commit was made"
            + (f"; conflicting: {', '.join(conflicts.stdout.split())}"
               if conflicts.stdout.strip() else "") + "."
        )
    after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if after == before:
        # Exit zero and HEAD unchanged is the one shape left: `git merge` said
        # "Already up to date", the candidate was already
        # reachable from HEAD -- in the pilot, pxx commits it onto the branch
        # itself -- so no merge commit exists. Say so rather than record a
        # merge that did not happen (T-0060, 2026-09-23, the first bridge
        # task to reach this gate).
        raise AlreadyOnBranch(after)
    return after


class AlreadyOnBranch(Exception):
    """The candidate is already reachable from the branch head; ``git merge``
    created nothing. Carries the unchanged HEAD."""

    def __init__(self, head: str) -> None:
        super().__init__(head)
        self.head = head
