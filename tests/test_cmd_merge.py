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

from conftest import SOFTWARE_MECHANISM
from dx.cli import build_parser
from dx.ledger_utils import LedgerError, SignerIdentity

REVIEWER = SignerIdentity(fingerprint="F" * 40, uid="Bob Reviewer <bob@example.invalid>",
                          residency="software", mechanism=SOFTWARE_MECHANISM)
AUTHOR = SignerIdentity(fingerprint="A" * 40, uid="Alice Author <alice@example.invalid>",
                        residency="software", mechanism=SOFTWARE_MECHANISM)


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
    sloppy = SignerIdentity(fingerprint="A" * 40, uid="  alice author  <a@example.invalid>",
                            residency="software", mechanism=SOFTWARE_MECHANISM)
    with pytest.raises(SystemExit) as exc:
        merge("T-TEST", signer=sloppy)
    assert exc.value.code == 1
    assert "Separation-of-duties violation" in capsys.readouterr().err


def test_uid_without_a_comment_field_still_matches_the_author(merge, capsys):
    """Regression: SignerIdentity.name kept the <email> when the uid had no
    (comment), so the author comparison never matched and the gate failed open.
    """
    no_comment = SignerIdentity(fingerprint="A" * 40, uid="Alice Author <alice@example.invalid>",
                                residency="software", mechanism=SOFTWARE_MECHANISM)
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


# ---------------------------------------------------------------------------
# approval_tier — Decision 0020. The signer's rights follow the chain diff.
# ---------------------------------------------------------------------------

import json  # noqa: E402

from conftest import manifest_with_approval, write_declaration  # noqa: E402
from dx.approval_tier import SOD_EXCEPTION  # noqa: E402


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True, timeout=30).stdout.strip()


def _rows(ledger: pathlib.Path) -> list[dict]:
    return [json.loads(ln) for ln in (ledger / "ledger.jsonl").read_text().splitlines() if ln.strip()]


@pytest.fixture
def tiered(monkeypatch, tmp_path, fake_ledger):
    """A declared single-reviewer scope holding a real base and candidate,
    wired into the fake ledger's T-TEST, with the manifest's `approval:`
    section pointing at the declaration. Returns a runner that takes the
    paths the candidate should touch and the signer."""
    ai_root = tmp_path / "ai"
    scope = ai_root / "pilot"
    scope.mkdir(parents=True)
    _git(scope, "init", "-q", "-b", "main")
    (scope / "src").mkdir()
    (scope / "src" / "app.py").write_text("x = 1\n")
    _git(scope, "add", "-A")
    _git(scope, "commit", "-qm", "base")
    base = _git(scope, "rev-parse", "HEAD")
    declaration = write_declaration(ai_root / "roles.json", ["${AI_ROOT}/pilot"])
    monkeypatch.setenv("DX_CONFIG", str(manifest_with_approval(tmp_path, declaration, ai_root)))
    monkeypatch.setenv("DX_LEDGER_REPO", str(fake_ledger))

    def _run(touch: list[str], *, signer=REVIEWER, repo: bool = True, argv=()):
        for rel in touch:
            f = scope / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("changed\n")
        _git(scope, "add", "-A")
        _git(scope, "commit", "-qm", "the work")
        candidate = _git(scope, "rev-parse", "HEAD")
        # the fake ledger's chain names candidate "e"*40; rebuild it on this one
        from conftest import _canonical, _chain  # noqa: PLC0415
        rows, head = _chain([
            {"ts": "2026-09-01T00:00:00Z", "task_id": "T-TEST", "action": "ADMITTED",
             "author_human": "Alice Author", "author_seat": "S4"},
            {"ts": "2026-09-02T00:00:00Z", "task_id": "T-TEST", "action": "EXECUTED",
             "author_human": "Alice Author", "author_seat": "S4", "sha": candidate},
        ])
        (fake_ledger / "ledger.jsonl").write_text("".join(_canonical(r) + "\n" for r in rows))
        (fake_ledger / "tools" / "verify_chain.py").write_text(
            f"print('Ledger head hash: {head}')\n")
        (fake_ledger / "queue" / "T-TEST.json").write_text(json.dumps(
            {"task_id": "T-TEST", "approve_role": "code_review", "state": "EXECUTED",
             "sha": candidate, "base_sha": base, "supersedes": ""}))
        (fake_ledger / "approvals" / "T-TEST.code_review.msg").write_text(
            f"T-TEST{head}code_review")
        subprocess.run(["git", "-C", str(fake_ledger), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(fake_ledger), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "candidate"], check=True, capture_output=True)
        monkeypatch.setattr("dx.cmd_merge.verify_detached_signature", lambda s, m, r: signer)
        extra = ["--repo", str(scope)] if repo else []
        args = build_parser().parse_args(["merge", "T-TEST", *extra, *argv])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        return exc.value.code

    _run.ledger = fake_ledger
    _run.scope = scope
    return _run


