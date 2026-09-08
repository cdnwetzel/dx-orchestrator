"""`dx merge` — the RL-003 gate logic that dx itself owns.

GPG signature validity is covered by `classify_gpg_status` in
test_ledger_utils.py. Here the signature check is stubbed so the *dx-owned*
decisions — stale head, payload binding, separation of duties, --force — are
each exercised, including the all-green path.
"""
import pathlib
import subprocess
import sys

import pytest

from dx.cli import build_parser
from dx.ledger_utils import LedgerError, SignerIdentity

REVIEWER = SignerIdentity(fingerprint="F" * 40, uid="Bob Reviewer <bob@example.invalid>")
AUTHOR = SignerIdentity(fingerprint="A" * 40, uid="Alice Author <alice@example.invalid>")


@pytest.fixture
def merge(monkeypatch, fake_ledger):
    """Run `dx merge` against the fake ledger with a stubbed signature check."""
    monkeypatch.setenv("DX_LEDGER_REPO", str(fake_ledger))

    def _merge(*argv, signer=REVIEWER):
        monkeypatch.setattr(
            "dx.cmd_merge.verify_detached_signature",
            lambda sig, msg, repo: signer,
        )
        args = build_parser().parse_args(["merge", *argv])
        args.func(args)

    _merge.ledger = fake_ledger
    return _merge


def test_all_checks_green_reaches_the_merge_step(merge, capsys):
    """The fully-green path: current head, valid signature, signer != author."""
    with pytest.raises(SystemExit) as exc:
        merge("T-TEST")
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "Ledger chain verifies" in out
    assert "Signature verified" in out
    assert "binds task_id + current head + role" in out
    assert "Separation of duties" in out
    assert "All RL-003 checks passed" in out


