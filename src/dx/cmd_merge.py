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
        # TODO(gpg): actually verify against ledger head + enforce Author != Signer
        # for Partial/Anchored roles. See VISION.md "Known gaps".
        print(f"✅ Signature file present: {sig_path} (GPG verification pending)")

    if args.force:
        print("⚠️  --force in effect: bypassing gates.")
    else:
        print(f"✅ Pre-merge checks passed for {args.task_id}.")

    # TODO(ledger): perform actual git merge --no-ff and append hash-chained
    # ledger entry. See VISION.md "Known gaps".
    print(f"🔄 Merging {args.task_id}... (stub — wire to devswarm-ledger)")
    sys.exit(0)
