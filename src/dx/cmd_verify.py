"""dx verify-gui — capture a screen and check it with a vision-language model.

There are deliberately no host defaults in this module. An unconfigured
`gui_verification` section is an error, not a reason to fall back to some
other machine: silently SSH-ing to a hardcoded host is both a privacy leak
and a lie about what was verified.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config_loader import get_config_path, get_gui_config


class GuiConfigError(RuntimeError):
    """The manifest's gui_verification section is missing or incomplete."""


@dataclass(frozen=True)
class GuiTarget:
    vlm_endpoint: str
    vlm_model: str
    ssh_host: str | None
    screenshot_cmd: str


def _require(cfg: dict, key: str, env_var: str) -> str:
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
        ssh_host=str(ssh_host) if ssh_host else None,
        screenshot_cmd=str(cfg.get("screenshot_cmd") or "import -window root -"),
    )


def register_verify_subcommand(subparsers) -> None:
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
    parser.set_defaults(func=cmd_verify)


def _capture_ssh(host: str, cmd: str) -> bytes:
    result = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"SSH screenshot failed: {result.stderr.decode(errors='replace').strip()}"
        )
    if not result.stdout:
        raise RuntimeError(f"SSH screenshot from {host} returned no image data")
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
    png_data: bytes, expected: str, endpoint: str, model: str
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
        resp = requests.post(endpoint, json=payload, timeout=30)
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
        png_data, expected, target.vlm_endpoint, target.vlm_model
    )


def cmd_verify(args) -> None:
    try:
        if args.screenshot:
            target = gui_target()
            png_data = Path(args.screenshot).expanduser().read_bytes()
            passed, output = _verify_with_vlm(
                png_data, args.expected, target.vlm_endpoint, target.vlm_model
            )
        elif args.use_psoperator:
            target = gui_target()
            png_data = _capture_psoperator_snapshot()
            passed, output = _verify_with_vlm(
                png_data, args.expected, target.vlm_endpoint, target.vlm_model
            )
        else:
            passed, output = verify_gui(args.expected)
    except (GuiConfigError, FileNotFoundError, OSError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"passed": False, "error": str(exc)}))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"passed": passed, "output": output}))
    else:
        marker = "✅" if passed else "❌"
        print(f"{marker} GUI verification: {output}")

    sys.exit(0 if passed else 1)
