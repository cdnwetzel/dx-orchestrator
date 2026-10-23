#!/usr/bin/env python3
"""Regenerate the committed GPG test fixtures.

Emits PUBLIC key material and detached signatures only — no private keys ever
leave the temporary GNUPGHOME this script creates and destroys.

Run from the repository root:

    python tests/fixtures/gpg/regenerate.py

It generates three throwaway ed25519 keys, signs the canonical RL-003 approval
payload with each, revokes one, waits out the expiry of another, writes the
fixture files, and re-verifies every signature in a pristine keyring before
exiting non-zero on any surprise. See README.md in this directory for why the
fixtures are committed rather than generated at test time.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent

# Exactly the canonical approval message: task_id + ledger_head + role.
PAYLOAD = b"T-TEST" + b"a" * 64 + b"code_review"

SPECS = [
    ("valid", "Bob Reviewer <bob@example.invalid>", "never"),
    ("revoked", "Rev Signer <rev@example.invalid>", "never"),
    ("expired", "Exp Signer <exp@example.invalid>", "seconds=2"),
]

EXPIRY_TIMEOUT_S = 60


def gpg(home: Path, *args: str, stdin: bytes | None = None):
    return subprocess.run(
        [
            "gpg",
            "--homedir", str(home),
            "--batch",
            "--no-tty",
            "--pinentry-mode", "loopback",
            "--passphrase", "",
            *args,
        ],
        input=stdin,
        capture_output=True,
        env={**os.environ, "GNUPGHOME": str(home)},
    )


def _field(home: Path, uid: str, record: str, index: int) -> str:
    out = gpg(home, "--list-keys", "--with-colons", uid).stdout.decode()
    for line in out.splitlines():
        if line.startswith(f"{record}:"):
            return line.split(":")[index]
    raise SystemExit(f"no {record} record for {uid}\n{out}")


def validity(home: Path, uid: str) -> str:
    """'u'/'-' usable, 'r' revoked, 'e' expired."""
    return _field(home, uid, "pub", 1)


def fingerprint(home: Path, uid: str) -> str:
    return _field(home, uid, "fpr", 9)


def status_codes(status: str) -> list[str]:
    return [
        line.split()[1]
        for line in status.splitlines()
        if line.startswith("[GNUPG:]") and len(line.split()) > 1
    ]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        home = work / "gnupghome"
        home.mkdir(mode=0o700)
        payload = work / "payload.bin"
        payload.write_bytes(PAYLOAD)

        for name, uid, expiry in SPECS:
            result = gpg(home, "--quick-generate-key", uid, "ed25519", "sign", expiry)
            if result.returncode != 0:
                return int(bool(sys.stderr.write(result.stderr.decode())))
            result = gpg(
                home, "--local-user", uid, "--detach-sign", "--armor",
                "-o", str(work / f"{name}.sig"), str(payload),
            )
            if result.returncode != 0:
                return int(bool(sys.stderr.write(result.stderr.decode())))
            print(f"  generated {name:<8} {fingerprint(home, uid)}")

        # gpg writes a ready-made revocation certificate at key creation, with a
        # leading ':' on each armor line so it cannot be applied by accident.
        rev_uid = SPECS[1][1]
        cert = home / "openpgp-revocs.d" / f"{fingerprint(home, rev_uid)}.rev"
        uncommented = "\n".join(
            line[1:] if line.startswith(":") else line
            for line in cert.read_text().splitlines()
        )
        if gpg(home, "--import", stdin=uncommented.encode()).returncode != 0:
            return 1
        if validity(home, rev_uid) != "r":
            return int(bool(sys.stderr.write("revocation did not take\n")))
        print(f"  revoked   {rev_uid.split()[0]}")

        # Wait out the real expiry. Polling rather than sleeping a fixed amount
        # tolerates a host whose clock steps backwards mid-run.
        exp_uid = SPECS[2][1]
        deadline = time.monotonic() + EXPIRY_TIMEOUT_S
        while validity(home, exp_uid) != "e":
            if time.monotonic() > deadline:
                return int(bool(sys.stderr.write("key never expired\n")))
            time.sleep(0.5)
        print(f"  expired   {exp_uid.split()[0]}")

        (OUT / "payload.bin").write_bytes(PAYLOAD)
        for name, uid, _ in SPECS:
            pub = gpg(home, "--export", "--armor", uid).stdout
            if not pub.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK"):
                return int(bool(sys.stderr.write(f"bad export for {name}\n")))
            (OUT / f"{name}.pub.asc").write_bytes(pub)
            (OUT / f"{name}.sig.asc").write_bytes((work / f"{name}.sig").read_bytes())

        gpg(home, "--version")  # no-op; keeps the agent alive until teardown
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "all"],
            capture_output=True, check=False,
        )

    # Re-verify each fixture in a pristine keyring — the same thing dx does.
    expected = {"valid": "GOODSIG", "revoked": "REVKEYSIG", "expired": "EXPKEYSIG"}
    failures = 0
    for name, _, _ in SPECS:
        with tempfile.TemporaryDirectory() as sd:
            scratch = Path(sd)
            scratch.chmod(0o700)
            gpg(scratch, "--import", stdin=(OUT / f"{name}.pub.asc").read_bytes())
            result = gpg(
                scratch, "--status-fd", "1", "--verify",
                str(OUT / f"{name}.sig.asc"), str(OUT / "payload.bin"),
            )
            codes = status_codes(result.stdout.decode())
            ok = expected[name] in codes and "VALIDSIG" in codes
            failures += not ok
            print(
                f"  verify {name:<8} rc={result.returncode} "
                f"{'OK' if ok else 'UNEXPECTED'} codes={','.join(codes)}"
            )
            subprocess.run(
                ["gpgconf", "--homedir", str(scratch), "--kill", "all"],
                capture_output=True, check=False,
            )

    if failures:
        sys.stderr.write(f"{failures} fixture(s) did not verify as expected\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