def test_stale_signature_is_rejected(merge, capsys, ledger_head, stale_head):
    """RL-003: a signature made against an older head is invalid."""
    msg = merge.ledger / "approvals" / "T-TEST.code_review.msg"
    msg.write_text(f"T-TEST{stale_head}code_review", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        merge("T-TEST")
    assert exc.value.code == 1

    captured = capsys.readouterr()
    assert "Stale signature (RL-003)" in captured.err
    assert stale_head[:16] in captured.err
    assert ledger_head[:16] in captured.err
    assert "All RL-003 checks passed" not in captured.out


def test_tampered_payload_is_rejected(merge, capsys):
    msg = merge.ledger / "approvals" / "T-TEST.code_review.msg"
    msg.write_text("approve this please", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        merge("T-TEST")
    assert exc.value.code == 1
    assert "payload does not match" in capsys.readouterr().err


def test_payload_for_a_different_task_is_rejected(merge, capsys, ledger_head):
    """The signature must bind the task id, not just the head."""
    msg = merge.ledger / "approvals" / "T-TEST.code_review.msg"
    msg.write_text(f"T-OTHER{ledger_head}code_review", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        merge("T-TEST")
    assert exc.value.code == 1
    assert "payload does not match" in capsys.readouterr().err


def test_trailing_newline_in_the_payload_is_rejected(merge, capsys, ledger_head):
    """Real .msg files are a bare concatenation; a stray newline changes what
    was signed and must not be silently tolerated."""
    msg = merge.ledger / "approvals" / "T-TEST.code_review.msg"
    msg.write_text(f"T-TEST{ledger_head}code_review\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        merge("T-TEST")
    assert exc.value.code == 1


def test_signer_equal_to_author_is_rejected(merge, capsys):
    """Separation of duties: the author of a task cannot approve it."""
    with pytest.raises(SystemExit) as exc:
        merge("T-TEST", signer=AUTHOR)
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "Separation-of-duties violation" in err
    assert "Alice Author" in err


def test_signer_match_ignores_case_and_whitespace(merge, capsys):
    sloppy = SignerIdentity(fingerprint="A" * 40, uid="  alice author  <a@example.invalid>")
    with pytest.raises(SystemExit) as exc:
        merge("T-TEST", signer=sloppy)
    assert exc.value.code == 1
    assert "Separation-of-duties violation" in capsys.readouterr().err


def test_uid_without_a_comment_field_still_matches_the_author(merge, capsys):
    """Regression: SignerIdentity.name kept the <email> when the uid had no
    (comment), so the author comparison never matched and the gate failed open.
    """
    no_comment = SignerIdentity(fingerprint="A" * 40, uid="Alice Author <alice@example.invalid>")
    assert no_comment.name == "Alice Author"
    with pytest.raises(SystemExit) as exc:
        merge("T-TEST", signer=no_comment)
    assert exc.value.code == 1


def test_queue_without_approve_role_is_rejected(merge, capsys):
    with pytest.raises(SystemExit) as exc:
        merge("T-NOROLE")
    assert exc.value.code == 1
    assert "approve_role" in capsys.readouterr().err


def test_unknown_task_is_rejected(merge, capsys):
    with pytest.raises(SystemExit) as exc:
        merge("T-ABSENT")
    assert exc.value.code == 1
    assert "queue file not found" in capsys.readouterr().err


def test_missing_signature_file_is_rejected(monkeypatch, fake_ledger, capsys):
    monkeypatch.setenv("DX_LEDGER_REPO", str(fake_ledger))

    def boom(sig, msg, repo):
        raise LedgerError(f"signature file not found: {sig}")

    monkeypatch.setattr("dx.cmd_merge.verify_detached_signature", boom)
    args = build_parser().parse_args(["merge", "T-TEST"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert "signature file not found" in capsys.readouterr().err


def test_missing_ledger_repo_is_rejected(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "absent"))
    args = build_parser().parse_args(["merge", "T-TEST"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert "DX_LEDGER_REPO" in capsys.readouterr().err


class TestForce:
    def test_force_short_circuits_before_any_ledger_read(self, monkeypatch, tmp_path, capsys):
        """--force must not require a ledger clone at all."""
        monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "absent"))
        args = build_parser().parse_args(["merge", "T-TEST", "--force"])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        assert exc.value.code == 0
        assert "--force in effect" in capsys.readouterr().err

    def test_force_banner_names_the_task(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "absent"))
        args = build_parser().parse_args(["merge", "T-9999", "--force"])
        with pytest.raises(SystemExit):
            args.func(args)
        assert "T-9999" in capsys.readouterr().err

    def test_force_admits_it_also_skips_gui_verification(self, monkeypatch, tmp_path, capsys):
        """--force bypasses the GUI gate too; the banner has to say so."""
        monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "absent"))
        monkeypatch.setattr(
            "dx.cmd_merge.verify_gui",
            lambda expected: pytest.fail("GUI verification ran under --force"),
        )
        args = build_parser().parse_args(["merge", "T-TEST", "--force", "--verify-gui"])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        assert exc.value.code == 0
        assert "GUI verification" in capsys.readouterr().err


def test_gate_verdicts_appear_in_decision_order_when_piped(fake_ledger, tmp_path):
    """Regression: ❌ (stderr, unbuffered) printed before the ✅ lines (stdout,
    block-buffered off a tty), so a piped merge transcript listed the failure
    before the checks that preceded it. A gate transcript is evidence; it has to
    read in the order the decisions were made.

    Removing docs/keys/ makes the signature step fail after the chain step
    succeeds — no GPG keys needed to reproduce the ordering.
    """
    import shutil

    shutil.rmtree(fake_ledger / "docs" / "keys")

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DX_LEDGER_REPO": str(fake_ledger),
        "PYTHONPATH": str(repo_root / "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "dx.cli", "merge", "T-TEST"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    combined = proc.stdout
    assert proc.returncode == 1
    assert "✅ Ledger chain verifies" in combined
    assert "❌" in combined
    assert combined.index("✅ Ledger chain verifies") < combined.index("❌")


class TestPayloadDiagnosis:
    """Stale and tampered are different failures with different remedies.

    "Stale signature" tells the operator to re-sign against the current head.
    That is the wrong advice — and actively unsafe — if the payload has been
    altered rather than merely superseded. Every case here is correctly
    *rejected* either way; what is under test is that the diagnosis matches.
    """

    def _write(self, merge, payload: bytes):
        (merge.ledger / "approvals" / "T-TEST.code_review.msg").write_bytes(payload)

    def test_a_genuinely_stale_head_says_stale(self, merge, capsys, stale_head):
        self._write(merge, f"T-TEST{stale_head}code_review".encode())
        with pytest.raises(SystemExit) as exc:
            merge("T-TEST")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Stale signature (RL-003)" in err
        assert "Re-sign" in err

    @pytest.mark.parametrize(
        "middle,label",
        [
            (b"", "empty"),
            (b"z" * 64, "non-hex"),
            (b"a" * 10, "too short"),
            (b"a" * 100, "too long"),
            (b"A" * 64, "uppercase hex"),
        ],
    )
    def test_a_malformed_head_is_not_called_stale(self, merge, capsys, middle, label):
        self._write(merge, b"T-TEST" + middle + b"code_review")
        with pytest.raises(SystemExit) as exc:
            merge("T-TEST")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Malformed approval payload" in err, f"{label}: {err}"
        assert "Stale signature" not in err, f"{label}: misdiagnosed as stale"

    def test_the_malformed_message_warns_against_re_signing(self, merge, capsys):
        self._write(merge, b"T-TESTcode_review")
        with pytest.raises(SystemExit):
            merge("T-TEST")
        assert "Do not re-sign" in capsys.readouterr().err

    def test_the_malformed_message_names_the_file(self, merge, capsys):
        self._write(merge, b"T-TESTcode_review")
        with pytest.raises(SystemExit):
            merge("T-TEST")
        assert "T-TEST.code_review.msg" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "payload", [b"", b"T-TEST" + b"a" * 64, b"nonsense", b"code_reviewT-TEST"]
    )
    def test_unrecognisable_payloads_report_a_length_mismatch(self, merge, capsys, payload):
        self._write(merge, payload)
        with pytest.raises(SystemExit) as exc:
            merge("T-TEST")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "does not match" in err
        assert "bytes, found" in err

    def test_every_bad_payload_is_still_rejected(self, merge, capsys, ledger_head):
        """Diagnosis is cosmetic; refusing to merge is not. None of these may
        reach the merge step."""
        for payload in (
            b"",
            b"T-TESTcode_review",
            b"T-TEST" + b"z" * 64 + b"code_review",
            b"T-TEST" + b"a" * 64 + b"code_review\n",
            f"T-OTHER{ledger_head}code_review".encode(),
        ):
            self._write(merge, payload)
            with pytest.raises(SystemExit) as exc:
                merge("T-TEST")
            out = capsys.readouterr()
            assert exc.value.code == 1, payload
            assert "All RL-003 checks passed" not in out.out
