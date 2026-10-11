"""Coordinated, recoverable forwarding of recommendations into pxx's inbox.

Ordering, all under the store's ``mutation_lock`` (the same lock every
decision takes, so a reject cannot land between the check and the write):

1. Re-read the recommendation. Only a PROPOSED, forwardable one is eligible.
2. Commit a ForwardRecord in state INTENT together with a revision check on
   the recommendation itself — if anything changed it since it was read, the
   commit fails and nothing reaches pxx.
3. Create the pxx entry (``learn_bridge.write_pending``: cycle.lock, ``"x"``).
4. Commit the outcome (WRITTEN or NOT_WRITTEN, with what pxx's inbox showed).

A crash between 2 and 4 leaves INTENT. ``reconcile_forwards`` — run at the
start of every ``dx learn run`` and after every reject/retire — resolves it
from what pxx's inbox actually holds: our entry present → WRITTEN, absent →
INTERRUPTED (forwarded again next pass if still PROPOSED). The pxx filename is
deterministic and created with ``"x"``, so a retry can never duplicate it.

pxx's ``improve triage`` takes no lock, so dx and a pxx human can decide the
same proposal differently. dx never resolves that itself: reconcile records a
``divergence`` on the ForwardRecord, it is audited, and ``queue``/``run``
print it until a human acts in one tool or the other.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .learn_bridge import (
    FORWARDABLE_KINDS,
    CycleLockBusy,
    ForwardError,
    build_proposal,
    inspect,
    withdraw_if_ours,
    write_pending,
)
from .learn_model import ForwardRecord, Recommendation
from .learn_store import StaleDecision, Store, Tx

ACTOR = "dx learn"
_RETRY = "retry: "          # NOT_WRITTEN notes with this prefix are forwarded again later
_DECIDED_VIEWS = frozenset({"qualified", "rejected"})


def _eligible(rec: Recommendation, fwd: ForwardRecord | None) -> bool:
    if rec.status != "PROPOSED" or rec.kind not in FORWARDABLE_KINDS:
        return False
    if fwd is None:
        return True
    return fwd.state == "INTERRUPTED" or (fwd.state == "NOT_WRITTEN" and fwd.note.startswith(_RETRY))


def _outcome_for(view: str) -> tuple[str, str]:
    if view == "pending-ours":
        return "WRITTEN", "entry created in pxx's human-review inbox"
    if view == "pending-other":
        return "NOT_WRITTEN", _RETRY + "another pending proposal holds this pxx signature"
    if view in _DECIDED_VIEWS:   # ours, and a pxx human decided it in the same moment
        return "WRITTEN", f"entry created; a pxx human {view} it at once"
    if view.startswith(("qualified", "rejected")):
        return "NOT_WRITTEN", f"a pxx human already decided this signature ({view})"
    return "NOT_WRITTEN", _RETRY + f"entry not present after the write ({view})"


def divergence(dx_status: str, view: str | None) -> str | None:
    """dx and pxx humans decided the same proposal differently. Recorded, never resolved."""
    if view == "qualified" and dx_status in ("REJECTED", "RETIRED"):
        return f"dx {dx_status} it, but a pxx human qualified it"
    if view == "rejected" and dx_status == "ACCEPTED":
        return "dx ACCEPTED it, but a pxx human rejected it"
    return None


def forward_proposals(store: Store, state_dir: Path, now: datetime) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with store.mutation_lock():
        recs, fwds = store.load_recs(), store.load_forwards()
        for key in sorted(recs):
            rec, fwd = recs[key], fwds.get(key)
            if not _eligible(rec, fwd):
                continue
            try:
                slug, payload, fingerprint = build_proposal(rec)
            except ForwardError as exc:
                results.append({"rec_key": key, "state": "SKIPPED", "note": str(exc)})
                continue
            intent = ForwardRecord(
                rec_key=key, slug=slug, payload_sha256=fingerprint, state="INTENT",
                rec_revision=rec.revision + 1, updated_at=now, note="about to write",
                revision=fwd.revision if fwd else 0)
            try:
                # rec rides along purely as a revision check: a decision since our
                # read makes this commit fail before anything reaches pxx.
                store.commit(Tx(ACTOR, "forward-intent", key, now, writes=[intent, rec],
                                detail={"slug": slug, "fingerprint": fingerprint}))
            except StaleDecision:
                results.append({"rec_key": key, "state": "SKIPPED",
                                "note": "decided while this pass ran; not forwarded"})
                continue
            try:
                view = write_pending(state_dir, slug, payload, fingerprint)
                state, note = _outcome_for(view)
            except CycleLockBusy:
                view, state, note = None, "NOT_WRITTEN", _RETRY + "pxx cycle.lock was busy"
            done = replace(intent, state=state, pxx_view=view, note=note, updated_at=now)
            store.commit(Tx(ACTOR, "forward-outcome", key, now, writes=[done],
                            detail={"slug": slug, "state": state, "pxx_view": view}))
            results.append({"rec_key": key, "slug": slug, "state": state, "note": note})
    return results


def reconcile_forwards(store: Store, state_dir: Path, now: datetime, *,
                       persist: bool = True, only: str | None = None) -> list[dict[str, Any]]:
    """Compare every forward record with pxx's inbox and dx's own decision.
    With ``persist``: resolve interrupted intents, withdraw our pending entry
    for a recommendation dx rejected or retired, and record divergence."""
    report: list[dict[str, Any]] = []
    with store.mutation_lock():
        recs, fwds = store.load_recs(), store.load_forwards()
        for key, fwd in sorted(fwds.items()):
            if only is not None and key != only:
                continue
            rec = recs.get(key)
            dx_status = rec.status if rec else "MISSING"
            view = inspect(state_dir, fwd.slug, fwd.payload_sha256)
            state, note = fwd.state, fwd.note
            if state == "INTENT":
                if view == "pending-ours" or view in _DECIDED_VIEWS:
                    state, note = "WRITTEN", "recovered: the interrupted write had landed"
                elif view == "absent":
                    state, note = "INTERRUPTED", "recovered: the interrupted write never landed"
                else:
                    state, note = "NOT_WRITTEN", _RETRY + f"recovered: slug held by {view}"
            if persist and dx_status in ("REJECTED", "RETIRED") and view == "pending-ours":
                try:
                    view = withdraw_if_ours(state_dir, fwd.slug, fwd.payload_sha256)
                    if view == "absent":
                        state, note = "WITHDRAWN", f"withdrawn from pxx: dx {dx_status} it"
                except CycleLockBusy:
                    note = "withdraw pending: pxx cycle.lock busy; next pass retries"
            div = divergence(dx_status, view)
            info = (f"a pxx human {view} it while dx still has it {dx_status}"
                    if view in _DECIDED_VIEWS and dx_status in ("PROPOSED", "STALE") else None)
            changed = (state, view, note, div) != (fwd.state, fwd.pxx_view, fwd.note, fwd.divergence)
            if persist and changed:
                updated = replace(fwd, state=state, pxx_view=view, note=note, divergence=div,
                                  updated_at=now)
                try:
                    store.commit(Tx(ACTOR, "forward-reconcile", key, now, writes=[updated],
                                    detail={"state": state, "pxx_view": view, "divergence": div}))
                except StaleDecision:
                    continue
            report.append({"rec_key": key, "slug": fwd.slug, "dx_status": dx_status,
                           "state": state, "pxx_view": view, "divergence": div, "info": info,
                           "note": note})
    return report
