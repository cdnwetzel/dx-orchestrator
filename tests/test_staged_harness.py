"""The three-act staged-action harness — the governance loop, receipted.

Each act is exercised against a ledger-row collector and synthetic world-states,
so the whole loop runs in CI without Xvfb or a live desktop. What is pinned:

  Act 1 — the full loop appends EVIDENCE → REVIEWED → SIGNED → EXECUTED, the
    executor runs, and the mechanism is derived from the key (not asserted).
  Act 2 — a rejection appends EVIDENCE → REVIEWED → ABANDONED, and the re-proposal
    gate refuses the same stage unchanged / admits it with changed evidence.
  Act 3 — a stale refusal appends EVIDENCE → REVIEWED → SIGNED → INCOMPLETE, and
    the executor is *never called* — the refusal comes from the runner's
    re-verification, not from the scenario.
"""

from __future__ import annotations

import pytest

from dx.approval_key import MECHANISM_FALLBACK, MECHANISM_STANDARD
from dx.ledger_utils import SignerIdentity
from dx.staged_action import WorldState
from dx.staged_harness import (
    HarnessError,
    refuse_reproposal_without_changed_evidence,
    run_full_loop,
    run_rejection,
    run_stale_refusal,
)

_SIGNER = SignerIdentity(fingerprint="A" * 40, uid="Rex Reviewer <rex@example.invalid>")


def _world(**kw) -> WorldState:
    base = dict(ledger_head="h" * 64, frame_hash="f" * 64, payload_hash="p" * 64, bundle_hash="b" * 64)
    base.update(kw)
    return WorldState(**base)


class _Ledger:
    """A collector standing in for the real ledger append."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def append(self, action: str, evidence: str) -> None:
        self.rows.append((action, evidence))

    @property
    def actions(self) -> list[str]:
        return [a for a, _ in self.rows]


# --- Act 1: the full loop ---------------------------------------------------


def test_full_loop_appends_the_lifecycle_and_executes():
    led = _Ledger()
    ran: list[dict] = []
    receipt = run_full_loop(
        stage_id="S-1",
        role="workflow-operator",
        world=_world(),
        signer=_SIGNER,
        residency="software",
        executor=lambda a: ran.append(a) or "replayed",
        append=led.append,
    )
    assert led.actions == ["EVIDENCE", "REVIEWED", "SIGNED", "EXECUTED"]
    assert receipt.outcome == "EXECUTED"
    assert len(ran) == 1  # the executor replayed exactly once


def test_full_loop_mechanism_is_derived_from_the_key():
    led = _Ledger()
    soft = run_full_loop(
        stage_id="S-1", role="r", world=_world(), signer=_SIGNER, residency="software",
        executor=lambda a: None, append=led.append,
    )
    assert MECHANISM_FALLBACK in soft.detail  # software key -> marked fallback

    led2 = _Ledger()
    card = run_full_loop(
        stage_id="S-2", role="r", world=_world(), signer=_SIGNER, residency="card",
        executor=lambda a: None, append=led2.append,
    )
    assert MECHANISM_STANDARD in card.detail  # card key -> the RL-010 standard
    # and the SIGNED row records the mechanism honestly
    signed = [ev for act, ev in led2.rows if act == "SIGNED"][0]
    assert MECHANISM_STANDARD in signed


# --- Act 2: the receipted rejection + re-proposal gate ----------------------


def test_rejection_is_receipted_and_abandoned():
    led = _Ledger()
    receipt = run_rejection(stage_id="S-9", reason="total does not match the invoice", append=led.append)
    assert led.actions == ["EVIDENCE", "REVIEWED", "ABANDONED"]
    assert receipt.outcome == "REJECTED"
    assert "total does not match" in [ev for act, ev in led.rows if act == "REVIEWED"][0]


def test_reproposal_of_the_same_bundle_is_refused():
    with pytest.raises(HarnessError, match="unchanged"):
        refuse_reproposal_without_changed_evidence(prior_bundle_hash="b" * 64, new_bundle_hash="b" * 64)


def test_reproposal_with_changed_evidence_is_allowed():
    # A different bundle hash is different evidence — no raise.
    refuse_reproposal_without_changed_evidence(prior_bundle_hash="b" * 64, new_bundle_hash="c" * 64)


# --- Act 3: the stale refusal ----------------------------------------------


def test_stale_refusal_never_calls_the_executor():
    led = _Ledger()
    called: list[bool] = []

    def executor(_a):
        called.append(True)
        return "executed"

    # observe_now returns a world whose frame moved since signing.
    receipt = run_stale_refusal(
        stage_id="S-3",
        role="workflow-operator",
        world=_world(),
        signer=_SIGNER,
        residency="software",
        observe_now=lambda: _world(frame_hash="moved" + "0" * 59),
        executor=executor,
        append=led.append,
    )
    assert led.actions == ["EVIDENCE", "REVIEWED", "SIGNED", "INCOMPLETE"]
    assert receipt.outcome == "STALE"
    assert called == [], "the executor ran against a moved world — the staleness gate failed open"
    assert "frame hash" in receipt.detail  # the refusal names the binding that moved


@pytest.mark.parametrize("moved_field", ["ledger_head", "payload_hash", "bundle_hash"])
def test_stale_refusal_catches_every_moved_binding(moved_field):
    led = _Ledger()
    called: list[bool] = []
    receipt = run_stale_refusal(
        stage_id="S-4", role="r", world=_world(), signer=_SIGNER, residency="software",
        observe_now=lambda: _world(**{moved_field: "z" * 64}),
        executor=lambda a: called.append(True),
        append=led.append,
    )
    assert receipt.outcome == "STALE" and called == []
    assert led.actions[-1] == "INCOMPLETE"


def test_act3_setup_error_if_the_world_did_not_move():
    # If observe_now returns the signed world, the staleness gate is not exercised
    # — that is a harness setup error, surfaced loudly, never a silent EXECUTED.
    led = _Ledger()
    with pytest.raises(HarnessError, match="staleness gate was not exercised"):
        run_stale_refusal(
            stage_id="S-5", role="r", world=_world(), signer=_SIGNER, residency="software",
            observe_now=lambda: _world(),  # unchanged
            executor=lambda a: "ran",
            append=led.append,
        )
