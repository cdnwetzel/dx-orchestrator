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
        raise LedgerWriteError(
            f"git {' '.join(args)} failed in {repo} "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    return result


def append_row(ledger_repo: Path, row: dict[str, Any], *, commit: bool = True) -> str:
    """Append ``row`` and return the new head hash.

    The row must already carry the correct ``prev_hash``; this refuses to write
    one that would break the chain rather than appending it and leaving the
    repository in a state whose verifier fails.
    """
    ledger = ledger_repo / "ledger.jsonl"
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
        _git(ledger_repo, "add", "ledger.jsonl")
        _git(
            ledger_repo,
            "-c",
            "user.name=dx",
            "-c",
            "user.email=dx@localhost",
            "commit",
            "-m",
            f"{row['action']} {row['task_id']}",
        )
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
        if self.path.exists():
            try:
                held = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                held = {}
            holder = held.get("task_id")
            if holder != self.task_id:
                raise LedgerWriteError(
                    f"MERGE_LOCK.json is held by {holder!r}. Another merge is in "
                    f"progress, or a previous one did not release. Resolve it "
                    f"deliberately — deleting a lock you did not take is how two "
                    f"merges end up interleaved."
                )
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
        _git(self.ledger_repo, "add", "queue/MERGE_LOCK.json")
        _git(
            self.ledger_repo,
            "-c",
            "user.name=dx",
            "-c",
            "user.email=dx@localhost",
            "commit",
            "-m",
            f"LOCK {self.task_id}",
        )
        self._path = self.path

    def release(self) -> None:
        if not self.path.exists():
            return
        self.path.unlink()
        _git(self.ledger_repo, "add", "-A", "queue/MERGE_LOCK.json")
        _git(
            self.ledger_repo,
            "-c",
            "user.name=dx",
            "-c",
            "user.email=dx@localhost",
            "commit",
            "-m",
            f"UNLOCK {self.task_id}",
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
    _git(
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
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()
