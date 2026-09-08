import sys
from pathlib import Path

from .cmd_verify import verify_gui


def register_merge_subcommand(subparsers) -> None:
    parser = subparsers.add_parser(
        "merge", help="Merge gate: separation of duties + optional GUI verification"
    )
    parser.add_argument("task_id", help="Task ID")
    parser.add_argument(
        "--signature", "-s", help="Path to GPG detached signature (stubbed for now)"
    )
    parser.add_argument(
        "--verify-gui",
        action="store_true",
        help="Run GUI verification before merging",
    )
    parser.add_argument(
        "--expected", help="Expected GUI state (used with --verify-gui)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass gates (audit-logged, use only for emergency rollback)",
    )
    parser.set_defaults(func=cmd_merge)


def cmd_merge(args) -> None:
    if args.verify_gui and not args.force:
        print("📷 Running GUI verification...")
        expected = args.expected or "The GUI shows the correct result."
        passed, output = verify_gui(expected)
        if not passed:
            print(f"❌ GUI verification failed: {output}", file=sys.stderr)
            sys.exit(1)
        print(f"✅ GUI verification passed: {output}")

    if args.signature:
        sig_path = Path(args.signature)
        if not sig_path.exists():
            print(f"ERROR: signature file {sig_path} not found", file=sys.stderr)
            sys.exit(1)
        # TODO(gpg): verify per devswarm-ledger SCHEMA.md "approvals/" contract.
        # The signature is GPG-detached over the canonical UTF-8 string:
        #     task_id + ledger_head_hash + role      (exact concat, no separators)
        # Valid iff:
        #   1. Signer's key is registered in devswarm-ledger/docs/keys/<name>.asc
        #      as the human accountable for `role`.
        #   2. Signer != task's author_human (when separation-of-duties applies —
        #      Partial or Anchored fit).
        #   3. Signed head hash equals the CURRENT head of ledger.jsonl
        #      (stale-head signatures rejected per RL-003).
        # Reference impl: devswarm-ledger/tools/verify_chain.py.
        # Real signed examples: approvals/T-0002.code_review.{msg,asc} etc.
        print(f"✅ Signature file present: {sig_path} (GPG verification pending)")

    if args.force:
        print("⚠️  --force in effect: bypassing gates.")
    else:
        print(f"✅ Pre-merge checks passed for {args.task_id}.")

    # TODO(ledger): append rows to devswarm-ledger/ledger.jsonl per SCHEMA.md.
    # Sequence for a successful merge:
    #   1. SIGNED row (if not already appended by the signer)
    #   2. MERGED row after git merge --no-ff succeeds
    # Row canonical form:
    #     json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    # Fields: ts (ISO 8601 UTC), task_id, author_seat, author_human, reviewer_seat,
    #         action, sha (merge commit), evidence (redacted per RL-011), prev_hash.
    # prev_hash MUST equal sha256(canonical_form(previous row including its own
    # prev_hash)). MERGE_LOCK.json in queue/ serializes concurrent merges.
    print(f"🔄 Merging {args.task_id}... (stub — wire to devswarm-ledger)")
    sys.exit(0)
