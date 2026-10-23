"""dx tier — which approval a task's candidate needs, and who may sign it.

The same decision `dx merge` enforces, answered without touching the ledger,
so the review surfaces (a bridge, a packet, a chat) can tell a person the
truth before they reach for a key: "you may sign this yourself and it will be
recorded as an exception", "a second keyholder signs this", or "this is not
signable — it touches the control plane". Asked of dx rather than restated
elsewhere, because a second implementation of the tier is the one that
drifts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._argtypes import SubParsers
from .approval_tier import (
    CONTROL_PLANE,
    SINGLE_REVIEWER,
    DeclarationError,
    chain_changed_paths,
    chain_root_base,
    decide,
    load_declaration,
)
from .config_loader import ConfigError, get_approval_config, get_ledger_repo_path
from .ledger_state import LedgerStateError, current_state, task_rows
from .ledger_utils import LedgerError, get_task_author_human, get_task_queue


def register_tier_subcommand(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "tier",
        help="Which approval a task's candidate needs (single-reviewer, "
        "two-human, control-plane) and why — read-only",
    )
    parser.add_argument("task_id", help="Task ID (e.g. T-0007)")
    parser.add_argument(
        "--repo",
        help="Work repository holding the candidate. Without it the chain diff "
        "cannot be read and the answer is two-human.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.set_defaults(func=cmd_tier)


def cmd_tier(args: argparse.Namespace) -> None:
    ledger_repo = get_ledger_repo_path()
    try:
        queue = get_task_queue(args.task_id, ledger_repo)
        author = get_task_author_human(args.task_id, ledger_repo)
        state = current_state(task_rows(ledger_repo, args.task_id), args.task_id)
    except (LedgerError, LedgerStateError) as exc:
        print(f"❌ {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    candidate = state.candidate_sha or (queue.get("sha") if isinstance(queue.get("sha"), str) else None)

    try:
        cfg = get_approval_config()
        declaration = (
            load_declaration(cfg.declaration, cfg.role, cfg.ai_root) if cfg else None
        )
    except (ConfigError, DeclarationError) as exc:
        print(f"❌ approval declaration unusable: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    repo = Path(args.repo).expanduser() if args.repo else None
    changed: list[str] | None = None
    root_base: str | None = None
    chain: list[str] = [args.task_id]
    if repo is not None and candidate:
        root_base, chain = chain_root_base(args.task_id, ledger_repo)
        changed = chain_changed_paths(repo, root_base, candidate)
    decision = decide(repo, changed, declaration)
    if repo is not None and not candidate:
        decision = decide(repo, None, declaration)

    out: dict[str, object] = {
        "task_id": args.task_id,
        "author": author,
        "candidate": candidate,
        "chain": chain,
        "root_base": root_base,
        **decision.as_json(),
        # Plain words the review surfaces can show without re-deriving them.
        "who_may_sign": _who_may_sign(decision.tier, author),
    }
    if args.json:
        print(json.dumps(out, sort_keys=True), flush=True)
        return
    print(f"{args.task_id}: {decision.tier}", flush=True)
    for reason in decision.reasons:
        print(f"  - {reason}", flush=True)
    print(f"  author: {author or '(none recorded)'}", flush=True)
    print(f"  {out['who_may_sign']}", flush=True)


def _who_may_sign(tier: str, author: str | None) -> str:
    if tier == CONTROL_PLANE:
        return "not signable: control-plane contact (REDLINE on merge)"
    if tier == SINGLE_REVIEWER:
        return (f"any registered keyholder, including the author "
                f"{author or ''}".rstrip()
                + "; an author's own signature is recorded as an sod_exception")
    return f"a registered keyholder other than the author {author or ''}".rstrip()
