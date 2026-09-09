"""End-to-end GPG tests against real keys.

The rest of the suite exercises the accept/reject policy against synthetic
``--status-fd`` transcripts. That proves the parser, not the premise. These
tests run the real ``verify_detached_signature`` path — scratch keyring import
and all — against committed public keys and signatures from three throwaway
ed25519 keys: one usable, one revoked, one expired.

They hold one claim honest: **gpg exits 0 and emits VALIDSIG for signatures
made by revoked and expired keys.** That is why gating on the return code was
insufficient, and why RL-003 verification requires GOODSIG. If a future GnuPG
changes that behavior, these tests say so instead of passing for the wrong
reason.

See ``tests/fixtures/gpg/README.md`` for what the fixtures are and how to
regenerate them. They are committed rather than generated per-run because the
generated version was flaky on a host whose clock ran backwards; nothing here
depends on wall-clock time.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from dx.cli import build_parser
from dx.ledger_utils import LedgerError, verify_detached_signature

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")

GPG_FIXTURES = Path(__file__).parent / "fixtures" / "gpg"
KEYS = ("valid", "revoked", "expired")


@pytest.fixture
def payload() -> Path:
    return GPG_FIXTURES / "payload.bin"


@pytest.fixture
def ledger_with_keys(tmp_path) -> Path:
    """A ledger clone whose docs/keys/ holds all three public keys."""
    repo = tmp_path / "ledger"
    (repo / "docs" / "keys").mkdir(parents=True)
    for name in KEYS:
        shutil.copy(GPG_FIXTURES / f"{name}.pub.asc", repo / "docs" / "keys" / f"{name}.asc")
    return repo


def _raw_gpg(name: str, payload: Path) -> tuple[int, str]:
    """What gpg itself says, in a pristine keyring — the premise under test."""
    # Not under tmp_path: pytest's per-test directory is ~130 chars on macOS,
    # past the 104-byte AF_UNIX limit, and gpg then exits 2 on --import with
    # "can't connect to the gpg-agent: File name too long". The production
    # path already works on macOS because ledger_utils keeps its scratch
    # keyring in a short TemporaryDirectory; mirror that here.
    home = Path(tempfile.mkdtemp(prefix=f"dx-gpg-{name}-"))
    home.chmod(0o700)
    base = ["gpg", "--homedir", str(home), "--batch", "--no-tty"]
    subprocess.run(
        [*base, "--import", str(GPG_FIXTURES / f"{name}.pub.asc")],
        capture_output=True, check=True,
    )
    r = subprocess.run(
        [*base, "--status-fd", "1", "--verify",
         str(GPG_FIXTURES / f"{name}.sig.asc"), str(payload)],
        capture_output=True, text=True,
    )
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"],
                   capture_output=True, check=False)
    shutil.rmtree(home, ignore_errors=True)
    return r.returncode, r.stdout


# ---------------------------------------------------------------------------
# The premise: gpg is permissive, so dx cannot be
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,marker,notice",
    [("revoked", "REVKEYSIG", "KEYREVOKED"), ("expired", "EXPKEYSIG", "KEYEXPIRED")],
)
def test_gpg_exits_zero_for_degraded_keys(payload, name, marker, notice):
    """This is the entire reason the return code is not a sufficient gate."""
    rc, status = _raw_gpg(name, payload)
    assert rc == 0, f"expected gpg to exit 0 for a {name} key, got {rc}"
    assert "VALIDSIG" in status, "gpg still considers the signature cryptographically valid"
    assert marker in status
    assert notice in status
    assert "GOODSIG" not in status, f"gpg emits {marker} in place of GOODSIG"


def test_gpg_reports_goodsig_for_the_usable_key(payload):
    rc, status = _raw_gpg("valid", payload)
    assert rc == 0
    assert "GOODSIG" in status
    assert "VALIDSIG" in status


def test_the_old_gate_would_have_accepted_all_three(payload):
    """Pins the regression itself: `rc == 0 and VALIDSIG present` — the pre-0.3.0
    condition — cannot distinguish a usable key from a retired one.
    """
    for name in KEYS:
        rc, status = _raw_gpg(name, payload)
        assert rc == 0 and "VALIDSIG" in status, (
            f"{name} should satisfy the old, insufficient condition"
        )


# ---------------------------------------------------------------------------
# dx's verdict
# ---------------------------------------------------------------------------


def test_valid_signature_is_accepted_and_names_the_signer(payload, ledger_with_keys):
    signer = verify_detached_signature(
        GPG_FIXTURES / "valid.sig.asc", payload, ledger_with_keys
    )
    assert signer.name == "Bob Reviewer"
    assert signer.email == "bob@example.invalid"
    assert len(signer.fingerprint) == 40


@pytest.mark.parametrize("name,reason", [("revoked", "revoked"), ("expired", "expired")])
def test_degraded_keys_are_rejected(payload, ledger_with_keys, name, reason):
    with pytest.raises(LedgerError) as exc:
        verify_detached_signature(
            GPG_FIXTURES / f"{name}.sig.asc", payload, ledger_with_keys
        )
    assert reason in str(exc.value)
    assert "RL-003" in str(exc.value)


def test_tampered_payload_is_rejected(payload, ledger_with_keys, tmp_path):
    tampered = tmp_path / "tampered.bin"
    tampered.write_bytes(payload.read_bytes() + b"!")
    with pytest.raises(LedgerError):
        verify_detached_signature(GPG_FIXTURES / "valid.sig.asc", tampered, ledger_with_keys)


def test_signer_not_registered_in_docs_keys_is_rejected(payload, tmp_path):
    """A cryptographically perfect signature from an unregistered key must fail."""
    empty = tmp_path / "ledger-no-keys"
    (empty / "docs" / "keys").mkdir(parents=True)
    with pytest.raises(LedgerError) as exc:
        verify_detached_signature(GPG_FIXTURES / "valid.sig.asc", payload, empty)
    assert "not in devswarm-ledger/docs/keys" in str(exc.value)


def test_missing_docs_keys_directory_is_rejected(payload, tmp_path):
    with pytest.raises(LedgerError) as exc:
        verify_detached_signature(GPG_FIXTURES / "valid.sig.asc", payload, tmp_path)
    assert "docs/keys directory not found" in str(exc.value)


def test_verification_does_not_touch_the_users_keyring(payload, ledger_with_keys):
    """RL-010 adjacency: dx imports into a scratch homedir, never the user's."""
    user_keyring = Path.home() / ".gnupg"
    before = sorted(p.name for p in user_keyring.iterdir()) if user_keyring.is_dir() else None
    verify_detached_signature(GPG_FIXTURES / "valid.sig.asc", payload, ledger_with_keys)
    after = sorted(p.name for p in user_keyring.iterdir()) if user_keyring.is_dir() else None
    assert before == after


