"""Is an approval key hardware-resident? — the RL-010 (Decision 0017) gate.

RL-010, as amended, requires an approval key's private material to be
**hardware-resident and non-exportable**: a CCID OpenPGP card, not an on-disk
secret. This module makes "exportable" a *deterministic* property of `gpg`
output rather than an English word: it reads `gpg --with-colons
--list-secret-keys` and decides residency from the token serial GnuPG prints in
field 15 of a `sec`/`ssb` record ("S/N of a token", per GnuPG DETAILS). A
card-resident signing secret carries that serial; a software key shows "+"
(secret on disk) there instead. If the check cannot tell the two apart, the gate
is only a claim —
so the two shapes are pinned by tests against real gpg output and the documented
card format.

The residency of a *secret* key is a fact about the signer's own box, visible
only there — a detached signature never carries it. So this gate runs where the
approval **row** is produced (the signing side), not in
`verify_detached_signature` (which holds only public keys). A real, standard
approval row requires a card-resident signing secret; a software key may sign
only as a *marked* fallback that names itself in the row.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# The `approval.mechanism` values a staged-action row may carry. The string is
# the honest label the ledger row keeps; the gate below maps residency to it.
MECHANISM_STANDARD = "openpgp-card"  # RL-010 standard: non-exportable hardware key
MECHANISM_FALLBACK = "software-ceremony (RL-010-non-compliant)"  # marked, transitional
MECHANISM_TEST_DOUBLE = "test double (RL-010 non-compliant)"  # CI hermetic; barred from real rows

# Registered public keys live in `docs/keys/`; a hermetic CI test double is
# registered one directory deeper, in `docs/keys/<TEST_DOUBLE_DIRNAME>/`, which
# the real keyring builder never descends into — so a double is barred from a
# real ledger row by construction, not by policy. See ledger_utils.
TEST_DOUBLE_DIRNAME = "test-doubles"

GPG_TIMEOUT_S = 20


class ApprovalKeyError(Exception):
    """A signing key does not meet the RL-010 residency the row claims."""


def parse_secret_key_residency(colon_text: str) -> str:
    """Classify `gpg --with-colons --list-secret-keys` output for one key.

    Returns:
        "card"     — every signing-capable secret is a smartcard stub (field 15
                     carries a token serial);
        "software" — a signing-capable secret is on disk (no token serial);
        "absent"   — no signing-capable secret is present.

    Software residency wins a tie: if any signing-capable secret is on disk, the
    key can sign without a token, so the whole key is treated as software. The
    discriminator is the serial in field 15 (GnuPG DETAILS: "S/N of a token"),
    never a substring of a comment or uid.
    """
    saw_card = False
    saw_software = False
    for line in colon_text.splitlines():
        fields = line.split(":")
        if not fields or fields[0] not in ("sec", "ssb"):
            continue
        # Field 12 (index 11) is the key-capability string; 's' means this
        # secret can sign. Skip encryption-only / auth-only secrets.
        caps = fields[11] if len(fields) > 11 else ""
        if "s" not in caps.lower():
            continue
        # Field 15 (index 14) reports the secret's location: gpg prints "+" when
        # the secret is available on disk (software), "#" when it is not present
        # on this box (an offline primary), and the **token serial** only when
        # the secret is a smartcard stub. So a real serial — anything that is not
        # "", "+", or "#" — is the card signal; "+" is software; "#"/"" is a
        # signing secret this box cannot use, which counts for neither.
        serial = fields[14].strip() if len(fields) > 14 else ""
        if serial and serial not in ("+", "#"):
            saw_card = True
        elif serial == "+":
            saw_software = True
    if saw_software:
        return "software"
    if saw_card:
        return "card"
    return "absent"


def signing_key_residency(fingerprint: str, *, gpg_home: Path | None = None) -> str:
    """Residency of the secret key `fingerprint` in a gpg home (default: the
    user's). Runs `gpg --with-colons --list-secret-keys` and classifies it."""
    gpg = shutil.which("gpg")
    if gpg is None:
        raise ApprovalKeyError("gpg not installed; cannot verify key residency")
    cmd = [gpg, "--batch", "--no-tty", "--with-colons", "--list-secret-keys"]
    if gpg_home is not None:
        cmd[1:1] = ["--homedir", str(gpg_home)]
    cmd.append(fingerprint)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GPG_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ApprovalKeyError(f"gpg timed out listing secret keys for {fingerprint}") from exc
    if result.returncode != 0:
        # No secret key for this fingerprint is itself "absent", not an error:
        # the caller decides whether absence is fatal for the claimed mechanism.
        return "absent"
    return parse_secret_key_residency(result.stdout)


def derive_mechanism(residency: str) -> str:
    """The mechanism a row *must* record for a signing key of this residency —
    derived from the discriminator, never taken from the signer. A card stub is
    the RL-010 standard; a software secret is the marked, transitional fallback;
    no secret at all cannot produce an approval."""
    if residency == "card":
        return MECHANISM_STANDARD
    if residency == "software":
        return MECHANISM_FALLBACK
    raise ApprovalKeyError(
        f"no signing secret present (residency {residency!r}); cannot record an approval"
    )


def resolve_mechanism(residency: str, *, claimed: str | None = None) -> str:
    """The recorded mechanism, derived from residency — the verifier's call, not
    the signer's. If the signer *claimed* a mechanism, it may not over-state what
    the key supports: claiming the hardware standard over a software key is
    laundering a weak approval through a strong label, and is refused. A weaker or
    matching claim is ignored; the derived value is authoritative either way.
    """
    derived = derive_mechanism(residency)
    if claimed == MECHANISM_STANDARD and derived != MECHANISM_STANDARD:
        raise ApprovalKeyError(
            f"mechanism laundering: the row claims {MECHANISM_STANDARD!r} (RL-010 standard) "
            f"but the signing key is {residency!r}, which supports only {derived!r}. "
            "The mechanism is derived from the key, never asserted by the signer."
        )
    return derived


def refuse_unless_card_resident(residency: str, *, claimed_mechanism: str) -> None:
    """Gate a would-be approval row against its claimed mechanism.

    A row claiming the RL-010 standard (`MECHANISM_STANDARD`) requires a
    card-resident signing secret; a software key claiming the standard is refused
    at row time — it may sign only as the marked fallback. This is the refusal
    the tests pin: the software shape rejected, the card shape accepted.
    """
    if claimed_mechanism == MECHANISM_STANDARD and residency != "card":
        raise ApprovalKeyError(
            f"approval claims mechanism {MECHANISM_STANDARD!r} (RL-010 standard) but the "
            f"signing key's secret is {residency!r}, not a non-exportable card stub. "
            f"Use a hardware token, or record this as {MECHANISM_FALLBACK!r}."
        )
