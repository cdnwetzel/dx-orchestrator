"""Tail-hash anchoring (A5) — the line between tamper-evident and tamper-evident
*to a third party*.

A hash-chained log (the gatekeeper audit, the ledger) is tamper-evident to anyone
who holds an earlier copy: rewrite a row and the chain breaks from there. But an
attacker who controls the whole file can rewrite it end to end and recompute every
hash — the chain still verifies. What they cannot do is change a value you already
copied *somewhere they don't control*.

Anchoring does exactly that on a cadence: it reads a source chain's current tail
hash and appends it to a **separate** anchor log kept in an independently-controlled
store (cold storage, a second repo, print). The anchor log is itself a
``verify_chain``-compatible chain, so an examiner with only the cold-storage copy
can (1) verify the anchor log was not rewritten, and (2) compare each source's live
tail to the value anchored at the time — a divergence is tamper-after-anchor,
provable without trusting the machine that produced the source.
"""

from __future__ import annotations

import json
from pathlib import Path

from dx.ledger_writer import LedgerWriteError, append_row, build_row, read_head

#: The action a tail-anchor row carries in the anchor log. Its own store, its own
#: chain — ``verify_chain`` validates the chaining, not the action vocabulary.
ANCHOR_ACTION = "ANCHOR"

_EVIDENCE_PREFIX = " tail sha256:"


def _evidence(source: str, tail_hash: str) -> str:
    return f"{source}{_EVIDENCE_PREFIX}{tail_hash}"


def anchor_tail(anchor_repo: Path, *, source: str, tail_hash: str) -> str:
    """Append an anchor row pinning ``source``'s current ``tail_hash`` to the
    anchor log in ``anchor_repo`` (a ledger repo with a genesis row). Returns the
    new anchor-log head. The row chains onto the anchor log, so the anchor history
    is itself append-only and independently verifiable."""
    ledger = anchor_repo / "ledger.jsonl"
    row = build_row(
        action=ANCHOR_ACTION,
        task_id=f"anchor:{source}",
        evidence=_evidence(source, tail_hash),
        prev_hash=read_head(ledger),
    )
    return append_row(anchor_repo, row)


def latest_anchored_tail(anchor_ledger: Path, source: str) -> str | None:
    """The most recently anchored tail hash for ``source``, or None if the anchor
    log has never anchored it."""
    if not anchor_ledger.exists():
        raise LedgerWriteError(f"anchor ledger not found: {anchor_ledger}")
    prefix = _evidence(source, "")
    found: str | None = None
    for line in anchor_ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("action") == ANCHOR_ACTION and str(row.get("evidence", "")).startswith(prefix):
            found = str(row["evidence"])[len(prefix):]
    return found


def tail_matches_anchor(anchor_ledger: Path, source: str, current_tail: str) -> bool:
    """True iff ``current_tail`` equals the most recently anchored tail for
    ``source``. A False means either the source was never anchored, or it was
    rewritten after it was anchored — the tamper an examiner is looking for."""
    anchored = latest_anchored_tail(anchor_ledger, source)
    return anchored is not None and anchored == current_tail