# ---------------------------------------------------------------------------
# The full dx merge gate, with real crypto
# ---------------------------------------------------------------------------

HEAD = "a" * 64


def _ledger_for_merge(repo: Path, which: str) -> Path:
    """Finish `ledger_with_keys` into a ledger dx merge can run against."""
    (repo / "tools").mkdir()
    (repo / "queue").mkdir()
    (repo / "approvals").mkdir()
    (repo / "tools" / "verify_chain.py").write_text(
        f"print('Ledger head hash: {HEAD}')\n", encoding="utf-8"
    )
    (repo / "ledger.jsonl").write_text(
        json.dumps({"ts": "2026-09-01T00:00:00Z", "task_id": "T-TEST",
                    "action": "ADMITTED", "author_human": "Alice Author"}) + "\n",
        encoding="utf-8",
    )
    (repo / "queue" / "T-TEST.json").write_text(
        json.dumps({"task_id": "T-TEST", "approve_role": "code_review"}), encoding="utf-8"
    )
    # The fixture payload IS task_id + head + role, byte for byte.
    shutil.copy(GPG_FIXTURES / "payload.bin", repo / "approvals" / "T-TEST.code_review.msg")
    shutil.copy(GPG_FIXTURES / f"{which}.sig.asc", repo / "approvals" / "T-TEST.code_review.asc")
    return repo


def _run_merge(*argv):
    args = build_parser().parse_args(["merge", *argv])
    args.func(args)


