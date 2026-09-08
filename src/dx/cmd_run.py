from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ._argtypes import SubParsers
from .config_loader import get_roles_path, get_route_for_role
from .psoperator_client import PSOperatorClient
from .role_models import FitLevel
from .role_registry import get_role, load_registry


def _resolve_pxx() -> str | None:
    """Prefer pxx alongside sys.executable (same venv), fall back to PATH."""
    venv_candidate = Path(sys.executable).parent / "pxx"
    if venv_candidate.exists():
        return str(venv_candidate)
    return shutil.which("pxx")

def register_run_subcommand(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "run", help="Dispatch a task with role injection and hardware routing"
    )
    parser.add_argument("task_id", help="Task ID (e.g., T-001)")
    parser.add_argument(
        "--required_role", default="backend-engineer", help="Role slug"
    )
    parser.add_argument("--scope", default=".", help="Path scope for edits")
    parser.add_argument("--message", "-m", required=True, help="Instruction")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="After code generation, launch GUI via PSOperator",
    )
    parser.add_argument(
        "--real-input",
        action="store_true",
        help="Allow real mouse/keyboard (PSOperator opt-in)",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Do not auto-commit pxx changes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pxx command without executing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass the Anchored-role hard-block. Use only when you understand "
            "you are stepping past a separation-of-duties invariant; this is "
            "audit-visible."
        ),
    )
    parser.set_defaults(func=cmd_run)


def cmd_run(args: argparse.Namespace) -> None:
    roles_path = get_roles_path()
    if not roles_path.exists():
        print(f"ERROR: role cards not found at {roles_path}", file=sys.stderr)
        print("Set DX_ROLES_PATH or run scripts/setup_dependencies.sh", file=sys.stderr)
        sys.exit(1)

    load_registry(roles_path)
    card = get_role(args.required_role)
    if not card:
        print(f"ERROR: role '{args.required_role}' not found.", file=sys.stderr)
        print("Run: dx roles list", file=sys.stderr)
        sys.exit(1)

    # Anchored roles require a named accountable human. dx run refuses to
    # execute autonomously; --force is the audit-visible escape hatch.
    if card.fit == FitLevel.ANCHORED and args.force:
        # --force claims to be audit-visible, so it has to actually say
        # something. Matches the banner dx merge --force prints.
        print(
            f"⚠️  --force in effect: running Anchored role '{card.slug}' "
            f"(seat: {card.seat}) autonomously, past a separation-of-duties "
            f"invariant.",
            file=sys.stderr,
            flush=True,
        )

    if card.fit == FitLevel.ANCHORED and not args.force:
        print(
            f"🔒 Role '{card.slug}' is Anchored (seat: {card.seat}).",
            file=sys.stderr,
        )
        print(
            "   A named accountable human must issue this decision. "
            "dx will not run it autonomously.",
            file=sys.stderr,
        )
        if card.handoff:
            print("\n   Handoff instructions from the card:", file=sys.stderr)
            for line in card.handoff.splitlines():
                print(f"   {line}", file=sys.stderr)
        print(
            "\n   To proceed anyway (audit-visible), re-run with --force.",
            file=sys.stderr,
        )
        sys.exit(2)

    enhanced_prompt = (
        f"[ROLE: {card.slug} (Fit: {card.fit.value}, Seat: {card.seat})]\n"
        f"MANDATE:\n{card.mandate}\n\n"
        f"MUST NOT (Separation of Duties):\n{card.must_not}\n\n"
        f"USER INSTRUCTION:\n{args.message}\n"
    )

    try:
        route = get_route_for_role(args.required_role)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    env = os.environ.copy()
    env["PXX_BASE_URL"] = route.endpoint
    if route.model:
        env["PXX_MODEL"] = route.model
    if route.provider:
        env["PXX_PROVIDER"] = route.provider

    pxx_bin = _resolve_pxx()
    if pxx_bin is None:
        print(
            "ERROR: pxx not found. Install with `pip install pxx-orchestrator` "
            "in this venv.",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = [pxx_bin, "edit", "--scope", args.scope, "--message", enhanced_prompt]
    if not args.no_commit:
        cmd.append("--commit")

    if args.dry_run:
        print("DRY RUN")
        print(f"  task_id:       {args.task_id}")
        print(f"  required_role: {args.required_role}")
        print(f"  fit:           {card.fit.value}")
        print(f"  PXX_BASE_URL:  {route.endpoint}")
        print(f"  PXX_MODEL:     {route.model or '(unset, pxx default)'}")
        print(f"  PXX_PROVIDER:  {route.provider or '(unset, pxx default: ollama)'}")
        print(f"  pxx:           {pxx_bin}")
        print(f"  command:       {pxx_bin} edit --scope {args.scope} [--commit] <prompt>")
        return

    print(
        f"🚀 Running task {args.task_id} with role {args.required_role} "
        f"on {route.endpoint} (model: {route.model or 'default'})..."
    )
    # unbounded: this is the model doing the work. A large refactor on a slow
    # local endpoint legitimately runs for minutes, and cutting it off at an
    # arbitrary deadline would destroy in-flight edits. Ctrl-C is the control.
    result = subprocess.run(cmd, env=env)

    if result.returncode != 0:
        print("❌ pxx task failed.", file=sys.stderr)
        sys.exit(result.returncode)

    if args.gui:
        print("🖥️  Launching GUI via PSOperator...")
        client = PSOperatorClient()
        if not client.observer_health():
            print(
                "⚠️  PSOperator Observer not healthy. On the target machine, run:\n"
                "     psoperator observer --backend mss"
            )
        success = client.launch_gui_task(
            task_description=args.message, real_input=args.real_input
        )
        if not success:
            print("❌ GUI task failed.", file=sys.stderr)
            sys.exit(1)
        print("✅ GUI task completed.")
        if client.verify_audit_log():
            print("✅ PSOperator audit log verified.")
        else:
            print("⚠️  Audit log verification failed — check manually.")

    print(f"✅ Task {args.task_id} completed.")
