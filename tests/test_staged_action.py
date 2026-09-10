"""Staged-action runtime: mechanism derived by the verifier, and the four-binding
staleness gate.

Two failure modes are pinned here, both of which would otherwise pass green:

  spec 1 — a software key whose row *claims* the hardware standard must be
    refused. The mechanism a row carries is derived from the key's residency, not
    from a string the signer supplied — otherwise the field launders a weak
    approval through a strong label (the 0.15.0 unmarked-fallback lesson).

  spec 2 — the runner re-verifies all four bindings at execution time and refuses
    a moved world; the executor is *provably not called* when a binding moved. Act
    three's stale refusal must originate in this re-check, not in a scenario that
    knew the frame would move.
"""

from __future__ import annotations

import pytest

from dx.approval_key import MECHANISM_FALLBACK, MECHANISM_STANDARD, ApprovalKeyError
from dx.ledger_utils import SignerIdentity
from dx.staged_action import (
    StaleStageError,
    WorldState,
    build_approval_record,
    canonical_staged_message,
    execute_approved_stage,
    reverify_bindings,
)

_SIGNER = SignerIdentity(fingerprint="A" * 40, uid="Rex Reviewer <rex@example.invalid>")


def _world(**kw) -> WorldState:
    base = dict(ledger_head="h" * 64, frame_hash="f" * 64, payload_hash="p" * 64, bundle_hash="b" * 64)
    base.update(kw)
    return WorldState(**base)


def _record(residency: str, claimed: str | None = None) -> dict:
    return build_approval_record(
        world=_world(),
        stage_id="S-1",
        role="workflow-operator",
        signer=_SIGNER,
        residency=residency,
        claimed_mechanism=claimed,
    )


# --- spec 1: mechanism derived by the verifier ------------------------------


def test_a_card_key_derives_the_hardware_standard():
    assert _record("card")["mechanism"] == MECHANISM_STANDARD


def test_a_software_key_derives_the_marked_fallback():
    assert _record("software")["mechanism"] == MECHANISM_FALLBACK


def test_a_software_key_claiming_the_hardware_standard_is_refused():
    # The wrongly-passing test: a weak key laundering a strong label.
    with pytest.raises(ApprovalKeyError, match="laundering"):
        _record("software", claimed=MECHANISM_STANDARD)


def test_a_card_key_that_also_claims_the_standard_is_accepted():
    assert _record("card", claimed=MECHANISM_STANDARD)["mechanism"] == MECHANISM_STANDARD


def test_a_software_key_claiming_the_fallback_records_the_fallback():
    # A matching/weaker claim is ignored; the derived value is authoritative.
    assert _record("software", claimed=MECHANISM_FALLBACK)["mechanism"] == MECHANISM_FALLBACK


def test_the_signer_identity_is_carried_from_the_verified_signature():
    rec = _record("card")
    assert rec["signer_fingerprint"] == "A" * 40
    assert rec["signer_uid"] == "Rex Reviewer <rex@example.invalid>"
    assert rec["bindings"]["signed_head"] == "h" * 64


def test_a_keyless_residency_cannot_produce_an_approval():
    with pytest.raises(ApprovalKeyError, match="no signing secret"):
        _record("absent")


# --- the canonical message --------------------------------------------------


def test_canonical_message_binds_one_world_state_in_fixed_order():
    msg = canonical_staged_message(
        stage_id="S-1", bundle_hash="B", payload_hash="P", frame_hash="F", head="H", role="R"
    )
    assert msg.split("\n") == ["S-1", "B", "P", "F", "H", "R"]


# --- spec 2: the four-binding staleness gate --------------------------------


def test_reverify_passes_when_the_world_has_not_moved():
    reverify_bindings(_record("card"), _world())  # no raise


@pytest.mark.parametrize(
    "moved_field, label",
    [
        ("ledger_head", "ledger head"),
        ("frame_hash", "frame hash"),
        ("payload_hash", "payload hash"),
        ("bundle_hash", "bundle hash"),
    ],
)
def test_reverify_refuses_and_names_each_moved_binding(moved_field, label):
    approval = _record("card")
    observed = _world(**{moved_field: "z" * 64})
    with pytest.raises(StaleStageError, match=label):
        reverify_bindings(approval, observed)


def test_reverify_refuses_an_approval_with_no_bindings():
    with pytest.raises(StaleStageError, match="no bindings"):
        reverify_bindings({"mechanism": MECHANISM_STANDARD}, _world())


# --- spec 2: the runner's execute step --------------------------------------


def test_execute_runs_the_executor_when_the_world_is_current():
    approval = _record("card")
    ran = []
    result = execute_approved_stage(
        approval, observe=lambda: _world(), executor=lambda a: ran.append(a) or "done"
    )
    assert result == "done" and len(ran) == 1


def test_execute_refuses_a_moved_frame_and_never_calls_the_executor():
    # Act three, as the gate rather than the script: the frame moved between
    # approval and execution. The executor must be provably not called — the
    # refusal originates in the runner's re-verification.
    approval = _record("card")
    called = []

    def executor(_a):
        called.append(True)
        return "executed"

    def observe_now():
        return _world(frame_hash="moved" + "0" * 59)  # a different frame

    with pytest.raises(StaleStageError, match="frame hash"):
        execute_approved_stage(approval, observe=observe_now, executor=executor)
    assert called == [], "the executor ran against a moved frame — the staleness gate failed open"


def test_execute_reobserves_at_execution_time_not_approval_time():
    # observe() is called by the runner, so a world that moves before execution
    # is caught even though the approval was valid when signed.
    approval = _record("card")
    observations = [_world(), _world(ledger_head="advanced" + "0" * 56)]

    def observe_now():
        return observations.pop(0)

    # first execution: world current -> runs
    assert execute_approved_stage(approval, observe_now, lambda a: "ran") == "ran"
    # second execution: head advanced -> refused
    with pytest.raises(StaleStageError, match="ledger head"):
        execute_approved_stage(approval, observe_now, lambda a: "ran")