@pytest.fixture
def gate_only(monkeypatch):
    """Isolate the RL-003 crypto gate from the ledger append that follows it.

    `HEAD` here is baked into a committed real signature — `payload.bin` was
    signed over `T-TEST + HEAD + code_review` — so the fixture's rows cannot be
    made to hash to it, and `append_row` rightly refuses a prev_hash that does
    not match the actual last row. These tests are about whether the signature
    gate holds; appending has its own tests in `test_ledger_writer.py`.
    """
    monkeypatch.setattr("dx.cmd_merge.MergeLock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr("dx.cmd_merge.append_row", lambda *a, **k: "d" * 64)
    monkeypatch.setattr("dx.cmd_merge.read_head", lambda *a, **k: "d" * 64)


def test_merge_all_green_with_a_real_signature(ledger_with_keys, monkeypatch, capsys, gate_only):
    """The complete RL-003 gate against real crypto: current head, registered
    and currently-valid key, signer != author. This is the all-green path.
    """
    repo = _ledger_for_merge(ledger_with_keys, "valid")
    monkeypatch.setenv("DX_LEDGER_REPO", str(repo))

    with pytest.raises(SystemExit) as exc:
        _run_merge("T-TEST")

    out = capsys.readouterr().out
    assert exc.value.code == 0, out
    assert "Ledger chain verifies" in out
    assert "Bob Reviewer" in out
    assert "binds task_id + current head + role" in out
    assert "author 'Alice Author' ≠ signer 'Bob Reviewer'" in out
    assert "All RL-003 checks passed" in out


def test_a_green_merge_writes_a_merge_gate_bundle(
    ledger_with_keys, monkeypatch, tmp_path, capsys, gate_only
):
    """dx.merge_gate.v1: a passing gate leaves a receipt of what it decided."""
    repo = _ledger_for_merge(ledger_with_keys, "valid")
    monkeypatch.setenv("DX_LEDGER_REPO", str(repo))
    ev = tmp_path / "ev"

    with pytest.raises(SystemExit) as exc:
        _run_merge("T-TEST", "--evidence-dir", str(ev))
    assert exc.value.code == 0

    bundles = sorted(ev.glob("T-TEST/*/manifest.json"))
    assert bundles, "no merge_gate bundle written"
    m = json.loads(bundles[-1].read_text())
    assert m["schema"] == "dx.merge_gate.v1"
    assert m["result"]["passed"] is True
    mg = m["merge_gate"]
    assert mg["signer"]["name"] == "Bob Reviewer"
    assert mg["separation_of_duties"] is True
    assert mg["merged"] is None  # no --repo
    assert "advisory" in " ".join(m["boundary"])


def test_a_rejected_merge_still_writes_a_bundle(ledger_with_keys, monkeypatch, tmp_path):
    """A refused merge is evidence too — the reason is worth as much as a pass."""
    repo = _ledger_for_merge(ledger_with_keys, "revoked")
    monkeypatch.setenv("DX_LEDGER_REPO", str(repo))
    ev = tmp_path / "ev"

    with pytest.raises(SystemExit) as exc:
        _run_merge("T-TEST", "--evidence-dir", str(ev))
    assert exc.value.code == 1

    bundles = sorted(ev.glob("T-TEST/*/manifest.json"))
    assert bundles, "a rejected merge wrote no bundle"
    m = json.loads(bundles[-1].read_text())
    assert m["schema"] == "dx.merge_gate.v1"
    assert m["result"]["passed"] is False


def test_no_evidence_skips_the_merge_bundle(
    ledger_with_keys, monkeypatch, tmp_path, gate_only
):
    repo = _ledger_for_merge(ledger_with_keys, "valid")
    monkeypatch.setenv("DX_LEDGER_REPO", str(repo))
    ev = tmp_path / "ev"
    with pytest.raises(SystemExit) as exc:
        _run_merge("T-TEST", "--evidence-dir", str(ev), "--no-evidence")
    assert exc.value.code == 0
    assert not ev.exists()


@pytest.mark.parametrize("which,reason", [("revoked", "revoked"), ("expired", "expired")])
def test_merge_rejects_degraded_signatures_end_to_end(
    ledger_with_keys, monkeypatch, capsys, which, reason
):
    repo = _ledger_for_merge(ledger_with_keys, which)
    monkeypatch.setenv("DX_LEDGER_REPO", str(repo))

    with pytest.raises(SystemExit) as exc:
        _run_merge("T-TEST")

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert reason in captured.err
    assert "All RL-003 checks passed" not in captured.out


def test_merge_rejects_a_stale_head_with_a_real_signature(
    ledger_with_keys, monkeypatch, capsys
):
    """Real signature, valid key — but the chain moved on."""
    repo = _ledger_for_merge(ledger_with_keys, "valid")
    (repo / "tools" / "verify_chain.py").write_text(
        f"print('Ledger head hash: {'b' * 64}')\n", encoding="utf-8"
    )
    monkeypatch.setenv("DX_LEDGER_REPO", str(repo))

    with pytest.raises(SystemExit) as exc:
        _run_merge("T-TEST")
    assert exc.value.code == 1
    assert "Stale signature (RL-003)" in capsys.readouterr().err


def test_merge_rejects_author_approving_their_own_task(
    ledger_with_keys, monkeypatch, capsys
):
    """Real signature from a valid key — but the signer authored the task."""
    repo = _ledger_for_merge(ledger_with_keys, "valid")
    (repo / "ledger.jsonl").write_text(
        json.dumps({"task_id": "T-TEST", "author_human": "Bob Reviewer"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DX_LEDGER_REPO", str(repo))

    with pytest.raises(SystemExit) as exc:
        _run_merge("T-TEST")
    assert exc.value.code == 1
    assert "Separation-of-duties violation" in capsys.readouterr().err
