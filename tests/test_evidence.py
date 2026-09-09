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
