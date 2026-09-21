"""Record that a run performed its task, bound to the commit that carries it.

WHY

``dx run`` wrote nothing to the ledger. ``dx admit`` writes ADMITTED and that
was the last row a task ever got — whether the run produced a hundred lines or
nothing at all. A reader could not tell those apart, which is the one question
a record of work most needs to answer.

On 2026-09-21 fourteen tasks sat ADMITTED in exactly that ambiguity and had to
be classified by hand afterwards, from memory and git archaeology. The executor
knows the answer at the moment it finishes; it should say so then.

WHAT AN EXECUTED ROW IS AND IS NOT

EXECUTED is already in SCHEMA.md's vocabulary. It says the work happened. It
says nothing about whether the work is any good: the task still needs a review,
and merging still needs a signature from someone who is not its author.

That is why this needs no key. The rule this estate runs on — **anything that
grants permission needs the key; anything that merely records needs none** —
puts recording on the safe side. The worst a compromised path could do here is
claim work exists, and the row carries the commit, so the claim is checkable
with ``git show`` rather than believed.

REFUSALS

Writing a second EXECUTED, or writing one for a task already SIGNED or MERGED,
would rewrite history rather than extend it. Both are refused; RL-009 says a
correction is an appended CORRECTION row naming the one it corrects.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path

from .ledger_state import LedgerStateError, current_state, task_rows
from .ledger_writer import append_row, read_head

#: Rows meaning this task is past the point where execution can be recorded.
#: Kept as a name for the tests that enumerate it; the decision itself is
#: made by ledger_state.current_state, which every other writer also asks.
CLOSED_ACTIONS = ("EXECUTED", "SIGNED", "MERGED", "ABANDONED")
#: A commit, not a ref name — see the rev-parse note below.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class ExecutedError(Exception):
    """Refused, with a reason a human can act on."""


def _git(scope: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(scope), *args],
        capture_output=True, text=True, timeout=60,
    )


def record_executed(
    ledger: Path, scope: Path, task_id: str, *, sha: str | None = None,
) -> str:
    """Append EXECUTED for ``task_id``. Returns the recorded commit.

    The author is taken from the task's own ADMITTED row rather than from the
    caller: the person accountable for a task is the one who opened it, and a
    runner that could name someone else is a runner that can forge provenance.
    """
    try:
        rows = task_rows(ledger, task_id)
    except LedgerStateError as exc:
        raise ExecutedError(str(exc)) from exc
    if not rows:
        raise ExecutedError(f"{task_id} has no rows — nothing to record against")

    state = current_state(rows, task_id)
    # One reading of "closed", shared with dx merge and the bridge: a task
    # that already has a candidate for its current admission, a signature, or
    # a terminal row is not re-executed. A re-admitted task (INCOMPLETE then
    # ADMITTED again) may be — the new admission has no candidate yet.
    closed = (
        "EXECUTED" if state.execution_recorded else
        "SIGNED" if state.signed else
        state.action if state.terminal else ""
    )
    if closed:
        raise ExecutedError(
            f"{task_id} already has {closed} — appending a second would "
            f"rewrite history. RL-009: append a CORRECTION naming that row.")

    author = next((r.get("author_human") for r in rows if r.get("author_human")), "")
    seat = next((r.get("author_seat") for r in rows if r.get("author_seat")), "")
    if not author or not seat:
        raise ExecutedError(f"{task_id} does not name an author and a seat")

    if sha:
        head = sha.strip()
    else:
        rev = _git(scope, "rev-parse", "HEAD")
        # returncode, not just stdout: `git rev-parse HEAD` on a repo with no
        # commits exits 128 AND prints the literal string "HEAD", so trusting
        # stdout alone writes sha="HEAD" into an append-only record.
        head = rev.stdout.strip() if rev.returncode == 0 else ""
    if not _SHA_RE.match(head):
        raise ExecutedError(
            f"{scope} has no HEAD to record" if not head
            else f"{head!r} is not a commit sha")
    stat = _git(scope, "show", "--stat", "--format=", head).stdout.strip()
    files = [ln.split("|")[0].strip() for ln in stat.splitlines() if "|" in ln]
    subject = _git(scope, "log", "-1", "--format=%s", head).stdout.strip()

    append_row(ledger, {
        "ts": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_id": task_id,
        "author_seat": seat,
        "author_human": author,
        "reviewer_seat": None,
        "action": "EXECUTED",
        "sha": head,
        "evidence": (f"mode=DX_RUN scope={scope.name} commit={head[:12]} "
                     f"files={len(files)} changed={','.join(files[:8])} "
                     f'subject="{subject[:90]}"'),
        "prev_hash": read_head(ledger / "ledger.jsonl"),
    })

    qf = ledger / "queue" / f"{task_id}.json"
    if qf.is_file():
        try:
            q = json.loads(qf.read_text())
            q["state"] = "EXECUTED"
            # `sha` — the one field name. `dx merge` reads it; until
            # 2026-09-21 this wrote `executed_sha` and merge read `sha`, so
            # every bridge task reached the gate with no candidate.
            q["sha"] = head
            q.pop("executed_sha", None)
            qf.write_text(json.dumps(q, indent=2, sort_keys=True) + "\n")
        except (OSError, ValueError) as exc:
            # The ledger is the record; the queue is a convenience view of it.
            # A queue that could not be updated is worth saying out loud and is
            # not worth discarding a written ledger row over.
            raise ExecutedError(
                f"EXECUTED was recorded, but {qf.name} could not be updated: "
                f"{exc}") from exc
    return head[:12]


__all__ = ["CLOSED_ACTIONS", "ExecutedError", "record_executed"]
