"""The RL-010 (Decision 0017) approval-key gates, written before the wiring.

Two gates, two shapes each — the point the amendment turns on is that "exportable"
must be a *discriminator*, not an English word:

  spec 1 — residency: a real approval row (claiming the RL-010 standard) requires
    a card-resident signing secret. The parser is pinned against both real gpg
    output (a generated software key) and the documented card-stub format, and
    the gate refuses the software shape / accepts the card shape.

  spec 2 — the test double is barred *structurally*, not by policy: it registers
    under docs/keys/test-doubles/, which the real keyring builder never reads, so
    a row it signed fails verify_detached_signature by construction — even when
    every policy field says the mechanism is fine. This is the 0.10.0 fake_ledger
    lesson: bar the fixture by making the real path unable to see it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from dx.approval_key import (
    MECHANISM_FALLBACK,
    MECHANISM_STANDARD,
    TEST_DOUBLE_DIRNAME,
    ApprovalKeyError,
    parse_secret_key_residency,
    refuse_unless_card_resident,
    signing_key_residency,
)
from dx.ledger_utils import LedgerError, _registered_key_files, verify_detached_signature

# --- spec 1, the pure discriminator (no gpg needed) -------------------------

# Real gpg field 15 (index 14) shows "+" for an on-disk (software) secret.
_SOFTWARE_COLONS = (
    "sec:u:255:22:AAAA1111BBBB2222:1789062664:::u:::scSC:::+::ed25519:::0:\n"
    "fpr:::::::::AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555:\n"
    "grp:::::::::0123456789ABCDEF0123456789ABCDEF01234567:\n"
)

# A card-resident sign-only key: field 15 carries the token serial instead of "+".
_CARD_SERIAL = "D2760001240103040006123456780000"
_CARD_COLONS = (
    f"sec:u:255:22:AAAA1111BBBB2222:1789062664:::u:::scSC:::{_CARD_SERIAL}::ed25519:::0:\n"
    "fpr:::::::::AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555:\n"
    "grp:::::::::0123456789ABCDEF0123456789ABCDEF01234567:\n"
)

# A key with a SOFTWARE signing secret ("+") and a card-resident ENCRYPTION
# subkey (serial, cap "e"). The card serial must not upgrade it to "card":
# it can still sign without the token.
_ENCRYPTION_ONLY_CARD_COLONS = (
    "sec:u:255:22:AAAA1111BBBB2222:1789062664:::u:::scSC:::+::ed25519:::0:\n"
    f"ssb:u:255:18:EEEE9999FFFF8888:1789062664::::::e:::{_CARD_SERIAL}::cv25519::\n"
)


def test_software_key_reads_as_software():
    assert parse_secret_key_residency(_SOFTWARE_COLONS) == "software"


def test_card_stub_reads_as_card():
    assert parse_secret_key_residency(_CARD_COLONS) == "card"


def test_a_card_encryption_subkey_does_not_make_a_software_signer_card():
    # The signing secret is software; only the encryption subkey is on a card.
    assert parse_secret_key_residency(_ENCRYPTION_ONLY_CARD_COLONS) == "software"


def test_no_secret_reads_as_absent():
    assert parse_secret_key_residency("pub:u:255:22:AAAA:1789062664:::u:::scSC:::::::\n") == "absent"


def test_the_serial_must_be_the_token_field_not_a_uid_substring():
    # A serial-looking string in the user-id field (index 9) must NOT count; the
    # real signal is field 15, which here is "+".
    line = f"sec:u:255:22:AAAA:1789062664:::u:{_CARD_SERIAL}::scSC:::+::ed25519:::0:\n"
    assert parse_secret_key_residency(line) == "software"


# --- spec 1, the gate -------------------------------------------------------


def test_a_software_key_claiming_the_standard_is_refused():
    with pytest.raises(ApprovalKeyError, match="not a non-exportable card stub"):
        refuse_unless_card_resident("software", claimed_mechanism=MECHANISM_STANDARD)


def test_a_card_key_claiming_the_standard_is_accepted():
    refuse_unless_card_resident("card", claimed_mechanism=MECHANISM_STANDARD)  # no raise


def test_a_software_key_may_sign_as_a_marked_fallback():
    refuse_unless_card_resident("software", claimed_mechanism=MECHANISM_FALLBACK)  # no raise


# --- spec 1, the parser against REAL gpg output -----------------------------

_HAVE_GPG = shutil.which("gpg") is not None
gpg_only = pytest.mark.skipif(not _HAVE_GPG, reason="gpg not installed")


def _gpg(home: Path, *args: str, stdin: bytes | None = None):
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--no-tty",
         "--pinentry-mode", "loopback", "--passphrase", "", *args],
        input=stdin, capture_output=True, env={**os.environ, "GNUPGHOME": str(home)},
    )


def _short_home() -> Path:
    # Short prefix: a long homedir pushes the gpg-agent socket past the 104-byte
    # AF_UNIX limit and gpg fails to connect (the macOS lesson in the gpg suite).
    return Path(tempfile.mkdtemp(prefix="dx-ak-"))


def _kill_agent(home: Path) -> None:
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"],
                   capture_output=True)


def _gen_software_key(home: Path, uid: str) -> str:
    r = _gpg(home, "--quick-generate-key", uid, "ed25519", "sign", "never")
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    colons = _gpg(home, "--with-colons", "--list-secret-keys", uid).stdout.decode()
    for line in colons.splitlines():
        f = line.split(":")
        if f[0] == "fpr":
            return f[9]
    raise AssertionError("no fingerprint")


@gpg_only
def test_a_real_generated_software_key_reads_as_software_and_is_refused():
    home = _short_home()
    try:
        fpr = _gen_software_key(home, "Sw Signer <sw@example.invalid>")
        residency = signing_key_residency(fpr, gpg_home=home)
        assert residency == "software", "a gpg-generated on-disk key must read as software"
        with pytest.raises(ApprovalKeyError):
            refuse_unless_card_resident(residency, claimed_mechanism=MECHANISM_STANDARD)
    finally:
        _kill_agent(home)
        shutil.rmtree(home, ignore_errors=True)


# --- spec 2, the structural test-double bar ---------------------------------


def _ledger_repo_with(tmp_path: Path, registered: dict[str, str], doubles: dict[str, str]) -> Path:
    """A ledger repo with public keys under docs/keys/ and doubles one level
    deeper under docs/keys/test-doubles/. `registered`/`doubles` map name->home."""
    repo = tmp_path / "ledger"
    keys = repo / "docs" / "keys"
    (keys / TEST_DOUBLE_DIRNAME).mkdir(parents=True)
    for name, home in registered.items():
        pub = _gpg(Path(home), "--armor", "--export").stdout
        (keys / f"{name}.asc").write_bytes(pub)
    for name, home in doubles.items():
        pub = _gpg(Path(home), "--armor", "--export").stdout
        (keys / TEST_DOUBLE_DIRNAME / f"{name}.asc").write_bytes(pub)
    return repo


def test_registered_key_files_excludes_the_test_double_dir(tmp_path):
    keys = tmp_path / "docs" / "keys"
    (keys / TEST_DOUBLE_DIRNAME).mkdir(parents=True)
    (keys / "real.asc").write_text("x")
    (keys / TEST_DOUBLE_DIRNAME / "double.asc").write_text("x")
    files = {p.name for p in _registered_key_files(keys)}
    assert files == {"real.asc"}, "a test-double key must never be a registered key"


@gpg_only
def test_a_row_signed_by_a_test_double_fails_verification_by_construction(tmp_path):
    real_home = _short_home()
    double_home = _short_home()
    try:
        _gen_software_key(real_home, "Real Reviewer <real@example.invalid>")
        _gen_software_key(double_home, "CI Double <double@example.invalid>")
        repo = _ledger_repo_with(
            tmp_path, registered={"real": str(real_home)}, doubles={"double": str(double_home)}
        )
        # The double signs a perfectly well-formed message.
        msg = tmp_path / "m.msg"
        msg.write_bytes(b"T-1" + b"a" * 64 + b"code_review")
        sig = tmp_path / "m.sig"
        r = _gpg(double_home, "--armor", "--detach-sign", "--output", str(sig), str(msg))
        assert r.returncode == 0

        # Even though the signature is cryptographically valid and (imagine) the
        # bundle's mechanism field says "standard", the real keyring never held
        # the double's key, so verification fails by construction — not by a
        # policy check that could be bypassed.
        with pytest.raises(LedgerError):
            verify_detached_signature(sig, msg, repo)

        # And the same message signed by the REAL registered key verifies — so
        # the failure above is the double being barred, not a broken fixture.
        rsig = tmp_path / "r.sig"
        _gpg(real_home, "--armor", "--detach-sign", "--output", str(rsig), str(msg))
        signer = verify_detached_signature(rsig, msg, repo)
        assert "Real Reviewer" in signer.uid
    finally:
        for h in (real_home, double_home):
            _kill_agent(h)
            shutil.rmtree(h, ignore_errors=True)