def test_the_author_may_sign_on_a_single_reviewer_scope_and_it_is_recorded(tiered, capsys):
    code = tiered(["src/app.py"], signer=AUTHOR)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Approval tier: single-reviewer" in out
    assert "recorded as sod_exception=" in out
    rows = _rows(tiered.ledger)
    signed, merged = [r for r in rows if r["action"] == "SIGNED"][-1], [r for r in rows if r["action"] == "MERGED"][-1]
    for row in (signed, merged):
        assert f"sod_exception={SOD_EXCEPTION}" in row["evidence"]
        assert f"mechanism={SOFTWARE_MECHANISM}" in row["evidence"]
        assert "approval_tier=single-reviewer" in row["evidence"]
    queue = json.loads((tiered.ledger / "queue" / "T-TEST.json").read_text())
    assert queue["sod_exception"] == SOD_EXCEPTION
    assert queue["approval_tier"] == "single-reviewer"


def test_a_second_person_on_a_single_reviewer_scope_records_no_exception(tiered, capsys, tmp_path):
    ev = tmp_path / "ev"
    code = tiered(["src/app.py"], signer=REVIEWER, argv=["--evidence-dir", str(ev)])
    assert code == 0
    assert "≠ signer 'Bob Reviewer'" in capsys.readouterr().out
    signed = [r for r in _rows(tiered.ledger) if r["action"] == "SIGNED"][-1]
    assert "sod_exception=" not in signed["evidence"]
    assert f"mechanism={SOFTWARE_MECHANISM}" in signed["evidence"]
    # The bundle says what the rows say: no exception was recorded.
    manifest = json.loads(sorted(ev.glob("T-TEST/*/manifest.json"))[-1].read_text())
    tier = manifest["merge_gate"]["approval_tier"]
    assert tier["tier"] == "single-reviewer" and tier["sod_exception"] is None


def test_the_bundle_carries_the_exception_when_the_author_signed(tiered, tmp_path):
    ev = tmp_path / "ev"
    assert tiered(["src/app.py"], signer=AUTHOR, argv=["--evidence-dir", str(ev)]) == 0
    manifest = json.loads(sorted(ev.glob("T-TEST/*/manifest.json"))[-1].read_text())
    assert manifest["merge_gate"]["approval_tier"]["sod_exception"] == SOD_EXCEPTION


@pytest.mark.parametrize("path", ["tests/test_app.py", "conftest.py", "pyproject.toml",
                                  ".github/workflows/ci.yml", "Makefile", "pxx.toml"])
def test_an_exec_surface_touch_makes_the_authors_signature_a_violation(tiered, capsys, path):
    code = tiered(["src/app.py", path], signer=AUTHOR)
    captured = capsys.readouterr()
    assert code == 1
    assert "Approval tier: two-human" in captured.out
    assert path in captured.out
    assert "Separation-of-duties violation" in captured.err
    assert not [r for r in _rows(tiered.ledger) if r["action"] == "SIGNED"]


