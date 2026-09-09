"""RL-003 primitives: the canonical approval payload and the GPG accept/reject
policy.

The GPG policy is tested through `classify_gpg_status` against captured
`--status-fd` transcripts rather than by generating real expired/revoked keys.
That keeps the security-relevant branch (a retired key must not clear the gate)
under test in CI without a key-generation fixture.
"""
import json

import pytest

from dx.ledger_utils import (
    LedgerError,
    SignerIdentity,
    canonical_approval_message,
    classify_gpg_status,
    get_ledger_head,
    get_task_author_human,
    get_task_queue,
)

FPR = "1234567890ABCDEF1234567890ABCDEF12345678"
HEAD = "a" * 64


def _status(*lines: str) -> str:
    return "\n".join(f"[GNUPG:] {line}" for line in lines) + "\n"


GOOD = _status(
    "NEWSIG",
    "KEY_CONSIDERED " + FPR + " 0",
    "GOODSIG DEADBEEFDEADBEEF Chris Wetzel <chris@example.invalid>",
    f"VALIDSIG {FPR} 2026-09-07 1757000000 0 4 0 22 8 00 {FPR}",
    "TRUST_UNDEFINED 0 pgp",
)


# ---------------------------------------------------------------------------
# canonical approval message
# ---------------------------------------------------------------------------


def test_canonical_message_is_bare_concatenation():
    """SCHEMA.md: exact concatenation, no separators, no newline."""
    msg = canonical_approval_message("T-0007", HEAD, "code_review")
    assert msg == f"T-0007{HEAD}code_review"
    assert "\n" not in msg
    assert " " not in msg


def test_canonical_message_length_is_stable():
    msg = canonical_approval_message("T-0007", HEAD, "code_review")
    assert len(msg) == len("T-0007") + 64 + len("code_review")


def test_canonical_message_is_head_sensitive():
    """A different head must produce a different payload — that is what makes a
    stale signature detectable (RL-003).
    """
    a = canonical_approval_message("T-0007", "a" * 64, "code_review")
    b = canonical_approval_message("T-0007", "b" * 64, "code_review")
    assert a != b


# ---------------------------------------------------------------------------
# GPG accept / reject policy
# ---------------------------------------------------------------------------


def test_good_signature_yields_fingerprint():
    assert classify_gpg_status(GOOD, 0) == FPR


@pytest.mark.parametrize(
    "code,needle",
    [
        ("REVKEYSIG", "revoked"),
        ("KEYREVOKED", "revoked"),
        ("EXPKEYSIG", "expired"),
        ("KEYEXPIRED", "expired"),
        ("EXPSIG", "expired"),
        ("SIGEXPIRED", "expired"),
    ],
)
def test_degraded_signatures_are_rejected(code, needle):
    """gpg exits 0 and emits VALIDSIG for a signature from a revoked or expired
    key. Gating on the return code alone would let a retired approval key
    through the RL-003 check.
    """
    status = _status(
        f"{code} DEADBEEFDEADBEEF Chris Wetzel <chris@example.invalid>",
        f"VALIDSIG {FPR} 2026-09-07 1757000000 0 4 0 22 8 00 {FPR}",
    )
    with pytest.raises(LedgerError) as exc:
        classify_gpg_status(status, 0)
    assert needle in str(exc.value)
    assert code in str(exc.value)


def test_rejection_wins_even_alongside_goodsig():
    """Defence in depth: a transcript carrying both must still be rejected."""
    status = GOOD + _status("KEYREVOKED")
    with pytest.raises(LedgerError):
        classify_gpg_status(status, 0)


def test_bad_signature_rejected():
    status = _status("BADSIG DEADBEEFDEADBEEF Someone <nobody@example.invalid>")
    with pytest.raises(LedgerError) as exc:
        classify_gpg_status(status, 1)
    assert "invalid or signer not in" in str(exc.value)


