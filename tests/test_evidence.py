"""`dx.role_task.v1` evidence bundles.

Admission record: `docs/admissions/T-1101-evidence-bundles.md`.

The two tests that carry the weight here are the ones that make the format
*earn* its adjectives. "Tamper-evident" is a claim until something demonstrates
it detecting tampering, and "boundary is mandatory" is a claim until something
demonstrates the writer refusing.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from dx.evidence import (
    DEFAULT_BOUNDARY,
    SCHEMA,
    Check,
    EvidenceError,
    RoleTaskBundle,
    write_bundle,
)


def _bundle(**kw) -> RoleTaskBundle:
    base = dict(
        task_id="T-TEST",
        title="T-TEST — backend-engineer",
        passed=True,
        source_head="a" * 40,
        role="backend-engineer",
        routing={"endpoint": "http://box.invalid:8000", "model": "m", "provider": "vllm"},
        checks={"pxx_exit_zero": Check(ok=True, path="artifacts/command.txt")},
        artifacts={"command.txt": "pxx edit\n", "changes.patch": "diff --git a/x b/x\n"},
    )
    base.update(kw)
    return RoleTaskBundle(**base)


class TestBundleShape:
    """VISION.md § Reference formats fixes this; these pin it."""

    def test_it_is_a_directory_with_the_four_required_members(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        assert out.is_dir()
        for member in ("README.md", "manifest.json", "SHA256SUMS", "artifacts"):
            assert (out / member).exists(), f"bundle is missing {member}"

    def test_the_manifest_carries_the_stable_core(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        for key in ("schema", "title", "source_head", "generated_utc", "result", "checks"):
            assert key in m, f"manifest is missing stable-core key {key!r}"
        assert m["schema"] == SCHEMA
        assert m["result"]["passed"] is True
        assert m["checks"]["pxx_exit_zero"]["ok"] is True

    def test_repeated_runs_of_one_task_accumulate(self, tmp_path):
        """An evidence store that overwrites its own history is not one."""
        a = write_bundle(_bundle(), tmp_path, now="2026-01-01T00:00:00Z")
        b = write_bundle(_bundle(), tmp_path, now="2026-01-01T00:00:01Z")
        assert a != b
        assert a.parent == b.parent and a.parent.name == "T-TEST"


class TestBoundaryIsMandatory:
    """`boundary` is what separates a receipt from a claim wearing one."""

    def test_the_default_boundary_is_present_and_non_empty(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["boundary"] == list(DEFAULT_BOUNDARY)
        assert all(line.strip() for line in m["boundary"])
        assert "does NOT prove" in (out / "README.md").read_text()

    @pytest.mark.parametrize("empty", [(), ("",), ("   ", "\t")])
    def test_the_writer_refuses_an_empty_boundary(self, tmp_path, empty):
        with pytest.raises(EvidenceError, match="boundary"):
            write_bundle(_bundle(boundary=empty), tmp_path)

    def test_nothing_is_written_when_it_refuses(self, tmp_path):
        """A refusal that still leaves a half-bundle on disk is worse than none."""
        with pytest.raises(EvidenceError):
            write_bundle(_bundle(boundary=()), tmp_path)
        assert not any(tmp_path.iterdir()), "a rejected bundle left files behind"


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not on PATH")
class TestTamperEvidence:
    """`sha256sum -c SHA256SUMS` is the verify step, so it is tested with the
    real binary rather than by re-implementing the check in Python."""

    @staticmethod
    def _verify(out):
        return subprocess.run(
            ["sha256sum", "-c", "SHA256SUMS"],
            cwd=out, capture_output=True, text=True, timeout=30,
        )

    def test_a_fresh_bundle_verifies(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        result = self._verify(out)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_every_file_except_the_sums_file_is_covered(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        listed = {
            line.split("  ", 1)[1]
            for line in (out / "SHA256SUMS").read_text().splitlines()
            if line.strip()
        }
        on_disk = {
            p.relative_to(out).as_posix()
            for p in out.rglob("*")
            if p.is_file() and p.name != "SHA256SUMS"
        }
        assert listed == on_disk, "SHA256SUMS does not cover exactly the bundle's files"

    def test_mutating_one_byte_of_one_artifact_fails_verification(self, tmp_path):
        """The acceptance criterion. A tamper-evident format that has never been
        shown detecting tampering is a claim, not a control."""
        out = write_bundle(_bundle(), tmp_path)
        assert self._verify(out).returncode == 0

        target = out / "artifacts" / "changes.patch"
        original = target.read_bytes()
        target.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))

        result = self._verify(out)
        assert result.returncode != 0, "a mutated artifact still verified"
        assert "changes.patch" in result.stdout + result.stderr

    def test_mutating_the_manifest_fails_verification(self, tmp_path):
        """The manifest is evidence too, not just an index of it."""
        out = write_bundle(_bundle(), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        m["result"]["passed"] = False        # the edit an attacker would want
        (out / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
        assert self._verify(out).returncode != 0, "a forged manifest still verified"


class TestUntrackedFilesCount:
    """Second dogfood finding, same root cause as the first: `produced_changes`
    was derived from `git diff`, which shows tracked changes only. A run whose
    whole output was one new file reported 0 changes. "Did anything happen" now
    comes from `git status`, which sees untracked paths."""

    def test_a_new_untracked_file_counts_as_a_change(self, tmp_path):
        out = write_bundle(
            _bundle(
                checks={
                    "produced_changes": Check(
                        ok=True,
                        path="artifacts/git-status.txt",
                        detail="0 tracked diff lines, 1 untracked path(s)",
                    )
                },
                artifacts={"changes.patch": "", "git-status.txt": "?? new_file.py\n"},
            ),
            tmp_path,
        )
        m = json.loads((out / "manifest.json").read_text())
        assert m["checks"]["produced_changes"]["ok"] is True
        assert "untracked" in m["checks"]["produced_changes"]["detail"]

    def test_the_boundary_says_the_patch_is_tracked_changes_only(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        boundary = " ".join(json.loads((out / "manifest.json").read_text())["boundary"])
        assert "tracked changes only" in boundary
        assert "git-status.txt" in boundary
class TestNoOpRunsAreVisible:
    """Found by dogfooding: a model reported COMPLETED over 11 rounds and 198k
    tokens without writing a file. pxx exited zero, so `result.passed` was True
    and every check was green — an empty run reading as an accomplishment.

    A no-op is not automatically a failure; some tasks legitimately change
    nothing. It must simply be *visible* in the receipt.
    """

    def test_an_empty_diff_is_recorded_as_no_changes(self, tmp_path):
        out = write_bundle(
            _bundle(
                checks={
                    "pxx_exit_zero": Check(ok=True),
                    "produced_changes": Check(ok=False, detail="0 diff lines"),
                },
                artifacts={"changes.patch": ""},
            ),
            tmp_path,
        )
        m = json.loads((out / "manifest.json").read_text())
        assert m["result"]["passed"] is True, "the task itself still succeeded"
        assert m["checks"]["produced_changes"]["ok"] is False, (
            "a run that changed nothing must say so in its own receipt"
        )

    def test_the_boundary_warns_that_passing_does_not_mean_changed(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        boundary = " ".join(json.loads((out / "manifest.json").read_text())["boundary"])
        assert "produced_changes" in boundary


# ---------------------------------------------------------------------------
# dx.gui_verification.v1
# ---------------------------------------------------------------------------

from dx.evidence import (  # noqa: E402
    GUI_DEFAULT_BOUNDARY,
    GUI_SCHEMA,
    GuiVerificationBundle,
    write_gui_bundle,
)

# a real 1x1 PNG, so the stored artifact is genuine image bytes
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da6360000002000001e221bc330000000049454e44ae426082"
)


def _gui_bundle(**kw) -> GuiVerificationBundle:
    base = dict(
        task_id="verify-gui",
        title="GUI verification — verify-gui",
        passed=True,
        expected="A window titled dx-under-test shows a temperature converter",
        vlm_answer="YES the window matches",
        vlm_model="gemma4:26b",
        vlm_endpoint="http://vlm.invalid:11434/api/generate",
        screenshot=_PNG_1x1,
        capture="ssh:tester@vlm.invalid",
        checks={"expectation_met": Check(ok=True, detail="YES")},
    )
    base.update(kw)
    return GuiVerificationBundle(**base)


class TestGuiVerificationBundle:
    def test_schema_and_family_tail(self, tmp_path):
        out = write_gui_bundle(_gui_bundle(), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["schema"] == GUI_SCHEMA
        assert m["result"]["passed"] is True
        gv = m["gui_verification"]
        assert gv["expected"].startswith("A window titled dx-under-test")
        assert gv["vlm_model"] == "gemma4:26b"
        assert gv["vlm_answer"] == "YES the window matches"
        assert gv["capture"] == "ssh:tester@vlm.invalid"
        assert gv["screenshot"] == "artifacts/screenshot.png"

    def test_the_screenshot_is_stored_verbatim(self, tmp_path):
        """The whole point of this family: a later reader can look at the exact
        bytes the model judged, not a re-capture or a description."""
        out = write_gui_bundle(_gui_bundle(), tmp_path)
        assert (out / "artifacts" / "screenshot.png").read_bytes() == _PNG_1x1

    def test_boundary_says_the_model_is_not_a_proof(self, tmp_path):
        out = write_gui_bundle(_gui_bundle(), tmp_path)
        boundary = " ".join(json.loads((out / "manifest.json").read_text())["boundary"])
        assert "advisory" in boundary and "never a" in boundary
        assert "GPG signature" in boundary  # RL-007: not a merge gate

    def test_an_empty_boundary_is_refused(self, tmp_path):
        with pytest.raises(EvidenceError):
            write_gui_bundle(_gui_bundle(boundary=()), tmp_path)

    def test_a_failed_verification_still_gets_a_bundle(self, tmp_path):
        out = write_gui_bundle(_gui_bundle(passed=False, vlm_answer="NO it is blank"), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["result"]["passed"] is False
        assert m["gui_verification"]["vlm_answer"] == "NO it is blank"

    def test_observer_provenance_is_recorded_when_present(self, tmp_path):
        prov = {
            "key_id": "dx-test-observer", "frame_hash": "a" * 64,
            "signature_verified": True, "frame_hash_matches": True,
        }
        out = write_gui_bundle(_gui_bundle(observer=prov), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["gui_verification"]["observer"] == prov
        assert "PSOperator observer" in " ".join(m["boundary"])

    def test_observer_is_null_when_absent(self, tmp_path):
        out = write_gui_bundle(_gui_bundle(), tmp_path)
        assert json.loads((out / "manifest.json").read_text())["gui_verification"]["observer"] is None

    def test_default_boundary_is_the_gui_family_one(self, tmp_path):
        out = write_gui_bundle(_gui_bundle(), tmp_path)
        assert tuple(json.loads((out / "manifest.json").read_text())["boundary"]) == GUI_DEFAULT_BOUNDARY


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not on PATH")
class TestGuiBundleTamperEvidence:
    @staticmethod
    def _verify(out) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sha256sum", "-c", "SHA256SUMS"], cwd=out, capture_output=True, text=True
        )

    def test_a_fresh_gui_bundle_verifies(self, tmp_path):
        out = write_gui_bundle(_gui_bundle(), tmp_path)
        assert self._verify(out).returncode == 0

    def test_mutating_one_byte_of_the_screenshot_fails_verification(self, tmp_path):
        out = write_gui_bundle(_gui_bundle(), tmp_path)
        shot = out / "artifacts" / "screenshot.png"
        blob = bytearray(shot.read_bytes())
        blob[-1] ^= 0x01
        shot.write_bytes(bytes(blob))
        assert self._verify(out).returncode != 0


# ---------------------------------------------------------------------------
# dx.merge_gate.v1
# ---------------------------------------------------------------------------

from dx.evidence import (  # noqa: E402
    MERGE_DEFAULT_BOUNDARY,
    MERGE_SCHEMA,
    MergeGateBundle,
    write_merge_bundle,
)


def _merge_bundle(**kw) -> MergeGateBundle:
    base = dict(
        task_id="T-0001",
        title="Merge gate — T-0001",
        passed=True,
        ledger_repo="/home/x/ledger",
        role="code_review",
        head_before="a" * 64,
        head_after="b" * 64,
        signer={"name": "Rex Reviewer", "email": "rex@example.invalid"},
        author_human="Ada Author",
        separation_of_duties=True,
        checks={"signature_verified": Check(ok=True, detail="Rex Reviewer")},
    )
    base.update(kw)
    return MergeGateBundle(**base)


class TestMergeGateBundle:
    def test_schema_and_family_tail(self, tmp_path):
        out = write_merge_bundle(_merge_bundle(), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["schema"] == MERGE_SCHEMA
        assert m["result"]["passed"] is True
        mg = m["merge_gate"]
        assert mg["role"] == "code_review"
        assert mg["signer"]["name"] == "Rex Reviewer"
        assert mg["separation_of_duties"] is True
        assert mg["merged"] is None  # no --repo

    def test_boundary_says_signature_gates_not_the_gui(self, tmp_path):
        out = write_merge_bundle(_merge_bundle(), tmp_path)
        boundary = " ".join(json.loads((out / "manifest.json").read_text())["boundary"])
        assert "advisory" in boundary and "signature is what" in boundary

    def test_a_failed_gate_gets_a_bundle_with_the_reason(self, tmp_path):
        out = write_merge_bundle(
            _merge_bundle(passed=False, separation_of_duties=False, failure="separation_of_duties"),
            tmp_path,
        )
        m = json.loads((out / "manifest.json").read_text())
        assert m["result"]["passed"] is False
        assert m["merge_gate"]["failure"] == "separation_of_duties"

    def test_a_real_merge_records_the_commit(self, tmp_path):
        out = write_merge_bundle(
            _merge_bundle(merged={"repo": "/w", "task_sha": "deadbeef", "merge_commit": "c0ffee"}),
            tmp_path,
        )
        assert json.loads((out / "manifest.json").read_text())["merge_gate"]["merged"]["merge_commit"] == "c0ffee"

    def test_empty_boundary_is_refused(self, tmp_path):
        with pytest.raises(EvidenceError):
            write_merge_bundle(_merge_bundle(boundary=()), tmp_path)

    def test_default_boundary_is_the_merge_family_one(self, tmp_path):
        out = write_merge_bundle(_merge_bundle(), tmp_path)
        assert tuple(json.loads((out / "manifest.json").read_text())["boundary"]) == MERGE_DEFAULT_BOUNDARY


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not on PATH")
def test_a_fresh_merge_bundle_verifies(tmp_path):
    out = write_merge_bundle(_merge_bundle(), tmp_path)
    r = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=out, capture_output=True, text=True)
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# dx.staged_action.v1
# ---------------------------------------------------------------------------

from dx.evidence import (  # noqa: E402
    STAGED_DEFAULT_BOUNDARY,
    STAGED_SCHEMA,
    StagedActionBundle,
    classify_bundle_risk,
    write_staged_action_bundle,
)


class TestBundleRiskClassification:
    """Per-action risk is a floor; the bundle is at least as risky, and aggregate
    reach pushes it up. Tested for the case the whole rule exists for: a pile of
    individually-trivial actions is not trivial in aggregate."""

    def test_all_t1_with_no_reach_stays_t1(self):
        cls, reasons, any_t3 = classify_bundle_risk(["T1", "T1", "T0"])
        assert cls == "T1" and reasons == [] and any_t3 is False

    def test_any_t3_hard_blocks(self):
        cls, reasons, any_t3 = classify_bundle_risk(["T0", "T3"])
        assert cls == "T3" and any_t3 is True and "action_class_T3" in reasons

    def test_cross_application_reach_is_at_least_t2(self):
        cls, reasons, _ = classify_bundle_risk(["T1"], cross_application=True)
        assert cls == "T2" and "cross_application_reach" in reasons

    def test_thirty_t1s_at_one_form_still_needs_t2(self):
        """The headline aggregate case: 30 individually-T1 actions escalate."""
        cls, reasons, _ = classify_bundle_risk(["T1"] * 30, action_count=30)
        assert cls == "T2"
        assert any(r.startswith("action_count") for r in reasons)

    def test_aggregate_diff_over_80pct_is_at_least_t2(self):
        cls, reasons, _ = classify_bundle_risk(["T1"], aggregate_diff_pct=85.0)
        assert cls == "T2" and any("aggregate_diff_cap" in r for r in reasons)

    def test_a_sensitive_target_is_at_least_t2(self):
        cls, reasons, _ = classify_bundle_risk(["T0"], sensitive_targets=1)
        assert cls == "T2" and any("sensitive_targets" in r for r in reasons)


def _staged(**kw) -> StagedActionBundle:
    per_action = kw.pop("per_action_risk", ["T0", "T1"])
    cls, reasons, _ = classify_bundle_risk(per_action, action_count=len(per_action))
    base = dict(
        stage_id="S-0001",
        title="Stage — invoice entry",
        state="STAGED",
        staged_sequence={
            "sequence_id": "abc",
            "actions": [
                {"ord": 1, "type": "focus_window", "target": {"element_id": 312}, "risk": "T0"},
                {"ord": 2, "type": "set_field", "target": {"element_id": 318}, "risk": "T1"},
            ],
        },
        per_action_risk=per_action,
        bundle_risk_class=cls,
        risk_reasons=reasons,
        frame_hash="f" * 64,
        ledger_head="a" * 64,
        trigger_rule="policy.invoice.v1#3",
        routes_to_seat="workflow-operator",
        checks={"element_ids_resolve": Check(ok=True, detail="2/2 resolved")},
    )
    base.update(kw)
    return StagedActionBundle(**base)


class TestStagedActionBundle:
    def test_schema_state_and_family_tail(self, tmp_path):
        out = write_staged_action_bundle(_staged(), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["schema"] == STAGED_SCHEMA
        sa = m["staged_action"]
        assert sa["state"] == "STAGED"
        assert sa["routes_to_seat"] == "workflow-operator"
        assert sa["bindings"]["frame_hash"] == "f" * 64
        assert sa["staged_sequence"] == "artifacts/staged_sequence.json"
        assert sa["approval"] is None

    def test_the_staged_sequence_is_stored_for_the_approver(self, tmp_path):
        out = write_staged_action_bundle(_staged(), tmp_path)
        seq = json.loads((out / "artifacts" / "staged_sequence.json").read_text())
        assert seq["actions"][1]["target"]["element_id"] == 318

    def test_a_rejected_stage_still_gets_a_bundle(self, tmp_path):
        out = write_staged_action_bundle(_staged(state="REJECTED", passed=False), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["staged_action"]["state"] == "REJECTED"
        assert m["result"]["passed"] is False

    def test_an_unknown_state_is_refused(self, tmp_path):
        with pytest.raises(EvidenceError, match="state"):
            write_staged_action_bundle(_staged(state="YOLO"), tmp_path)

    def test_an_empty_boundary_is_refused(self, tmp_path):
        with pytest.raises(EvidenceError):
            write_staged_action_bundle(_staged(boundary=()), tmp_path)

    def test_boundary_says_a_stage_is_a_proposal_not_an_act(self, tmp_path):
        out = write_staged_action_bundle(_staged(), tmp_path)
        boundary = " ".join(json.loads((out / "manifest.json").read_text())["boundary"])
        assert "not an act" in boundary or "not prove anything executed" in boundary
        assert "one world-state" in boundary  # staleness contract

    def test_an_approved_stage_records_the_signed_binding(self, tmp_path):
        approval = {
            "approval_class": "human_attested",
            "signed_message": "S-0001+bundlehash+payloadhash+framehash+head+workflow-operator",
            "signature": "c" * 40,
            "mechanism": "touch-sign token",
        }
        out = write_staged_action_bundle(_staged(state="APPROVED", approval=approval), tmp_path)
        assert json.loads((out / "manifest.json").read_text())["staged_action"]["approval"] == approval

    def test_default_boundary_is_the_staged_family_one(self, tmp_path):
        out = write_staged_action_bundle(_staged(), tmp_path)
        assert tuple(json.loads((out / "manifest.json").read_text())["boundary"]) == STAGED_DEFAULT_BOUNDARY


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not on PATH")
def test_a_fresh_staged_bundle_verifies(tmp_path):
    out = write_staged_action_bundle(_staged(), tmp_path)
    r = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=out, capture_output=True, text=True)
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# bundle_digest — the one value a ledger row carries to commit to a bundle
# ---------------------------------------------------------------------------

from dx.evidence import bundle_digest  # noqa: E402


def test_bundle_digest_is_the_sha256_of_sha256sums(tmp_path):
    out = write_bundle(_bundle(), tmp_path)
    import hashlib
    expected = hashlib.sha256((out / "SHA256SUMS").read_bytes()).hexdigest()
    assert bundle_digest(out) == expected


def test_bundle_digest_changes_if_any_artifact_is_mutated(tmp_path):
    out = write_bundle(_bundle(), tmp_path)
    before = bundle_digest(out)
    # mutate an artifact; SHA256SUMS still lists the old hash, so the digest of
    # SHA256SUMS is unchanged — but `sha256sum -c` would now fail. The digest
    # commits to SHA256SUMS, which commits to the files: rewrite the sums too.
    (out / "artifacts" / "command.txt").write_text("tampered\n")
    # regenerate SHA256SUMS the way a forger would, to prove the digest moves
    import hashlib
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    sums = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out).as_posix()}\n"
        for p in files
    )
    (out / "SHA256SUMS").write_text(sums)
    assert bundle_digest(out) != before


def test_bundle_digest_refuses_a_non_bundle(tmp_path):
    with pytest.raises(EvidenceError, match="not a bundle"):
        bundle_digest(tmp_path)
