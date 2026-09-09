"""Evidence bundles — `dx.role_task.v1`.

Shape is fixed by ``VISION.md § Reference formats``, drawn from surveying 111
real bundles in ``cdnwetzel/camelid``. The four decisions worth restating,
because each was learned the expensive way somewhere else:

**A bundle is a directory, not a file.** ``README.md`` for a human,
``manifest.json`` for a machine, ``SHA256SUMS`` for tamper-evidence, and the raw
artifacts beside them. Verification is ``sha256sum -c SHA256SUMS`` — no PKI, no
Python, no network. A receipt nobody can check on a bare box is not a receipt.

**Schema per family, not one shape for everything.** ``dx.role_task.v1`` here;
``dx.gui_verification.v1`` and ``dx.merge_gate.v1`` are separate families with
separate tails. The camelid archive has 40+ schema names for exactly this reason:
a universal shape ends up describing nothing precisely.

**A stable core with a family-specific tail.** ``schema``, ``title``,
``source_head``, ``generated_utc``, ``result.passed``, ``checks{}``. Everything
else belongs to the family.

**``boundary`` is not optional.** Every serious bundle states what it does *not*
prove. A bundle without one is a claim wearing a receipt's clothing, so
:func:`write_bundle` refuses to emit one and there is a test that it refuses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "dx.role_task.v1"

#: Written into every bundle. This is policy text, not a docstring: what a
#: bundle claims — and what it declines to claim — is a `compliance-privacy`
#: judgement, and that card is Anchored to a named accountable human. Changing
#: these lines is a policy change, not a refactor.
DEFAULT_BOUNDARY: tuple[str, ...] = (
    "This bundle records that a task ran and what changed. It does not assert "
    "that the generated code is correct.",
    "It is not a review and not an approval. No human has attested to this "
    "content by its existence.",
    "It does not contain the model's transcript. dx hands pxx the inherited "
    "stdout on purpose; interposing a pipe to capture it would change what pxx "
    "sees, and evidence must not perturb what it observes.",
    "A passing `result` means pxx exited zero, not that any test of the "
    "generated behaviour was written or run.",
    "`result.passed` does not mean anything changed. Check "
    "`checks.produced_changes`: a run can exit zero having written nothing.",
    "The diff is of the scope directory only, and covers tracked changes only. "
    "New files not yet added to git appear in `artifacts/git-status.txt`, not in "
    "the patch. Changes made outside the scope are not captured at all.",
)


GUI_SCHEMA = "dx.gui_verification.v1"

#: Policy text for the GUI-verification family. RL-007: a vision model's answer
#: is advisory evidence, never a gate. Same standing as DEFAULT_BOUNDARY — a
#: `compliance-privacy` judgement, so changing it is a policy change.
GUI_DEFAULT_BOUNDARY: tuple[str, ...] = (
    "This bundle records that a vision-language model was shown one screenshot "
    "and returned a YES/NO answer. That answer is advisory evidence, never a "
    "proof.",
    "`result.passed` true means the model's reply began with YES for the stated "
    "expectation. It does not mean the GUI is correct — only that one model said "
    "so, about one frame.",
    "The screenshot under `artifacts/` is the exact bytes the model was shown. "
    "The model's reasoning beyond its short reply is not recorded.",
    "This is not a merge gate. `dx merge` requires a GPG signature regardless of "
    "what this bundle says (RL-007).",
    "The frame is one moment. It attests to nothing before or after it, and to "
    "nothing off-screen or scrolled out of view.",
)


class EvidenceError(RuntimeError):
    """A bundle could not be written, or would have been misleading if it were."""


@dataclass(frozen=True)
class Check:
    """One named check and where its supporting artifact lives."""

    ok: bool
    path: str | None = None
    detail: str | None = None

    def as_json(self) -> dict[str, object]:
        out: dict[str, object] = {"ok": self.ok}
        if self.path is not None:
            out["path"] = self.path
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass
class RoleTaskBundle:
    """The inputs a `dx.role_task.v1` bundle is built from."""

    task_id: str
    title: str
    passed: bool
    source_head: str | None = None
    role: str | None = None
    routing: dict[str, str | None] = field(default_factory=dict)
    checks: dict[str, Check] = field(default_factory=dict)
    #: relative-path -> text content, written under ``artifacts/``
    artifacts: dict[str, str] = field(default_factory=dict)
    boundary: tuple[str, ...] = DEFAULT_BOUNDARY


@dataclass
class GuiVerificationBundle:
    """The inputs a ``dx.gui_verification.v1`` bundle is built from.

    The screenshot is the load-bearing artifact: the exact bytes handed to the
    model, stored so a later reader can look at what was actually judged rather
    than trust the one-line verdict.
    """

    task_id: str
    title: str
    passed: bool
    expected: str
    vlm_answer: str
    vlm_model: str
    vlm_endpoint: str
    screenshot: bytes
    screenshot_name: str = "screenshot.png"
    #: how the frame was obtained (ssh host, --screenshot <path>, psoperator)
    capture: str | None = None
    source_head: str | None = None
    checks: dict[str, Check] = field(default_factory=dict)
    boundary: tuple[str, ...] = GUI_DEFAULT_BOUNDARY


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_readme(bundle: RoleTaskBundle, generated_utc: str) -> str:
    lines = [
        f"# {bundle.title}",
        "",
        f"**Schema:** `{SCHEMA}` · **Task:** `{bundle.task_id}` · "
        f"**Generated:** {generated_utc}",
        f"**Result:** {'PASSED' if bundle.passed else 'FAILED'}",
        "",
    ]
    if bundle.role:
        lines += [f"**Role:** `{bundle.role}`", ""]
    if bundle.routing:
        lines += ["## Routing", ""]
        lines += [f"- **{k}:** `{v}`" for k, v in bundle.routing.items() if v]
        lines += [""]
    if bundle.checks:
        lines += ["## Checks", "", "| Check | Result | Artifact |", "| --- | --- | --- |"]
        for name, check in bundle.checks.items():
            mark = "✅" if check.ok else "❌"
            lines.append(f"| `{name}` | {mark} | {f'`{check.path}`' if check.path else '—'} |")
        lines += [""]
    lines += ["## Boundary — what this bundle does NOT prove", ""]
    lines += [f"- {line}" for line in bundle.boundary]
    lines += [
        "",
        "## Verify",
        "",
        "```sh",
        "sha256sum -c SHA256SUMS",
        "```",
        "",
    ]
    return "\n".join(lines)


def _check_boundary(boundary: tuple[str, ...]) -> None:
    if not boundary or not any(line.strip() for line in boundary):
        raise EvidenceError(
            "refusing to write a bundle with an empty boundary block. Every "
            "bundle must state what it does not prove; see VISION.md "
            "§ Reference formats."
        )


def _prepare_out(root: Path, task_id: str, generated_utc: str) -> Path:
    stamp = generated_utc.replace(":", "").replace("-", "")
    out = root / task_id / stamp
    try:
        (out / "artifacts").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvidenceError(f"could not create bundle directory {out}: {exc}") from exc
    return out


def _finalize(
    out: Path,
    manifest: dict[str, object],
    readme: str,
    text_artifacts: dict[str, str],
    binary_artifacts: dict[str, bytes],
) -> Path:
    """Write artifacts, manifest.json, README.md and SHA256SUMS into ``out``."""
    try:
        for name, content in text_artifacts.items():
            target = out / "artifacts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for name, blob in binary_artifacts.items():
            target = out / "artifacts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)

        (out / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (out / "README.md").write_text(readme, encoding="utf-8")

        # SHA256SUMS covers every file in the bundle except itself, in the exact
        # `<hex>  <path>` form GNU coreutils expects, with paths relative to the
        # bundle root so `sha256sum -c` works from inside the directory.
        files = sorted(
            p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS"
        )
        sums = "".join(f"{_sha256(p)}  {p.relative_to(out).as_posix()}\n" for p in files)
        (out / "SHA256SUMS").write_text(sums, encoding="utf-8")
    except OSError as exc:
        raise EvidenceError(f"could not write bundle at {out}: {exc}") from exc

    return out


def write_bundle(bundle: RoleTaskBundle, root: Path, *, now: str | None = None) -> Path:
    """Write ``bundle`` under ``root`` and return the bundle directory.

    ``root`` is the evidence root; the bundle lands at
    ``<root>/<task_id>/<timestamp>/`` so repeated runs of one task accumulate
    rather than overwrite — an evidence store that silently replaces its own
    history is not an evidence store.
    """
    _check_boundary(bundle.boundary)
    generated_utc = now or _utc_now()
    out = _prepare_out(root, bundle.task_id, generated_utc)
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "title": bundle.title,
        "task_id": bundle.task_id,
        "source_head": bundle.source_head,
        "generated_utc": generated_utc,
        "result": {"passed": bundle.passed},
        "role": bundle.role,
        "routing": bundle.routing,
        "checks": {k: v.as_json() for k, v in bundle.checks.items()},
        "boundary": list(bundle.boundary),
    }
    return _finalize(out, manifest, _render_readme(bundle, generated_utc), bundle.artifacts, {})


def _render_gui_readme(bundle: GuiVerificationBundle, generated_utc: str) -> str:
    lines = [
        f"# {bundle.title}",
        "",
        f"**Schema:** `{GUI_SCHEMA}` · **Task:** `{bundle.task_id}` · "
        f"**Generated:** {generated_utc}",
        f"**Result:** {'PASSED' if bundle.passed else 'FAILED'}",
        "",
        "## GUI verification",
        "",
        f"- **Expected:** {bundle.expected}",
        f"- **VLM answer:** {bundle.vlm_answer}",
        f"- **VLM model:** `{bundle.vlm_model}`",
        f"- **VLM endpoint:** `{bundle.vlm_endpoint}`",
    ]
    if bundle.capture:
        lines.append(f"- **Capture:** `{bundle.capture}`")
    lines += [f"- **Screenshot:** `artifacts/{bundle.screenshot_name}`", ""]
    if bundle.checks:
        lines += ["## Checks", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
        for name, check in bundle.checks.items():
            mark = "✅" if check.ok else "❌"
            lines.append(f"| `{name}` | {mark} | {check.detail or '—'} |")
        lines += [""]
    lines += ["## Boundary — what this bundle does NOT prove", ""]
    lines += [f"- {line}" for line in bundle.boundary]
    lines += ["", "## Verify", "", "```sh", "sha256sum -c SHA256SUMS", "```", ""]
    return "\n".join(lines)


def write_gui_bundle(
    bundle: GuiVerificationBundle, root: Path, *, now: str | None = None
) -> Path:
    """Write a ``dx.gui_verification.v1`` bundle and return its directory.

    Same core as :func:`write_bundle` — directory layout, SHA256SUMS,
    mandatory boundary — with a family-specific tail (`gui_verification`) and
    the screenshot stored as a binary artifact, since the whole point is that a
    later reader can look at what the model was actually shown.
    """
    _check_boundary(bundle.boundary)
    generated_utc = now or _utc_now()
    out = _prepare_out(root, bundle.task_id, generated_utc)
    manifest: dict[str, object] = {
        "schema": GUI_SCHEMA,
        "title": bundle.title,
        "task_id": bundle.task_id,
        "source_head": bundle.source_head,
        "generated_utc": generated_utc,
        "result": {"passed": bundle.passed},
        "checks": {k: v.as_json() for k, v in bundle.checks.items()},
        "gui_verification": {
            "expected": bundle.expected,
            "vlm_answer": bundle.vlm_answer,
            "vlm_model": bundle.vlm_model,
            "vlm_endpoint": bundle.vlm_endpoint,
            "capture": bundle.capture,
            "screenshot": f"artifacts/{bundle.screenshot_name}",
        },
        "boundary": list(bundle.boundary),
    }
    return _finalize(
        out,
        manifest,
        _render_gui_readme(bundle, generated_utc),
        {},
        {bundle.screenshot_name: bundle.screenshot},
    )
