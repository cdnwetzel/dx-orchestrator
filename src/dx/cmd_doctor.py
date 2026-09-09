from __future__ import annotations

import argparse
import importlib
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from ._argtypes import SubParsers
from .config_loader import (
    ConfigError,
    endpoint_warnings,
    get_config_path,
    get_gui_config,
    get_ledger_repo_path,
    get_psoperator_repo,
    get_roles_path,
    load_config,
    validate_manifest,
)
from .role_registry import get_parse_failures, load_registry

# doctor asks tools for their version; none of them should take longer.
PROBE_TIMEOUT_S = 15


def _observer_health(host: str, port: int) -> str:
    """One-line health summary from a running PSOperator observer, or raise.

    Split out so `dx doctor` can be tested without psoperator installed — the
    psoperator import lives here, behind the seam the test patches.
    """
    from psoperator.services.observer_client import ObserverClient

    health = ObserverClient(host, port).health()
    return f"epoch {health.observer_epoch[:8]}…, key {health.attestation_key_id}"


def _find_on_path(name: str) -> str | None:
    """Look for `name` first alongside the current Python (venv/bin), then PATH."""
    venv_candidate = Path(sys.executable).parent / name
    if venv_candidate.exists():
        return str(venv_candidate)
    return shutil.which(name)


def register_doctor_subcommand(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "doctor", help="Self-test the dx environment"
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip network reachability probes",
    )
    parser.set_defaults(func=cmd_doctor)


def _check_cmd(cmd: list[str], label: str) -> bool:
    resolved = _find_on_path(cmd[0])
    if resolved is None:
        print(f"❌ {label}")
        return False
    try:
        subprocess.run(
            [resolved, *cmd[1:]], check=True, capture_output=True, timeout=PROBE_TIMEOUT_S
        )
        print(f"✅ {label} ({resolved})")
        return True
    except subprocess.CalledProcessError:
        print(f"❌ {label} (found at {resolved} but returned non-zero)")
        return False
    except subprocess.TimeoutExpired:
        print(f"❌ {label} (found at {resolved} but hung for {PROBE_TIMEOUT_S}s)")
        return False


def _check_import(module: str, label: str) -> bool:
    try:
        importlib.import_module(module)
        print(f"✅ {label}")
        return True
    except ImportError:
        print(f"❌ {label}")
        return False