def test_an_exec_surface_touch_still_merges_with_a_second_person(tiered, capsys):
    code = tiered(["src/app.py", "tests/test_app.py"], signer=REVIEWER)
    assert code == 0
    signed = [r for r in _rows(tiered.ledger) if r["action"] == "SIGNED"][-1]
    assert "approval_tier=two-human" in signed["evidence"]
    assert "sod_exception=" not in signed["evidence"]


def test_control_plane_contact_is_redlined_and_not_signable_by_anyone(tiered, capsys):
    code = tiered(["src/app.py", "CODEOWNERS"], signer=REVIEWER)
    captured = capsys.readouterr()
    assert code == 1
    assert "not signable" in captured.err and "CODEOWNERS" in captured.err
    rows = _rows(tiered.ledger)
    assert rows[-1]["action"] == "REDLINE"
    assert "control-plane" in rows[-1]["evidence"] and "CODEOWNERS" in rows[-1]["evidence"]
    assert not [r for r in rows if r["action"] == "SIGNED"]
    # a second attempt is refused at the state check, so the row is not duplicated
    args = build_parser().parse_args(["merge", "T-TEST", "--repo", str(tiered.scope)])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert [r["action"] for r in _rows(tiered.ledger)].count("REDLINE") == 1


def test_without_a_repo_the_tier_is_two_human_even_on_a_single_reviewer_scope(tiered, capsys):
    code = tiered(["src/app.py"], signer=AUTHOR, repo=False)
    captured = capsys.readouterr()
    assert code == 1
    assert "no --repo" in captured.out
    assert "Separation-of-duties violation" in captured.err


def test_a_signer_without_a_registry_mechanism_is_refused_before_any_row(tiered, capsys):
    """An identity that did not come through the registry path carries no
    mechanism; the gate refuses to write a row it cannot mark."""
    bare = SignerIdentity(fingerprint="F" * 40, uid="Bob Reviewer <bob@example.invalid>")
    code = tiered(["src/app.py"], signer=bare)
    assert code == 1
    assert "carries no registry mechanism" in capsys.readouterr().err
    assert not [r for r in _rows(tiered.ledger) if r["action"] in ("SIGNED", "MERGED")]


def test_a_task_without_an_author_is_refused_not_waved_through(merge, capsys):
    """Until 0.20.0 a missing author_human warned and proceeded — the
    separation check passed vacuously over nobody."""
    ledger = merge.ledger
    rows = [json.loads(ln) for ln in (ledger / "ledger.jsonl").read_text().splitlines() if ln.strip()]
    from conftest import _canonical, _chain  # noqa: PLC0415
    stripped = []
    for r in rows:
        r = dict(r)
        r.pop("prev_hash", None)
        if r["task_id"] == "T-TEST":
            r["author_human"] = None
        stripped.append(r)
    chained, head = _chain(stripped)
    (ledger / "ledger.jsonl").write_text("".join(_canonical(r) + "\n" for r in chained))
    (ledger / "tools" / "verify_chain.py").write_text(f"print('Ledger head hash: {head}')\n")
    (ledger / "approvals" / "T-TEST.code_review.msg").write_text(f"T-TEST{head}code_review")
    with pytest.raises(SystemExit) as exc:
        merge("T-TEST")
    assert exc.value.code == 1
    assert "names no author_human" in capsys.readouterr().err


def test_a_malformed_declaration_refuses_the_merge(tiered, monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"pxx": {"scope": ["${AI_ROOT}/pilot"], "approval_tier_default": "anyone"}}))
    (tmp_path / "m2").mkdir()
    monkeypatch.setenv("DX_CONFIG", str(manifest_with_approval(tmp_path / "m2", bad, tmp_path / "ai")))
    code = tiered(["src/app.py"], signer=REVIEWER)
    assert code == 1
    assert "approval declaration unusable" in capsys.readouterr().err


def test_the_force_banner_names_the_tier_check(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "absent"))
    args = build_parser().parse_args(["merge", "T-X", "--force"])
    with pytest.raises(SystemExit):
        args.func(args)
    assert "approval_tier check" in capsys.readouterr().err
