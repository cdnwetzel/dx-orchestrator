"""Tail-hash anchoring (A5): pin a chain's tail into a separate, verifiable store.

Proves the property that makes it examiner-grade: the anchor log is itself a
hash-chained, ``verify_chain``-verifiable ledger; the anchored tail is recoverable
per source; and a source rewritten after it was anchored no longer matches — the
tamper an outside examiner detects with only the cold-storage anchor log.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dx.ledger_writer import build_row, row_hash
from dx.tail_anchor import (
    ANCHOR_ACTION,
    anchor_tail,
    latest_anchored_tail,
    tail_matches_anchor,
)

_VERIFY_CHAIN = Path("~/ai/devswarm-ledger-reference/tools/verify_chain.py").expanduser()


def _new_anchor_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "anchor-log"
    repo.mkdir()
    genesis = build_row(action="GENESIS", task_id="ANCHOR-LOG", evidence="genesis", prev_hash="0" * 64)
    (repo / "ledger.jsonl").write_text(
        json.dumps(genesis, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=a@b.invalid", "-c", "user.name=t", "commit", "-q", "-m", "genesis"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def _rows(ledger: Path) -> list[dict]:
    return [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]


def _chain_is_intact(ledger: Path) -> bool:
    rows = _rows(ledger)
    for prev, cur in zip(rows, rows[1:], strict=False):
        if cur["prev_hash"] != row_hash(prev):
            return False
    return True


def test_anchoring_records_the_tail_and_keeps_the_anchor_chain_intact(tmp_path):
    repo = _new_anchor_repo(tmp_path)
    ledger = repo / "ledger.jsonl"
    anchor_tail(repo, source="gatekeeper-audit", tail_hash="a" * 64)
    assert latest_anchored_tail(ledger, "gatekeeper-audit") == "a" * 64
    assert _chain_is_intact(ledger)
    assert [r["action"] for r in _rows(ledger)] == ["GENESIS", ANCHOR_ACTION]


def test_a_tail_that_still_matches_its_anchor_passes(tmp_path):
    repo = _new_anchor_repo(tmp_path)
    anchor_tail(repo, source="ledger", tail_hash="b" * 64)
    assert tail_matches_anchor(repo / "ledger.jsonl", "ledger", "b" * 64) is True


def test_a_source_rewritten_after_anchoring_no_longer_matches(tmp_path):
    repo = _new_anchor_repo(tmp_path)
    anchor_tail(repo, source="ledger", tail_hash="b" * 64)
    # The live source chain was rewritten end-to-end; its recomputed tail differs
    # from what cold storage anchored. That mismatch is the tamper signal.
    assert tail_matches_anchor(repo / "ledger.jsonl", "ledger", "deadbeef" + "0" * 56) is False


def test_an_unanchored_source_never_matches(tmp_path):
    repo = _new_anchor_repo(tmp_path)
    anchor_tail(repo, source="ledger", tail_hash="b" * 64)
    assert tail_matches_anchor(repo / "ledger.jsonl", "gatekeeper-audit", "b" * 64) is False


def test_sources_are_anchored_independently(tmp_path):
    repo = _new_anchor_repo(tmp_path)
    anchor_tail(repo, source="gatekeeper-audit", tail_hash="a" * 64)
    anchor_tail(repo, source="ledger", tail_hash="b" * 64)
    ledger = repo / "ledger.jsonl"
    assert latest_anchored_tail(ledger, "gatekeeper-audit") == "a" * 64
    assert latest_anchored_tail(ledger, "ledger") == "b" * 64


def test_reanchoring_a_source_records_the_new_tail_without_losing_history(tmp_path):
    repo = _new_anchor_repo(tmp_path)
    ledger = repo / "ledger.jsonl"
    anchor_tail(repo, source="ledger", tail_hash="1" * 64)
    anchor_tail(repo, source="ledger", tail_hash="2" * 64)  # the chain advanced; re-anchor
    assert latest_anchored_tail(ledger, "ledger") == "2" * 64
    # both anchors remain in the append-only log
    anchors = [r for r in _rows(ledger) if r["action"] == ANCHOR_ACTION]
    assert len(anchors) == 2 and _chain_is_intact(ledger)


def test_a_source_is_matched_by_exact_identity_not_a_prefix(tmp_path):
    # "ledger" must not read "ledger-audit"'s anchor: a prefix match here would
    # let one source's tail be mistaken for another's.
    repo = _new_anchor_repo(tmp_path)
    ledger = repo / "ledger.jsonl"
    anchor_tail(repo, source="ledger-audit", tail_hash="a" * 64)
    assert latest_anchored_tail(ledger, "ledger") is None
    assert latest_anchored_tail(ledger, "ledger-audit") == "a" * 64


def test_anchoring_a_non_hex_tail_is_refused(tmp_path):
    from dx.ledger_writer import LedgerWriteError

    repo = _new_anchor_repo(tmp_path)
    with pytest.raises(LedgerWriteError, match="sha256"):
        anchor_tail(repo, source="ledger", tail_hash="not-a-hash")


@pytest.mark.skipif(not _VERIFY_CHAIN.is_file(), reason="reference verify_chain.py not present")
def test_the_anchor_log_verifies_with_the_reference_verify_chain(tmp_path):
    # The A5 exit criterion: anchoring rows verifiable by tools/verify_chain.py
    # from cold storage — the same tool dx treats as the ledger contract.
    repo = _new_anchor_repo(tmp_path)
    anchor_tail(repo, source="ledger", tail_hash="c" * 64)
    result = subprocess.run(
        ["python3", str(_VERIFY_CHAIN), str(repo / "ledger.jsonl")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "row(s) verified" in result.stdout
