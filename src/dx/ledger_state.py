"""One reading of a task's ledger rows, shared by everything that acts on it.

WHY ONE MODULE

On 2026-09-21 four programs each carried their own idea of what "closed"
meant. The bridge's review list withheld a redlined task; the approval
service still offered it for signature; the execution recorder refused a
task with any EXECUTED row; ``dx merge`` checked no state at all and would
have appended SIGNED over a redline. Four vocabularies, and the one that
could actually sign used the weakest.

A task's state is a property of its rows, so it is computed here, once, from
the rows, and every caller asks the same function. A caller that wants a
different answer changes this file and its tests, where the disagreement is
visible.

THE READING

Rows are read in order. Some actions *bear status* — the latest of them is
the task's state. Others *merely record* and change nothing:

  status-bearing   ADMITTED EXECUTED REVIEWED REDLINE INCOMPLETE ESCALATED
                   SIGNED MERGED ABANDONED
  record-only      GENESIS EVIDENCE CORRECTION

Two consequences of "latest status wins" are deliberate and worth stating:

  * ADMITTED after REDLINE or INCOMPLETE *re-opens* the task (that is what a
    rework's re-admission means), and the candidate resets — an EXECUTED row
    only counts if it comes after the latest ADMITTED. T-0002 and T-0007 on
    the real ledger have this shape.
  * REVIEWED after REDLINE clears the redline. Only a non-author can record
    REVIEWED, so this is the reviewer withdrawing an objection, and it is
    recorded as such rather than hidden.

CORRECTION (RL-009) names the row it corrects and is otherwise neutral here:
it says the record was wrong, not what the task's state now is. A correction
that changes state is followed by the row that states it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

STATUS_ACTIONS = frozenset({
    "ADMITTED", "EXECUTED", "REVIEWED", "REDLINE", "INCOMPLETE", "ESCALATED",
    "SIGNED", "MERGED", "ABANDONED",
})
RECORD_ONLY_ACTIONS = frozenset({"GENESIS", "EVIDENCE", "CORRECTION"})
#: A human has objected, or a human is needed. Nothing merges past these
#: without a row that says the objection was answered.
WITHHOLDING_ACTIONS = frozenset({"REDLINE", "INCOMPLETE", "ESCALATED"})
TERMINAL_ACTIONS = frozenset({"MERGED", "ABANDONED"})

#: The public reference ledger ships synthetic rows. Gating a real merge
#: against it attests to nothing, and dx falls back to it when nothing is
#: configured — the dangerous resting point its own docstring warns about.
REFERENCE_LEDGER_NAME = "devswarm-ledger-reference"


class LedgerStateError(RuntimeError):
    """The rows do not support the action asked of them; the message says why."""


@dataclass(frozen=True)
class TaskState:
    task_id: str
    #: Latest status-bearing action, or "" for a task with none.
    action: str
    by: str
    reason: str
    #: The commit on the EXECUTED row that follows the latest ADMITTED, if any.
    candidate_sha: str | None
    #: An EXECUTED row follows the latest ADMITTED — with or without a commit.
    #: Distinct from ``executed``: a row with no sha is no candidate to sign,
    #: but it is still a row, and a second one would rewrite history.
    execution_recorded: bool
    signed: bool
    corrections: int
    rows: int

    @property
    def terminal(self) -> bool:
        return self.action in TERMINAL_ACTIONS

    @property
    def is_open(self) -> bool:
        return bool(self.action) and not self.terminal

    @property
    def withheld(self) -> bool:
        return self.action in WITHHOLDING_ACTIONS

    @property
    def executed(self) -> bool:
        return self.candidate_sha is not None

    @property
    def is_signable(self) -> bool:
        return why_not_signable(self) is None

    @property
    def supersedable(self) -> bool:
        """May a rework name this task as the one it supersedes?

        Anything still open and not yet signed. Signed work is corrected, not
        superseded: a signature is a statement someone made and it stays in
        the record with its answer.
        """
        return self.is_open and not self.signed


def task_rows(ledger_repo: Path, task_id: str) -> list[dict]:
    """Every row for ``task_id``, in ledger order. Corrupt rows stop the line."""
    path = ledger_repo / "ledger.jsonl"
    if not path.is_file():
        raise LedgerStateError(f"no ledger at {path}")
    out: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerStateError(
                f"{path}:{lineno} is not valid JSON: {exc}. Stop the line "
                f"(RL-009) and repair the ledger before acting on any task."
            ) from exc
        if isinstance(row, dict) and row.get("task_id") == task_id:
            out.append(row)
    return out


def current_state(rows: list[dict], task_id: str = "") -> TaskState:
    """Reduce ``rows`` (one task's, in order) to its state."""
    action = by = reason = ""
    candidate: str | None = None
    recorded = False
    signed = False
    corrections = 0
    seen = 0
    for row in rows:
        seen += 1
        a = row.get("action") or ""
        if a == "CORRECTION":
            corrections += 1
        if a not in STATUS_ACTIONS:
            continue
        action = a
        by = row.get("author_human") or ""
        reason = row.get("evidence") or ""
        if a == "ADMITTED":
            # A (re-)admission opens fresh: whatever ran before it was for
            # the previous admission, and a signature before it was over
            # work that has since been sent back.
            candidate = None
            recorded = False
            signed = False
        elif a == "EXECUTED":
            sha = (row.get("sha") or "").strip()
            candidate = sha or None
            recorded = True
        elif a == "SIGNED":
            signed = True
    return TaskState(
        task_id=task_id or (rows[0].get("task_id", "") if rows else ""),
        action=action, by=by, reason=reason, candidate_sha=candidate,
        execution_recorded=recorded, signed=signed, corrections=corrections,
        rows=seen,
    )


def why_not_signable(state: TaskState) -> str | None:
    """None if a second human may sign this task now; otherwise the reason."""
    if not state.rows:
        return "no rows — nothing to approve"
    if not state.action:
        return "no status-bearing row — the task was never admitted"
    if state.terminal:
        return f"already {state.action}"
    if state.withheld:
        who = f" by {state.by}" if state.by else ""
        return (f"{state.action}{who} — the objection has not been answered "
                f"(a rework re-admits; a reviewer records REVIEWED)")
    if state.signed:
        return "already SIGNED — a second signature would rewrite history"
    if not state.executed:
        return ("no EXECUTED row with a commit after the latest ADMITTED — "
                "there is no candidate to approve")
    return None


def candidate_for_merge(queue: dict, state: TaskState) -> str:
    """The one commit a merge may act on.

    The ledger's EXECUTED row is authoritative; the queue file is a convenience
    view and must agree with it. Before 2026-09-21 ``dx merge`` read the queue
    alone, under a field name nothing on the bridge path ever wrote, and read
    it only after the SIGNED row was already appended.
    """
    reason = why_not_signable(state)
    if reason:
        raise LedgerStateError(reason)
    assert state.candidate_sha  # why_not_signable guarantees it
    q = str(queue.get("sha") or "").strip()
    if not q:
        if queue.get("executed_sha"):
            raise LedgerStateError(
                "queue file names the candidate as 'executed_sha'; the field "
                "is 'sha' (one name, settled 2026-09-21). Rename it and retry.")
        raise LedgerStateError(
            "queue file has no 'sha' — it does not name a candidate")
    if q != state.candidate_sha:
        raise LedgerStateError(
            f"queue file says candidate {q[:12]} but the EXECUTED row says "
            f"{state.candidate_sha[:12]} — the ledger is the record; "
            f"establish which is wrong before merging")
    return state.candidate_sha


def is_reference_ledger(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser()
    return resolved.name == REFERENCE_LEDGER_NAME


__all__ = [
    "LedgerStateError", "RECORD_ONLY_ACTIONS", "REFERENCE_LEDGER_NAME",
    "STATUS_ACTIONS", "TERMINAL_ACTIONS", "TaskState", "WITHHOLDING_ACTIONS",
    "candidate_for_merge", "current_state", "is_reference_ledger",
    "task_rows", "why_not_signable",
]
