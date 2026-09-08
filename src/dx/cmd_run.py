from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ._argtypes import SubParsers
from .config_loader import get_roles_path, get_route_for_role
from .evidence import Check, EvidenceError, RoleTaskBundle, write_bundle
from .psoperator_client import PSOperatorClient
from .role_models import FitLevel
from .role_registry import failed_slug, get_parse_failures, get_role, load_registry
from .role_validate import validate_card

#: Evidence lands outside the repository under edit. Writing it inside would
#: put receipts in the tree pxx is committing, which is how an evidence store
#: ends up attesting to itself.
DEFAULT_EVIDENCE_ROOT = Path("~/.local/state/dx/evidence").expanduser()


def _evidence_root(args: argparse.Namespace) -> Path:
    if getattr(args, "evidence_dir", None):
        return Path(args.evidence_dir).expanduser()
    env = os.environ.get("DX_EVIDENCE_DIR")
    return Path(env).expanduser() if env else DEFAULT_EVIDENCE_ROOT


def _git(scope: str, *args: str) -> str | None:
    """Best-effort git read. Returns None when scope is not a repo or git fails.

    Evidence collection must never be able to fail the task it is recording.
    """
    try:
        result = subprocess.run(
            ["git", "-C", scope, *args], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _emit_evidence(
    args: argparse.Namespace,
    card_fit: str,
    route: object,
    prompt: str,
    cmd: list[str],
    source_head: str | None,
    returncode: int,
) -> Path:
    """Build and write the `dx.role_task.v1` bundle for this run.

    Raises EvidenceError; the caller decides what a missing receipt is worth.
    """
    endpoint = getattr(route, "endpoint", None)
    model = getattr(route, "model", None)
    provider = getattr(route, "provider", None)

    diff = _git(args.scope, "diff", source_head) if source_head else None
    status = _git(args.scope, "status", "--porcelain")

    artifacts: dict[str, str] = {
        "prompt.txt": prompt,
        "command.txt": " ".join(cmd) + f"\n\nexit: {returncode}\n",
        "routing.json": json.dumps(
            {
                "endpoint": endpoint,
                "model": model,
                "provider": provider,
                "endpoint_raw": getattr(route, "endpoint_raw", None),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    }
    checks: dict[str, Check] = {
        "role_card_valid": Check(ok=True, detail=f"fit={card_fit}"),
        "routing_resolved": Check(ok=bool(endpoint), path="artifacts/routing.json"),
        "pxx_exit_zero": Check(ok=returncode == 0, path="artifacts/command.txt",
                               detail=f"exit={returncode}"),
    }
    if diff is not None:
        artifacts["changes.patch"] = diff
        checks["scope_diff_captured"] = Check(ok=True, path="artifacts/changes.patch")
    else:
        checks["scope_diff_captured"] = Check(
            ok=False, detail="scope is not a git repository, or git failed"
        )
    if status is not None:
        artifacts["git-status.txt"] = status

    bundle = RoleTaskBundle(
        task_id=args.task_id,
        title=f"{args.task_id} — {args.required_role}",
        passed=returncode == 0,
        source_head=source_head,
        role=args.required_role,
        routing={"endpoint": endpoint, "model": model, "provider": provider},
        checks=checks,
        artifacts=artifacts,
    )
    return write_bundle(bundle, _evidence_root(args))


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
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="Skip evidence-bundle emission (the run still happens; the receipt does not)",
    )
    parser.add_argument(
        "--evidence-dir",
        help="Where to write evidence bundles (default: DX_EVIDENCE_DIR, "
        "else ~/.local/state/dx/evidence)",
    )
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


# Exit codes are dx's own contract, not a downstream tool's.
#
# EXIT_ANCHORED_REFUSED is a governance decision: dx looked at the role card and
# refused. It must never be producible by a tool dx shells out to, or a caller
# cannot tell "policy refused this role" from "the task tried and failed" —
# and pxx does exit 2 in the wild (a missing shell safeguard does it). So a
# downstream failure gets its own code and pxx's real code is printed, not
# returned.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ANCHORED_REFUSED = 2
EXIT_TASK_FAILED = 3


def cmd_run(args: argparse.Namespace) -> None:
    roles_path = get_roles_path()
    if not roles_path.exists():
        print(f"ERROR: role cards not found at {roles_path}", file=sys.stderr)
        print("Set DX_ROLES_PATH or run scripts/setup_dependencies.sh", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    load_registry(roles_path)
    card = get_role(args.required_role)
    if not card:
        broken = failed_slug(args.required_role)
        if broken:
            # Distinguishing these matters: "not found" reads like a typo, and
            # would send an operator looking in the wrong place while their
            # governance file is the thing that is broken.
            print(
                f"ERROR: the card for role '{args.required_role}' exists but "
                f"could not be parsed ({broken}).",
                file=sys.stderr,
            )
            print(
                f"       {get_parse_failures()[broken]}",
                file=sys.stderr,
            )
            print("Run: dx roles validate", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        print(f"ERROR: role '{args.required_role}' not found.", file=sys.stderr)
        print("Run: dx roles list", file=sys.stderr)
        sys.exit(EXIT_ERROR)

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
        sys.exit(EXIT_ANCHORED_REFUSED)

    # A card that fails structural validation cannot govern a run. Without this
    # dx would inject an empty MANDATE and an empty MUST NOT into the prompt and
    # report success — the governance text meant to constrain the agent silently
    # blank, while `dx roles validate` said the card was invalid all along.
    card_ok, card_errors = validate_card(card)
    if not card_ok:
        if not args.force:
            print(
                f"ERROR: role card '{card.slug}' fails validation and cannot "
                f"govern a task:",
                file=sys.stderr,
            )
            for error in card_errors:
                print(f"  - {error}", file=sys.stderr)
            print("Run: dx roles validate", file=sys.stderr)
            print(
                "To proceed anyway (audit-visible), re-run with --force.",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERROR)
        print(
            f"⚠️  --force in effect: running under invalid role card "
            f"'{card.slug}' ({'; '.join(card_errors)}).",
            file=sys.stderr,
            flush=True,
        )

    enhanced_prompt = (
        f"[ROLE: {card.slug} (Fit: {card.fit.value}, Seat: {card.seat})]\n"
        f"MANDATE:\n{card.mandate}\n\n"
        f"MUST NOT (Separation of Duties):\n{card.must_not}\n\n"
        f"USER INSTRUCTION:\n{args.message}\n"
    )

    try:
        route = get_route_for_role(args.required_role)
        if route.endpoint_raw is not None:
            # Corrected, never silent: dx changed what the operator wrote.
            print(
                f"ℹ️  endpoint {route.endpoint_raw} → {route.endpoint} "
                f"(pxx appends its own /v1; the manifest's trailing /v1 would "
                f"be probed as /v1/v1/models and 404). Fix it in the manifest "
                f"to silence this.",
                file=sys.stderr,
                flush=True,
            )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

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
        sys.exit(EXIT_ERROR)

    cmd = [pxx_bin, "edit", "--scope", args.scope, "--message", enhanced_prompt]
    if not args.no_commit:
        cmd.append("--commit")

    if args.dry_run:
        print("DRY RUN")
        print(f"  task_id:       {args.task_id}")
        print(f"  required_role: {args.required_role}")
        print(f"  fit:           {card.fit.value}")
        print(f"  PXX_BASE_URL:  {route.endpoint}")
        if route.endpoint_raw is not None:
            print(f"                 (corrected from {route.endpoint_raw})")
        print(f"  PXX_MODEL:     {route.model or '(unset, pxx default)'}")
        print(f"  PXX_PROVIDER:  {route.provider or '(unset, pxx default: ollama)'}")
        print(f"  pxx:           {pxx_bin}")
        print(f"  command:       {pxx_bin} edit --scope {args.scope} [--commit] <prompt>")
        return

    # Flushed before handing stdout to the subprocess. pxx writes straight to
    # the inherited fd, so an unflushed status line here surfaces *after* the
    # output of the command it announces — the same defect fixed in dx merge in
    # 0.3.0. A run transcript is evidence too.
    print(
        f"🚀 Running task {args.task_id} with role {args.required_role} "
        f"on {route.endpoint} (model: {route.model or 'default'})...",
        flush=True,
    )
    # Captured before the run so the bundle's source_head is the base the diff
    # is taken against, not whatever pxx committed.
    source_head = (
        None
        if args.no_evidence
        else ((_git(args.scope, "rev-parse", "HEAD") or "").strip() or None)
    )
    # unbounded: this is the model doing the work. A large refactor on a slow
    # local endpoint legitimately runs for minutes, and cutting it off at an
    # arbitrary deadline would destroy in-flight edits. Ctrl-C is the control.
    result = subprocess.run(cmd, env=env)

    # Emitted for failed runs too. A failed run's evidence is worth more, not
    # less, and a store that only records successes is a highlight reel.
    if not args.no_evidence:
        try:
            bundle_path = _emit_evidence(
                args, card.fit.value, route, enhanced_prompt, cmd,
                source_head, result.returncode,
            )
        except EvidenceError as exc:
            print(
                f"❌ The task itself finished (pxx exit {result.returncode}), but its "
                f"evidence bundle could not be written: {exc}",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(EXIT_ERROR)
        print(f"🧾 Evidence: {bundle_path}", flush=True)

    if result.returncode != 0:
        print(
            f"❌ pxx task failed (pxx exit {result.returncode}).",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_TASK_FAILED)

    if args.gui:
        print("🖥️  Launching GUI via PSOperator...", flush=True)
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
            print("❌ GUI task failed.", file=sys.stderr, flush=True)
            sys.exit(EXIT_ERROR)
        print("✅ GUI task completed.", flush=True)
        if client.verify_audit_log():
            print("✅ PSOperator audit log verified.")
        else:
            print("⚠️  Audit log verification failed — check manually.")

    print(f"✅ Task {args.task_id} completed.", flush=True)
