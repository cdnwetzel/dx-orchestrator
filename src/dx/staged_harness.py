"""The three-act staged-action harness — the governance loop, run end to end.

Three acts, each a real path through the staged-action lifecycle, each producing
ledger rows on the existing action enum (no charter change — the staging
lifecycle rides EVIDENCE → REVIEWED → SIGNED → EXECUTED):

  Act 1 — the full loop: a stage is filed, its field-level preview reviewed, its
    approval signed (mechanism derived from the key, never asserted), re-verified
    against the current world, and executed. Rows: EVIDENCE, REVIEWED, SIGNED,
    EXECUTED.
  Act 2 — the receipted rejection: a reviewed stage is rejected, and the
    re-proposal gate refuses the *same* stage without changed evidence. Rows:
    EVIDENCE, REVIEWED, ABANDONED.
  Act 3 — the stale refusal: a signed stage whose world moved before execution is
    refused by the runner's re-verification; the executor is never called, and the
    stage is recorded INCOMPLETE (re-review required). Rows: EVIDENCE, REVIEWED,
    SIGNED, INCOMPLETE.

The acts orchestrate the seam (:mod:`dx.staged_action`) over *injected*
dependencies — an ``append`` that writes a ledger row, an ``observe`` that reads
the world at execution time, and an ``executor`` that replays the staged
sequence. That injection is what lets the whole loop be exercised in CI against a
collector and synthetic world-states, then bound to the real ledger, live AT-SPI
capture, and Xvfb for the on-hardware run — the same code either way, so what CI
proves is what the demo runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from dx.ledger_utils import SignerIdentity
from dx.staged_action import (
    ResidencyResolver,
    StaleStageError,
    WorldState,
    build_approval_record,
    execute_approved_stage,
)

#: Append one ledger row: ``append(action, evidence)``. The real binding wraps
#: ``dx.ledger_writer.append_row`` against a ledger repo; tests pass a collector.
LedgerAppend = Callable[[str, str], None]

#: Read the world *now* (current head, fresh frame hash, payload/bundle hashes).
Observe = Callable[[], WorldState]

#: Replay the approved staged sequence. Receives the approval record.
Executor = Callable[[dict[str, object]], object]


class HarnessError(Exception):
    """A harness act could not run as specified (not a governance refusal —
    those are recorded as receipts, never raised)."""


@dataclass
class ActReceipt:
    """What one act produced: its outcome and the ledger rows it appended."""

    act: str
    outcome: str  # "EXECUTED" | "REJECTED" | "STALE"
    ledger_actions: list[str] = field(default_factory=list)
    detail: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "act": self.act,
            "outcome": self.outcome,
            "ledger_actions": list(self.ledger_actions),
            "detail": self.detail,
        }


def _stage_and_review(stage_id: str, append: LedgerAppend, actions: list[str]) -> None:
    """The two rows every act begins with: the stage is filed as evidence, then
    its field-level preview is reviewed."""
    append("EVIDENCE", f"dx.staged_action.v1 staged {stage_id}")
    actions.append("EVIDENCE")
    append("REVIEWED", f"field-level preview reviewed for {stage_id}")
    actions.append("REVIEWED")


def run_full_loop(
    *,
    stage_id: str,
    role: str,
    world: WorldState,
    signer: SignerIdentity,
    resolve_residency: ResidencyResolver,
    observe_now: Observe,
    executor: Executor,
    append: LedgerAppend,
) -> ActReceipt:
    """Act 1. Stage → review → sign → re-verify → execute, fully receipted.

    The mechanism is derived from ``residency`` (never asserted). ``observe_now``
    reads the world *at execution time* — the happy path is that nothing moved, so
    it matches the signed world and execution proceeds. But the re-verification is
    real, not vacuous: if a binding moved between signing and execution,
    :func:`execute_approved_stage` raises :class:`StaleStageError`, the executor is
    never called, and no EXECUTED row is written. Act 1 is not exempt from the gate.
    """
    actions: list[str] = []
    _stage_and_review(stage_id, append, actions)

    approval = build_approval_record(
        world=world, stage_id=stage_id, role=role, signer=signer,
        resolve_residency=resolve_residency,
    )
    append("SIGNED", f"approval mechanism={approval['mechanism']} signer={signer.fingerprint[:12]}")
    actions.append("SIGNED")

    # Re-observe and re-verify against the current world before executing. A move
    # here raises StaleStageError (executor never called) — the happy path passes
    # only because the world is genuinely unchanged.
    execute_approved_stage(approval, observe=observe_now, executor=executor)
    append("EXECUTED", f"replayed staged sequence for {stage_id}")
    actions.append("EXECUTED")
    return ActReceipt(
        act="full-loop",
        outcome="EXECUTED",
        ledger_actions=actions,
        detail=f"mechanism={approval['mechanism']}",
    )


def refuse_reproposal_without_changed_evidence(
    *, prior_bundle_hash: str, new_bundle_hash: str
) -> None:
    """The re-proposal gate: a stage rejected once may not be re-filed unchanged.

    "Changed evidence" is a different bundle hash — the bundle commits to the whole
    staged sequence, so an identical hash is an identical proposal. Raises
    :class:`HarnessError` if the re-proposal is the same stage under a new name.
    """
    if new_bundle_hash == prior_bundle_hash:
        raise HarnessError(
            "re-proposal refused: the bundle hash is unchanged, so the evidence "
            "is unchanged; a rejected stage may only return with changed evidence"
        )


def run_rejection(
    *,
    stage_id: str,
    reason: str,
    append: LedgerAppend,
) -> ActReceipt:
    """Act 2. A reviewed stage is rejected and abandoned; the verdict is receipted.

    The re-proposal gate (:func:`refuse_reproposal_without_changed_evidence`) is
    what stops the rejected stage from simply firing again; the demo exercises it
    against this stage's bundle hash.
    """
    actions: list[str] = []
    append("EVIDENCE", f"dx.staged_action.v1 staged {stage_id}")
    actions.append("EVIDENCE")
    append("REVIEWED", f"rejected: {reason}")
    actions.append("REVIEWED")
    append("ABANDONED", f"stage {stage_id} rejected; re-proposal requires changed evidence")
    actions.append("ABANDONED")
    return ActReceipt(
        act="rejection", outcome="REJECTED", ledger_actions=actions, detail=reason
    )


def run_stale_refusal(
    *,
    stage_id: str,
    role: str,
    world: WorldState,
    signer: SignerIdentity,
    resolve_residency: ResidencyResolver,
    observe_now: Observe,
    executor: Executor,
    append: LedgerAppend,
) -> ActReceipt:
    """Act 3. A signed stage whose world moved before execution is refused.

    ``observe_now`` reads the world at execution time; if it differs from the
    signed world, :func:`execute_approved_stage`'s re-verification raises and the
    executor is never reached. The refusal originates in the runner — this function
    does not know in advance that the frame moved; it only records what the
    re-check decided. ``executor`` is accepted for signature symmetry but is never
    delegated to: Act 3 is a refusal, so it must never perform a real action.
    """
    actions: list[str] = []
    _stage_and_review(stage_id, append, actions)

    approval = build_approval_record(
        world=world, stage_id=stage_id, role=role, signer=signer,
        resolve_residency=resolve_residency,
    )
    append("SIGNED", f"approval mechanism={approval['mechanism']} signer={signer.fingerprint[:12]}")
    actions.append("SIGNED")

    reached_executor = False

    def _sentinel_executor(a: dict[str, object]) -> object:
        # Act 3 must never execute — the frame is meant to have moved, so the gate
        # should refuse before this runs. Record that it was reached, but do NOT
        # delegate to the real executor: a misconfigured drill (world unchanged)
        # must not perform the action or leave a side effect a retry could double.
        nonlocal reached_executor
        reached_executor = True
        return None

    try:
        execute_approved_stage(approval, observe=observe_now, executor=_sentinel_executor)
    except StaleStageError as exc:
        # The intended path: the world moved, the runner refused, the real executor
        # was never reached (reached_executor is False here).
        append("INCOMPLETE", f"stale at execution: {exc}")
        actions.append("INCOMPLETE")
        return ActReceipt(
            act="stale-refusal", outcome="STALE", ledger_actions=actions, detail=str(exc)
        )

    # Re-verification unexpectedly passed — the world did not move, so this act
    # cannot demonstrate the staleness gate. That is a harness setup error, not a
    # governance outcome: the sentinel performed no real action, and no EXECUTED row
    # is written. Fail loud.
    assert reached_executor  # by construction: no StaleStageError means the gate passed
    raise HarnessError(
        "act 3 expected a moved world but observe_now returned the signed world; "
        "the staleness gate was not exercised (no action taken, no EXECUTED row)"
    )
