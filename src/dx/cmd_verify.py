"""dx verify-gui — capture a screen and check it with a vision-language model.

There are deliberately no host defaults in this module. An unconfigured
`gui_verification` section is an error, not a reason to fall back to some
other machine: silently SSH-ing to a hardcoded host is both a privacy leak
and a lie about what was verified.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._argtypes import SubParsers
from .config_loader import get_config_path, get_gui_config
from .evidence import (
    Check,
    EvidenceError,
    GuiVerificationBundle,
    write_gui_bundle,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Kept in sync with cmd_run.DEFAULT_EVIDENCE_ROOT — receipts live outside any
#: repo under edit. Duplicated (three trivial lines) rather than imported so the
#: two command modules stay independent.
DEFAULT_EVIDENCE_ROOT = Path("~/.local/state/dx/evidence").expanduser()


def _evidence_root(args: argparse.Namespace) -> Path:
    if getattr(args, "evidence_dir", None):
        return Path(args.evidence_dir).expanduser()
    env = os.environ.get("DX_EVIDENCE_DIR")
    return Path(env).expanduser() if env else DEFAULT_EVIDENCE_ROOT


class GuiConfigError(RuntimeError):
    """The manifest's gui_verification section is missing or incomplete."""


DEFAULT_VLM_TIMEOUT_S = 30


@dataclass(frozen=True)
class GuiTarget:
    vlm_endpoint: str
    vlm_model: str
    ssh_host: str | None
    screenshot_cmd: str
    vlm_timeout_s: int = DEFAULT_VLM_TIMEOUT_S


def _resolve_vlm_timeout(cfg: dict[str, Any]) -> int:
    """Seconds to wait on the VLM. DX_VLM_TIMEOUT overrides the manifest's
    gui_verification.timeout_s, which overrides the 30 s default. A large local
    vision model on a cold load can take well over 30 s to answer, so a slow box
    can raise this rather than see every check fail with a read timeout."""
    raw = os.environ.get("DX_VLM_TIMEOUT")
    source = "$DX_VLM_TIMEOUT"
    if raw is None:
        val = cfg.get("timeout_s")
        if val is None:
            return DEFAULT_VLM_TIMEOUT_S
        raw = str(val)
        source = "gui_verification.timeout_s"
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise GuiConfigError(f"{source} must be a positive integer, got {raw!r}") from None
    if seconds <= 0:
        raise GuiConfigError(f"{source} must be a positive integer, got {seconds}")
    return seconds


def _require(cfg: dict[str, Any], key: str, env_var: str) -> str:
    value = os.environ.get(env_var) or cfg.get(key)
    if not value:
        raise GuiConfigError(
            f"gui_verification.{key} is not set in {get_config_path()} "
            f"(or ${env_var}). GUI verification has no default host."
        )
    return str(value)


def gui_target(require_ssh: bool = False) -> GuiTarget:
    cfg = get_gui_config()
    ssh_host = os.environ.get("DX_GUI_SSH_HOST") or cfg.get("ssh_host")
    if require_ssh and not ssh_host:
        raise GuiConfigError(
            f"gui_verification.ssh_host is not set in {get_config_path()} "
            "(or $DX_GUI_SSH_HOST). Pass --screenshot or --use-psoperator to "
            "verify without SSH capture."
        )
    return GuiTarget(
        vlm_endpoint=_require(cfg, "vlm_endpoint", "DX_VLM_ENDPOINT"),
        vlm_model=_require(cfg, "vlm_model", "DX_VLM_MODEL"),
        vlm_timeout_s=_resolve_vlm_timeout(cfg),
        ssh_host=str(ssh_host) if ssh_host else None,
        # png:- is load-bearing: with a bare "-" ImageMagick writes PostScript to
        # stdout, and the VLM would be handed a PS document labelled as an image.
        screenshot_cmd=str(cfg.get("screenshot_cmd") or "import -window root png:-"),
    )


def register_verify_subcommand(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "verify-gui", help="Verify GUI state using a vision-language model"
    )
    parser.add_argument(
        "--expected",
        default="The GUI shows the correct result.",
        help="What the screen should show",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument(
        "--screenshot",
        type=str,
        help="Use a local screenshot file instead of SSH capture",
    )
    parser.add_argument(
        "--use-psoperator",
        action="store_true",
        help="Use the latest PSOperator observer snapshot",
    )
    parser.add_argument(
        "--task",
        default="verify-gui",
        help="Task id the evidence bundle is filed under (default: verify-gui)",
    )
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="Skip the dx.gui_verification.v1 bundle (the check still runs)",
    )
    parser.add_argument(
        "--evidence-dir",
        help="Where to write the bundle (default: DX_EVIDENCE_DIR, "
        "else ~/.local/state/dx/evidence)",
    )
    parser.set_defaults(func=cmd_verify)


def _capture_ssh(host: str, cmd: str) -> bytes:
    result = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"SSH screenshot failed: {result.stderr.decode(errors='replace').strip()}"
        )
    if not result.stdout:
        raise RuntimeError(f"SSH screenshot from {host} returned no image data")
    if not result.stdout.startswith(PNG_MAGIC):
        # ImageMagick's `import ... -` writes PostScript to stdout, and
        # `screencapture` on macOS refuses stdout entirely. Both were silently
        # forwarded to the VLM as an "image" before this check existed.
        head = result.stdout[:8]
        raise RuntimeError(
            f"SSH screenshot from {host} is not a PNG (starts with {head!r}); "
            f"screenshot_cmd must write PNG to stdout — with ImageMagick use "
            f"`import -window root png:-`"
        )
    return result.stdout


