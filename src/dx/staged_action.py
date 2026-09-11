"""Runtime for staged actions: derive the approval mechanism, and re-verify the
four world-state bindings at execution time.

Two invariants live here, both of them things a green checkmark could otherwise
launder past:

1. **The mechanism is derived by the verifier, never asserted by the signer.**
   :func:`build_approval_record` sets ``approval.mechanism`` from the signing
   key's residency (:mod:`dx.approval_key`), not from any string the signing side
   supplied. A software key whose row *claims* the hardware standard is refused —
   otherwise the field is decoration and a weak approval can wear a strong label.

2. **The staleness gate is the runner's re-verification, not the scenario.**
   :func:`execute_approved_stage` re-observes the world *now* and re-checks all
   four bindings (ledger head, frame hash, payload hash, bundle hash) against the
   approved row before the executor is ever called. If any moved, it refuses and
   the executor does not run. The demo's act-three stale refusal is this function
   doing its job against a frame that moved — the refusal originates here, so it
   proves the gate, not the script.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from dx.approval_key import resolve_mechanism
from dx.ledger_utils import SignerIdentity

T = TypeVar("T")


class StaleStageError(Exception):
    """A staged action's world-state moved since it was approved. Names every
    binding that moved so the refusal is legible, never a bare "stale"."""


class MalformedApprovalError(Exception):
    """An approval record is structurally invalid — it carries no bindings, or a
    binding is missing — so it cannot be re-verified at all. Distinct from
    :class:`StaleStageError`: the world did not move, the record is broken. An
    examiner reading the receipt should see "malformed", not "stale".
    """


def canonical_staged_message(
    *, stage_id: str, bundle_hash: str, payload_hash: str, frame_hash: str, head: str, role: str
) -> str:
    """The exact string a staged-action approval signs. Binds one world-state:
    the stage, the whole bundle, the payload, the perceived frame, the ledger
    head, and the seat — order fixed so signer and verifier agree byte for byte."""
    return f"{stage_id}\n{bundle_hash}\n{payload_hash}\n{frame_hash}\n{head}\n{role}"


@dataclass(frozen=True)
class WorldState:
    """The four bindings that make an approval specific to one moment. Compared
    whole at approval time and again at execution time."""

    ledger_head: str
    frame_hash: str
    payload_hash: str
    bundle_hash: str


def build_approval_record(
    *,
    world: WorldState,
    stage_id: str,
    role: str,
    signer: SignerIdentity,
    residency: str,
    claimed_mechanism: str | None = None,
) -> dict[str, object]:
    """Assemble the ``approval`` block for an APPROVED staged bundle.

    The mechanism is *derived* from ``residency`` via
    :func:`dx.approval_key.resolve_mechanism`; a ``claimed_mechanism`` that
    over-states the key (a software key claiming the hardware standard) is refused
    there. The signer identity comes from a verified detached signature, never
    from self-report.
    """
    mechanism = resolve_mechanism(residency, claimed=claimed_mechanism)
    return {
        "approval_class": "human_attested",
        "signer_fingerprint": signer.fingerprint,
        "signer_uid": signer.uid,
        "mechanism": mechanism,
        "bindings": {
            "stage_id": stage_id,
            "role": role,
            "signed_head": world.ledger_head,
            "frame_hash": world.frame_hash,
            "payload_hash": world.payload_hash,
            "bundle_hash": world.bundle_hash,
        },
    }


def _approved_world(approval: dict[str, object]) -> WorldState:
    b = approval.get("bindings")
    if not isinstance(b, dict):
        raise MalformedApprovalError("approval carries no bindings; it cannot be re-verified")
    try:
        return WorldState(
            ledger_head=str(b["signed_head"]),
            frame_hash=str(b["frame_hash"]),
            payload_hash=str(b["payload_hash"]),
            bundle_hash=str(b["bundle_hash"]),
        )
    except KeyError as exc:
        raise MalformedApprovalError(f"approval bindings missing {exc.args[0]!r}") from exc


def reverify_bindings(approval: dict[str, object], observed: WorldState) -> None:
    """Refuse unless every binding the approval signed still matches the observed
    world. Raises :class:`StaleStageError` naming **every** binding that moved —
    not just the first — so one refusal is the whole story and an operator is not
    made to fix them one re-run at a time. This is the check the runner runs at
    execute time: the staleness gate. (An approval too malformed to re-verify
    raises :class:`MalformedApprovalError` instead — a different failure.)"""
    approved = _approved_world(approval)
    moved = [
        f"{name} ({was[:12]}… → {now[:12]}…)"
        for name, was, now in (
            ("ledger head", approved.ledger_head, observed.ledger_head),
            ("frame hash", approved.frame_hash, observed.frame_hash),
            ("payload hash", approved.payload_hash, observed.payload_hash),
            ("bundle hash", approved.bundle_hash, observed.bundle_hash),
        )
        if was != now
    ]
    if moved:
        raise StaleStageError(
            f"{len(moved)} binding(s) moved since approval: {'; '.join(moved)}. "
            "Re-review required (RL-003). The stage is not executed."
        )


def execute_approved_stage(
    approval: dict[str, object],
    observe: Callable[[], WorldState],
    executor: Callable[[dict[str, object]], T],
) -> T:
    """Execute an approved stage — but only after re-verifying it is still bound
    to the current world.

    ``observe`` reads the world *now* (current head, a fresh frame hash, the
    payload and bundle hashes); the runner calls it at execution time, not at
    approval time. If :func:`reverify_bindings` refuses, ``executor`` is never
    called — so a frame that moved between approval and execution stops the act,
    and the refusal comes from this re-check rather than from a scenario that knew
    the frame would move.
    """
    observed = observe()
    reverify_bindings(approval, observed)
    return executor(approval)
