import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Tuple

from .config_loader import get_gui_config


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
    result = subprocess.run(
        ["ssh", host, cmd], capture_output=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"SSH screenshot failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def _capture_psoperator_snapshot() -> bytes:
    snap_dir = Path("~/.psoperator/snapshots").expanduser()
    if not snap_dir.exists():
        raise RuntimeError(f"PSOperator snapshot dir not found at {snap_dir}")
    snaps = sorted(snap_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
    if not snaps:
        raise RuntimeError("no PSOperator snapshots found")
    return snaps[-1].read_bytes()


def _verify_with_vlm(
    png_data: bytes,
    expected: str,
    endpoint: str,
    model: str,
) -> Tuple[bool, str]:
    import requests

    img_b64 = base64.b64encode(png_data).decode("utf-8")
    prompt = (
        "You are a GUI verification agent.\n"
        "Look at this screenshot of a GUI.\n\n"
        f"Task expected: {expected}\n\n"
        "Does the screen show the correct result?\n"
        'Answer ONLY with "YES" or "NO" followed by a brief reason (max 20 words).'
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
    }
    try:
        resp = requests.post(endpoint, json=payload, timeout=30)
        resp.raise_for_status()
        output = (resp.json().get("response") or "").strip()
        passed = output.upper().startswith("YES")
        return (passed, output)
    except Exception as exc:
        return (False, f"VLM error: {exc}")


def _vlm_model_from_cfg(cfg: dict) -> str:
    return os.environ.get("DX_VLM_MODEL") or cfg.get("vlm_model", "qwen2.5vl:3b")


def verify_gui(expected: str) -> Tuple[bool, str]:
    cfg = get_gui_config()
    vlm_endpoint = cfg.get("vlm_endpoint", "http://orin.lab:11434/api/generate")
    vlm_model = _vlm_model_from_cfg(cfg)
    ssh_host = cfg.get("ssh_host", "operator@orin.lab")
    screenshot_cmd = cfg.get("screenshot_cmd", "import -window root -")
    try:
        png_data = _capture_ssh(ssh_host, screenshot_cmd)
    except Exception as exc:
        return (False, f"capture error: {exc}")
    return _verify_with_vlm(png_data, expected, vlm_endpoint, vlm_model)


def cmd_verify(args) -> None:
    try:
        cfg = get_gui_config()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    vlm_endpoint = cfg.get("vlm_endpoint", "http://orin.lab:11434/api/generate")
    vlm_model = _vlm_model_from_cfg(cfg)

    try:
        if args.screenshot:
            png_data = Path(args.screenshot).read_bytes()
            passed, output = _verify_with_vlm(
                png_data, args.expected, vlm_endpoint, vlm_model
            )
        elif args.use_psoperator:
            png_data = _capture_psoperator_snapshot()
            passed, output = _verify_with_vlm(
                png_data, args.expected, vlm_endpoint, vlm_model
            )
        else:
            passed, output = verify_gui(args.expected)
    except Exception as exc:
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
