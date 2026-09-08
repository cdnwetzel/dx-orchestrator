"""Thin helpers around cdnwetzel/devswarm-ledger.

Design rule: never re-implement anything the ledger repo already ships.
Chain verification is delegated to `tools/verify_chain.py` in the ledger
clone; GPG operations go through the system `gpg` binary. This module
provides just enough plumbing for cmd_merge.py to enforce the RL-003
signature contract described in devswarm-ledger/SCHEMA.md.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LedgerError(RuntimeError):
    """Ledger state is inconsistent or unreachable — stop the line."""


@dataclass(frozen=True)
class SignerIdentity:
    fingerprint: str
    uid: str  # full GPG user-ID string, e.g. "A Name (comment) <a@example.invalid>"

    @property
    def email(self) -> str | None:
        m = re.search(r"<([^>]+)>", self.uid)
        return m.group(1) if m else None

    @property
    def name(self) -> str:
        """The bare name from the GPG uid, with comment and email removed.

        A uid may take any of these forms::

            Chris Wetzel <chris@example.invalid>
            Chris Wetzel (dx signing key) <chris@example.invalid>
            Chris Wetzel

        cmd_merge compares this against the ledger's ``author_human`` to enforce
        separation of duties, so every form must reduce to the same bare name.
        Stripping only the ``(comment)`` left the ``<email>`` attached whenever a
        key had no comment field — the comparison could then never match and the
        separation-of-duties check failed open.
        """
        uid = re.sub(r"\([^)]*\)", " ", self.uid)  # drop the comment field
        uid = re.sub(r"<[^>]*>", " ", uid)  # drop the email field
        return " ".join(uid.split()) or self.uid


# ---------------------------------------------------------------------------
# ledger chain / head
# ---------------------------------------------------------------------------

_HEAD_LINE_RE = re.compile(r"Ledger head hash:\s*([0-9a-f]{64})", re.IGNORECASE)


def get_ledger_head(ledger_repo: Path) -> str:
    """Run verify_chain.py and return the head hash. Raises LedgerError on failure.

    Single source of truth: we don't parse ledger.jsonl ourselves. If the
    chain is broken, verify_chain.py exits 1 with a stop-the-line message
    (RL-009) and we surface that unchanged.
    """
    verifier = ledger_repo / "tools" / "verify_chain.py"
    ledger = ledger_repo / "ledger.jsonl"
    if not verifier.exists():
        raise LedgerError(f"verify_chain.py not found at {verifier}")
    if not ledger.exists():
        raise LedgerError(f"ledger.jsonl not found at {ledger}")

    result = subprocess.run(
        [sys.executable, str(verifier), str(ledger)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LedgerError(
            f"chain verification failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )
    m = _HEAD_LINE_RE.search(result.stdout)
    if not m:
        raise LedgerError(
            f"could not parse head hash from verifier output:\n{result.stdout}"
        )
    return m.group(1)


# ---------------------------------------------------------------------------
# task metadata (queue file + ledger scan)
# ---------------------------------------------------------------------------


def get_task_queue(task_id: str, ledger_repo: Path) -> dict[str, Any]:
    """Read queue/<task_id>.json — the per-task state file."""
    path = ledger_repo / "queue" / f"{task_id}.json"
    if not path.exists():
        raise LedgerError(f"queue file not found: {path}")
    with path.open(encoding="utf-8") as f:
        queue: dict[str, Any] = json.load(f)
    return queue


def get_task_author_human(task_id: str, ledger_repo: Path) -> str | None:
    """Scan ledger.jsonl for the earliest row for this task and return
    the `author_human` field. Used for separation-of-duties (signer ≠ author).
    """
    ledger = ledger_repo / "ledger.jsonl"
    if not ledger.exists():
        raise LedgerError(f"ledger.jsonl not found at {ledger}")
    with ledger.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("task_id") == task_id and row.get("author_human"):
                author: str = row["author_human"]
                return author
    return None


# ---------------------------------------------------------------------------
# GPG operations (isolated keyring so we never touch the user's default one)
# ---------------------------------------------------------------------------


def _gpg(
    *args: str, keyring: Path, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Invoke gpg with a scratch keyring. Never uses the user's default keyring."""
    cmd = [
        "gpg",
        "--homedir", str(keyring),
        "--batch",
        "--no-tty",
        "--status-fd", "1",
        *args,
    ]
    return subprocess.run(cmd, input=input_bytes, capture_output=True)