def test_unknown_signer_rejected():
    """No key in docs/keys/ matched — gpg emits NO_PUBKEY and exits non-zero."""
    status = _status("NO_PUBKEY DEADBEEFDEADBEEF")
    with pytest.raises(LedgerError):
        classify_gpg_status(status, 2)


def test_validsig_without_goodsig_rejected():
    status = _status(f"VALIDSIG {FPR} 2026-09-07 1757000000 0 4 0 22 8 00 {FPR}")
    with pytest.raises(LedgerError):
        classify_gpg_status(status, 0)


def test_goodsig_without_validsig_fingerprint_rejected():
    status = _status("GOODSIG DEADBEEFDEADBEEF Chris Wetzel <chris@example.invalid>")
    with pytest.raises(LedgerError) as exc:
        classify_gpg_status(status, 0)
    assert "no VALIDSIG fingerprint" in str(exc.value)


def test_empty_status_rejected():
    with pytest.raises(LedgerError):
        classify_gpg_status("", 0)


# ---------------------------------------------------------------------------
# signer identity parsing
# ---------------------------------------------------------------------------


def test_signer_identity_splits_name_and_email():
    ident = SignerIdentity(fingerprint=FPR, uid="Chris Wetzel (dx key) <chris@example.invalid>")
    assert ident.name == "Chris Wetzel"
    assert ident.email == "chris@example.invalid"


def test_signer_identity_without_comment():
    ident = SignerIdentity(fingerprint=FPR, uid="Chris Wetzel <chris@example.invalid>")
    assert ident.name == "Chris Wetzel"


def test_signer_identity_without_email():
    ident = SignerIdentity(fingerprint=FPR, uid="Chris Wetzel")
    assert ident.name == "Chris Wetzel"
    assert ident.email is None


# ---------------------------------------------------------------------------
# ledger / queue reads
# ---------------------------------------------------------------------------


def test_get_task_queue_reads_the_state_file(fake_ledger):
    assert get_task_queue("T-TEST", fake_ledger)["approve_role"] == "code_review"


def test_missing_queue_file_is_a_ledger_error(fake_ledger):
    with pytest.raises(LedgerError) as exc:
        get_task_queue("T-ABSENT", fake_ledger)
    assert "queue file not found" in str(exc.value)


def test_author_human_comes_from_the_earliest_matching_row(fake_ledger):
    assert get_task_author_human("T-TEST", fake_ledger) == "Alice Author"


def test_author_human_is_none_for_unknown_task(fake_ledger):
    assert get_task_author_human("T-NOPE", fake_ledger) is None


def test_head_is_read_from_the_reference_verifier(fake_ledger, ledger_head):
    """dx never parses ledger.jsonl for the head — verify_chain.py is the single
    source of truth, so a broken chain stops the line (RL-009).
    """
    assert get_ledger_head(fake_ledger) == ledger_head


def test_broken_chain_surfaces_the_verifier_failure(fake_ledger):
    verifier = fake_ledger / "tools" / "verify_chain.py"
    verifier.write_text(
        "import sys\nprint('CHAIN BROKEN at row 3', file=sys.stderr)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    with pytest.raises(LedgerError) as exc:
        get_ledger_head(fake_ledger)
    assert "CHAIN BROKEN" in str(exc.value)


def test_missing_verifier_is_a_ledger_error(fake_ledger):
    (fake_ledger / "tools" / "verify_chain.py").unlink()
    with pytest.raises(LedgerError) as exc:
        get_ledger_head(fake_ledger)
    assert "verify_chain.py not found" in str(exc.value)


def test_unparseable_verifier_output_is_a_ledger_error(fake_ledger):
    (fake_ledger / "tools" / "verify_chain.py").write_text(
        "print('everything is fine')\n", encoding="utf-8"
    )
    with pytest.raises(LedgerError) as exc:
        get_ledger_head(fake_ledger)
    assert "could not parse head hash" in str(exc.value)


def test_ledger_rows_are_valid_json(fake_ledger):
    for line in (fake_ledger / "ledger.jsonl").read_text().splitlines():
        if line.strip():
            json.loads(line)
