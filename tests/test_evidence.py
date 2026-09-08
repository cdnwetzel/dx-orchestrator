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