def _import_registered_keys(ledger_repo: Path, keyring: Path) -> None:
    keys_dir = ledger_repo / "docs" / "keys"
    if not keys_dir.is_dir():
        raise LedgerError(f"docs/keys directory not found at {keys_dir}")
    for asc in sorted(keys_dir.glob("*.asc")):
        result = _gpg("--import", str(asc), keyring=keyring)
        if result.returncode != 0:
            raise LedgerError(
                f"failed to import {asc.name}: {result.stderr.decode(errors='replace').strip()}"
            )


# gpg --fingerprint prints lines like:
#   uid           [ unknown] A Name (comment) <a@example.invalid>
# Skip the leading "uid" label AND any bracketed trust marker like "[ unknown]".
_UID_LINE_RE = re.compile(
    rb"^uid\s+(?:\[[^\]]*\]\s*)?(.+)$", re.MULTILINE
)


def _lookup_identity(fingerprint: str, keyring: Path) -> SignerIdentity:
    result = _gpg("--fingerprint", fingerprint, keyring=keyring)
    if result.returncode != 0:
        raise LedgerError(
            f"gpg could not resolve fingerprint {fingerprint}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    uid_match = _UID_LINE_RE.search(result.stdout)
    uid = uid_match.group(1).decode("utf-8", errors="replace").strip() if uid_match else "(unknown)"
    return SignerIdentity(fingerprint=fingerprint.replace(" ", ""), uid=uid)


# GnuPG emits exactly ONE of GOODSIG / EXPSIG / EXPKEYSIG / REVKEYSIG for a
# cryptographically-valid signature, and returns exit code 0 for all four.
# VALIDSIG is emitted for all four as well. So neither the return code nor
# VALIDSIG alone is a sufficient gate: a signature made by a revoked or expired
# approval key would clear it. RL-003 requires the key be currently valid, so we
# reject the three degraded outcomes explicitly and require GOODSIG.
_REJECT_STATUS = {
    "REVKEYSIG": "the signing key has been revoked",
    "KEYREVOKED": "the signing key has been revoked",
    "EXPKEYSIG": "the signing key has expired",
    "KEYEXPIRED": "the signing key has expired",
    "EXPSIG": "the signature itself has expired",
    "SIGEXPIRED": "the signature itself has expired",
}


def classify_gpg_status(status: str, returncode: int) -> str:
    """Map gpg --status-fd output to a fingerprint, or raise LedgerError.

    Split out from verify_detached_signature so the accept/reject policy is
    unit-testable without generating real keys.
    """
    for code, reason in _REJECT_STATUS.items():
        if re.search(rf"\[GNUPG:\] {code}\b", status):
            raise LedgerError(
                f"signature rejected (RL-003): {reason} [{code}]. "
                "Re-sign with a currently-valid key registered in docs/keys/."
            )

    if returncode != 0 or "[GNUPG:] GOODSIG" not in status:
        raise LedgerError(
            "signature invalid or signer not in devswarm-ledger/docs/keys/:\n"
            + status.strip()
        )

    m = re.search(r"\[GNUPG:\] VALIDSIG ([0-9A-F]{40})", status)
    if not m:
        raise LedgerError(
            "gpg reported GOODSIG but no VALIDSIG fingerprint:\n" + status.strip()
        )
    return m.group(1)


def verify_detached_signature(
    signature_path: Path,
    message_path: Path,
    ledger_repo: Path,
) -> SignerIdentity:
    """Verify a detached GPG signature using only keys registered in
    devswarm-ledger/docs/keys/. Returns the signer's identity on success;
    raises LedgerError on any failure (bad signature, unknown signer, missing
    key, or a key that is expired or revoked).
    """
    if not signature_path.exists():
        raise LedgerError(f"signature file not found: {signature_path}")
    if not message_path.exists():
        raise LedgerError(f"message file not found: {message_path}")

    with tempfile.TemporaryDirectory(prefix="dx-gpg-") as tmpdir:
        keyring = Path(tmpdir)
        keyring.chmod(0o700)
        _import_registered_keys(ledger_repo, keyring)

        result = _gpg(
            "--verify", str(signature_path), str(message_path),
            keyring=keyring,
        )
        status = result.stdout.decode("utf-8", errors="replace")
        if not status.strip():
            status = result.stderr.decode("utf-8", errors="replace")

        fingerprint = classify_gpg_status(status, result.returncode)
        return _lookup_identity(fingerprint, keyring)


# ---------------------------------------------------------------------------
# The canonical string that approvals sign (SCHEMA.md § approvals/)
# ---------------------------------------------------------------------------


def canonical_approval_message(task_id: str, ledger_head: str, role: str) -> str:
    """Build the exact concat string that .msg files contain, per SCHEMA.md."""
    return f"{task_id}{ledger_head}{role}"