def _capture_psoperator_snapshot() -> bytes:
    snap_dir = Path(
        os.environ.get("PSOPERATOR_SNAPSHOT_DIR", "~/.psoperator/snapshots")
    ).expanduser()
    if not snap_dir.exists():
        raise RuntimeError(f"PSOperator snapshot dir not found at {snap_dir}")
    snaps = sorted(snap_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
    if not snaps:
        raise RuntimeError(f"no PSOperator snapshots found in {snap_dir}")
    return snaps[-1].read_bytes()


def _verify_with_vlm(
    png_data: bytes,
    expected: str,
    endpoint: str,
    model: str,
    timeout_s: int = DEFAULT_VLM_TIMEOUT_S,
) -> tuple[bool, str]:
    import requests

    img_b64 = base64.b64encode(png_data).decode("utf-8")
    prompt = (
        "You are a GUI verification agent.\n"
        "Look at this screenshot of a GUI.\n\n"
        f"Task expected: {expected}\n\n"
        "Does the screen show the correct result?\n"
        'Answer ONLY with "YES" or "NO" followed by a brief reason (max 20 words).'
    )
    payload = {"model": model, "prompt": prompt, "images": [img_b64], "stream": False}
    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        output = (resp.json().get("response") or "").strip()
        # RL-007: this is advisory evidence, never a gate on its own. dx merge
        # requires a GPG signature regardless of what the model says here.
        passed = output.upper().startswith("YES")
        return (passed, output)
    except Exception as exc:
        return (False, f"VLM error: {exc}")


def verify_gui(expected: str) -> tuple[bool, str]:
    """Capture over SSH and verify. Returns (passed, detail); never raises."""
    try:
        target = gui_target(require_ssh=True)
    except (GuiConfigError, FileNotFoundError) as exc:
        return (False, f"config error: {exc}")
    try:
        png_data = _capture_ssh(target.ssh_host or "", target.screenshot_cmd)
    except Exception as exc:
        return (False, f"capture error: {exc}")
    return _verify_with_vlm(
        png_data, expected, target.vlm_endpoint, target.vlm_model, target.vlm_timeout_s
    )


def _obtain_frame(args: argparse.Namespace) -> tuple[GuiTarget, bytes, str]:
    """Resolve the target and the exact bytes to verify, plus how they were got.

    Captured explicitly (rather than inside ``verify_gui``) so the same bytes
    handed to the model can be stored in the evidence bundle — the receipt shows
    what was actually judged, not a re-capture.
    """
    if args.screenshot:
        target = gui_target()
        png = Path(args.screenshot).expanduser().read_bytes()
        return target, png, f"--screenshot {args.screenshot}"
    if args.use_psoperator:
        target = gui_target()
        png = _capture_psoperator_snapshot()
        return target, png, "psoperator-observer-snapshot"
    target = gui_target(require_ssh=True)
    png = _capture_ssh(target.ssh_host or "", target.screenshot_cmd)
    return target, png, f"ssh:{target.ssh_host}"


def _emit_gui_evidence(
    args: argparse.Namespace,
    target: GuiTarget,
    png: bytes,
    capture: str,
    passed: bool,
    output: str,
) -> Path:
    answered = not output.startswith("VLM error")
    checks = {
        "frame_captured": Check(ok=True, path="artifacts/screenshot.png", detail=capture),
        "vlm_answered": Check(ok=answered, detail=target.vlm_model),
        "expectation_met": Check(ok=passed, detail=output[:200]),
    }
    bundle = GuiVerificationBundle(
        task_id=args.task,
        title=f"GUI verification — {args.task}",
        passed=passed,
        expected=args.expected,
        vlm_answer=output,
        vlm_model=target.vlm_model,
        vlm_endpoint=target.vlm_endpoint,
        screenshot=png,
        capture=capture,
        checks=checks,
    )
    return write_gui_bundle(bundle, _evidence_root(args))


def cmd_verify(args: argparse.Namespace) -> None:
    try:
        target, png_data, capture = _obtain_frame(args)
    except (GuiConfigError, FileNotFoundError, OSError, RuntimeError) as exc:
        # No frame means nothing to attest to, so no bundle is written.
        if args.json:
            print(json.dumps({"passed": False, "error": str(exc)}))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    passed, output = _verify_with_vlm(
        png_data, args.expected, target.vlm_endpoint, target.vlm_model, target.vlm_timeout_s
    )

    bundle_path: Path | None = None
    if not args.no_evidence:
        try:
            bundle_path = _emit_gui_evidence(args, target, png_data, capture, passed, output)
        except EvidenceError as exc:
            # Same stance as dx run: a receipted verification that produced no
            # receipt is not one. Fail closed rather than report a clean pass.
            msg = f"evidence bundle could not be written: {exc}"
            if args.json:
                print(json.dumps({"passed": False, "error": msg}))
            else:
                print(f"ERROR: {msg}", file=sys.stderr)
            sys.exit(1)

    if args.json:
        payload: dict[str, object] = {"passed": passed, "output": output}
        if bundle_path is not None:
            payload["evidence"] = str(bundle_path)
        print(json.dumps(payload))
    else:
        marker = "✅" if passed else "❌"
        print(f"{marker} GUI verification: {output}")
        if bundle_path is not None:
            print(f"🧾 Evidence: {bundle_path}")

    sys.exit(0 if passed else 1)