def cmd_doctor(args: argparse.Namespace) -> None:
    print("🔍 dx doctor — self-test\n")
    all_ok = True

    # 1. Python version
    #
    # ruff flags this as unreachable given requires-python >= 3.11, but doctor
    # is exactly the command someone runs from a source checkout with the wrong
    # interpreter (`python3.9 -m dx.cli doctor`), where the package metadata was
    # never consulted. Reporting the version is the check, so keep the branch.
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info >= (3, 11):  # noqa: UP036
        print(f"✅ Python {py_ver} (>= 3.11)")
    else:
        print(f"❌ Python {py_ver} (need >= 3.11)")
        all_ok = False

    # 2. pxx CLI
    if not _check_cmd(["pxx", "--version"], "pxx installed"):
        all_ok = False

    # 3. PSOperator importable (indicates pip install -e ~/ai/psoperator ran)
    if not _check_import("psoperator", "PSOperator importable"):
        print(
            "   hint: clone and install with "
            "`gh repo clone cdnwetzel/psoperator ~/ai/psoperator "
            "&& pip install -e ~/ai/psoperator`"
        )
        all_ok = False

    # 3b. PSOperator examples/run_agent.py script present
    psop_agent = get_psoperator_repo() / "examples" / "run_agent.py"
    if psop_agent.exists():
        print(f"✅ PSOperator run_agent script at {psop_agent}")
    else:
        print(f"❌ PSOperator run_agent script missing at {psop_agent}")
        print("   hint: set PSOPERATOR_REPO or run scripts/setup_dependencies.sh")
        all_ok = False

    # 4. Role cards
    roles_path = get_roles_path()
    if roles_path.exists():
        count = len(list(roles_path.glob("*.md")))
        # Parse them, don't just count files. A card that cannot be parsed is
        # dropped from the registry, which for an Anchored role silently removes
        # its hard-block from dx run.
        load_registry(roles_path, force=True)
        parse_failures = get_parse_failures()
        if parse_failures:
            print(f"❌ Role cards at {roles_path}: {len(parse_failures)} of {count} failed to parse")
            for filename, reason in sorted(parse_failures.items()):
                print(f"   - {filename}: {reason}")
            print("   hint: dx roles validate")
            all_ok = False
        elif count == 0:
            # An install with no constitution is not a working install, and
            # dx roles validate already treats this as a failure.
            print(f"❌ No role cards at {roles_path}")
            print("   hint: set DX_ROLES_PATH or run scripts/setup_dependencies.sh")
            all_ok = False
        else:
            print(f"✅ Role cards parse cleanly ({count} files at {roles_path})")
    else:
        print(f"❌ Role cards missing at {roles_path}")
        print("   hint: set DX_ROLES_PATH or run scripts/setup_dependencies.sh")
        all_ok = False

    # 4b. devswarm-ledger clone (needed by dx merge)
    ledger_repo = get_ledger_repo_path()
    verify_chain = ledger_repo / "tools" / "verify_chain.py"
    if verify_chain.exists():
        print(f"✅ devswarm-ledger at {ledger_repo}")
    else:
        print(f"⚠️  devswarm-ledger not found at {ledger_repo} (needed for dx merge)")
        # Not marking as failure — dx run and dx roles still work without it.

    # 4c. gpg binary (needed by dx merge)
    if not _check_cmd(["gpg", "--version"], "gpg installed"):
        print("   hint: apt install gnupg (needed for dx merge signature check)")
        # Not marking all_ok=False — same reason as above.

    # 5. Hardware manifest
    #
    # Checked by loading it exactly the way dx run and dx verify-gui do, not by
    # parsing the YAML and calling it a day. A manifest can be syntactically
    # perfect and still be the wrong shape — `roles:` written as a list, say —
    # and doctor used to report that install as healthy right up until the
    # first `dx run` failed. A green doctor has to mean the config is usable.
    cfg_path = get_config_path()
    if cfg_path.exists():
        print(f"✅ Hardware manifest at {cfg_path}")
        try:
            validate_manifest()
            print("   (parses, and every section has the expected shape)")
            for warning in endpoint_warnings():
                # Not fatal — the endpoint may be unusual on purpose — but this
                # is the mistake that actually gets made, so say it loudly.
                print(f"   ⚠️  {warning}")
        except ConfigError as exc:
            print(f"   ❌ {exc}")
            all_ok = False
    else:
        print(f"❌ Hardware manifest missing at {cfg_path}")
        all_ok = False

    # 6. Network probes (optional) — derived from the manifest so they stay
    #    in sync with routing config. We only test TCP reachability (a live
    #    vLLM will happily 404 on /, which shouldn't count as failure).
    if not args.no_network:
        print("\n🌐 Network checks (non-critical):")
        try:
            cfg = load_config()
            targets: dict[str, str] = {}
            for slug, role in (cfg.get("roles") or {}).items():
                ep = (role or {}).get("endpoint")
                if ep:
                    targets[f"role:{slug}"] = ep
            gui = get_gui_config()
            if gui.get("vlm_endpoint"):
                targets["gui.vlm_endpoint"] = gui["vlm_endpoint"]

            seen: set[str] = set()
            for label, url in targets.items():
                parsed = urlparse(url)
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                key = f"{host}:{port}"
                if key in seen:
                    continue
                seen.add(key)
                try:
                    with socket.create_connection((host, port), timeout=2):
                        print(f"✅ {label} → {key} reachable")
                except OSError:
                    print(f"⚠️  {label} → {key} not reachable")
        except Exception as exc:
            print(f"⚠️  could not derive probes from manifest: {exc}")

        # Observer health (non-critical). Probed only when the observer is
        # configured for use — the attestation key path is the intent signal —
        # so an install that never uses `--observer` sees no warning about it.
        obs_key = os.environ.get("PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH")
        if obs_key:
            obs_host = os.environ.get("PSOPERATOR_OBSERVER_HOST", "127.0.0.1")
            obs_port = int(os.environ.get("PSOPERATOR_OBSERVER_PORT", "8764"))
            try:
                summary = _observer_health(obs_host, obs_port)
                print(f"✅ observer → {obs_host}:{obs_port} healthy ({summary})")
            except Exception as exc:
                print(f"⚠️  observer → {obs_host}:{obs_port} not healthy: {exc}")

    print("")
    if all_ok:
        print("✅ All core checks passed. dx is ready to use.")
        sys.exit(0)
    else:
        print("❌ Some core checks failed. See messages above.")
        sys.exit(1)
