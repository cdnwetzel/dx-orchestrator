"""dx merge — pre-merge gate.

Wires the three RL-003 signature validity checks from
devswarm-ledger/SCHEMA.md § approvals/. The actual git merge and ledger
append are still stubbed — those cross into devswarm-ledger territory and
are intentionally deferred until Gate 1 unpauses.

Output discipline: every gate verdict is flushed as it is decided. A merge
gate's transcript is evidence, and evidence gets piped into logs — verdicts
must appear in the order they were reached whether stdout is a tty or a pipe.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from ._argtypes import SubParsers
from .cmd_verify import verify_gui
from .config_loader import get_ledger_repo_path
from .ledger_utils import (
    LedgerError,
    canonical_approval_message,
    get_ledger_head,
    get_task_author_human,
    get_task_queue,
    verify_detached_signature,
)
from .ledger_writer import (
    LedgerWriteError,
    MergeLock,
    append_row,
    build_row,
    git_merge_no_ff,
    read_head,
)


def _ok(msg: str) -> None:
    print(f"✅ {msg}", flush=True)


def _info(msg: str) -> None:
    print(msg, flush=True)


def _warn(msg: str) -> None:
    sys.stdout.flush()
    print(f"⚠️  {msg}", file=sys.stderr, flush=True)


def _fail(msg: str, code: int = 1) -> None:
    sys.stdout.flush()
    print(f"❌ {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def register_merge_subcommand(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "merge",
        help="Merge gate: RL-003 signature check + optional GUI verification",
    )
    parser.add_argument("task_id", help="Task ID (e.g. T-0007)")
    parser.add_argument(
        "--repo",
        help="Work repository to merge in. Without it dx records the approval "
        "but performs no git merge, and says so.",
    )
    parser.add_argument(
        "--signature", "-s",
        help="Path to GPG detached signature (.asc). "
        "Default: <ledger>/approvals/<task_id>.<role>.asc",
    )
    parser.add_argument(
        "--message", "-M",
        help="Path to the signed message file (.msg). "
        "Default: <ledger>/approvals/<task_id>.<role>.msg",
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
        help=(
            "Bypass ALL merge gates — the RL-003 signature check AND GUI "
            "verification. Audit-visible; use only for emergency rollback."
        ),
    )
    parser.set_defaults(func=cmd_merge)


_HEAD_RE = re.compile(r"^[0-9a-f]{64}$")


def _looks_like_head(value: str) -> bool:
    """A ledger head is a 64-character lowercase hex SHA-256 digest."""
    return bool(_HEAD_RE.match(value))


def _norm(name: str | None) -> str:
    """Case- and whitespace-insensitive compare for author vs signer names."""
    return (name or "").strip().lower()


def cmd_merge(args: argparse.Namespace) -> None:
    if args.force:
        _warn(
            f"--force in effect for {args.task_id}: bypassing the RL-003 "
            "signature check"
            + (" AND GUI verification." if args.verify_gui else ".")
        )
        _info(
            f"🔄 {args.task_id}: gates bypassed, so nothing was written. dx does "
            f"not append SIGNED or MERGED rows for an ungated merge — the ledger "
            f"would then attest to a check that did not happen."
        )
        sys.exit(0)

    # 1. Optional GUI verification
    if args.verify_gui:
        _info("📷 Running GUI verification...")
        expected = args.expected or "The GUI shows the correct result."
        passed, output = verify_gui(expected)
        if not passed:
            _fail(f"GUI verification failed: {output}")
        _ok(f"GUI verification passed: {output}")

    # 2. Ledger-based signature check (RL-003)
    try:
        ledger_repo = get_ledger_repo_path()
        if not ledger_repo.exists():
            _fail(
                f"devswarm-ledger not found at {ledger_repo}. "
                "Clone it or set DX_LEDGER_REPO."
            )

        queue = get_task_queue(args.task_id, ledger_repo)
        role = queue.get("approve_role")
        if not role:
            raise LedgerError(
                f"queue file for {args.task_id} has no 'approve_role' field"
            )

        # Resolve signature + message paths (default to ledger conventions)
        sig_path = (
            Path(args.signature).expanduser()
            if args.signature
            else ledger_repo / "approvals" / f"{args.task_id}.{role}.asc"
        )
        msg_path = (
            Path(args.message).expanduser()
            if args.message
            else ledger_repo / "approvals" / f"{args.task_id}.{role}.msg"
        )

        # (0) Verify the ledger chain itself and get the current head hash
        current_head = get_ledger_head(ledger_repo)
        _ok(f"Ledger chain verifies. Head: {current_head[:16]}…")

        # (1) Signature verifies against a currently-valid key in docs/keys/
        signer = verify_detached_signature(sig_path, msg_path, ledger_repo)
        _ok(f"Signature verified. Signer: {signer.name} <{signer.email or 'no-email'}>")

        # (2) Signed message payload must be exactly `task_id + head + role`
        expected_msg = canonical_approval_message(args.task_id, current_head, role)
        actual_msg = msg_path.read_text(encoding="utf-8")
        if actual_msg != expected_msg:
            # Stale and tampered are different failures with different
            # remedies. "Stale" tells the operator to re-sign against the
            # current head — the wrong and actively unsafe advice if the
            # payload has been altered. Only claim staleness when the middle
            # section actually looks like a ledger head.
            if actual_msg.startswith(args.task_id) and actual_msg.endswith(role):
                signed_head = actual_msg[len(args.task_id):-len(role)]
                if _looks_like_head(signed_head) and signed_head != current_head:
                    _fail(
                        f"Stale signature (RL-003). Signed head "
                        f"{signed_head[:16]}… but current head is "
                        f"{current_head[:16]}…. Re-sign after re-verifying the "
                        f"chain."
                    )
                if not _looks_like_head(signed_head):
                    _fail(
                        f"Malformed approval payload in {msg_path.name}: the "
                        f"section between the task id and the role is not a "
                        f"64-character hex ledger head "
                        f"(found {len(signed_head)} chars). Do not re-sign this "
                        f"— establish where it came from."
                    )
            _fail(
                f"Signed message payload does not match "
                f"'{args.task_id} + head + {role}'. Expected "
                f"{len(expected_msg)} bytes, found {len(actual_msg)}."
            )
        _ok("Signed message binds task_id + current head + role.")

        # (3) Separation of duties: signer != task's author_human
        author_human = get_task_author_human(args.task_id, ledger_repo)
        if author_human and _norm(author_human) == _norm(signer.name):
            _fail(
                f"Separation-of-duties violation: author '{author_human}' "
                f"and signer '{signer.name}' are the same person."
            )
        if not author_human:
            _warn(
                f"No author_human found in ledger for {args.task_id} — "
                "cannot enforce signer != author. Proceeding."
            )
        else:
            _ok(
                f"Separation of duties: author '{author_human}' ≠ "
                f"signer '{signer.name}'."
            )

    except LedgerError as exc:
        _fail(f"Pre-merge check failed: {exc}")

    _ok(f"All RL-003 checks passed for {args.task_id}.")

    # The head moves as soon as anything is appended, and the signature above
    # was verified against `current_head`. So: verify first (done), then append,
    # and never re-check that signature afterwards. The SIGNED row records the
    # head it was actually checked against, so the sequence stays auditable once
    # the head has moved on.
    try:
        with MergeLock(ledger_repo, args.task_id):
            signed_head = append_row(
                ledger_repo,
                build_row(
                    action="SIGNED",
                    task_id=args.task_id,
                    prev_hash=current_head,
                    author_human="(reviewer)",
                    author_seat=queue.get("author_seat"),
                    reviewer_seat=queue.get("reviewer_seat"),
                    sha=queue.get("sha"),
                    evidence=(
                        f"detached signature over task_id+{current_head}+{role} "
                        f"at {sig_path.name}, verified against that head; "
                        f"signer {signer.name}"
                    ),
                ),
            )
            _ok(f"SIGNED row appended. Head: {signed_head[:16]}…")

            merged_sha = None
            if args.repo:
                target = Path(args.repo).expanduser()
                task_sha = queue.get("sha")
                if not task_sha:
                    _fail(f"queue file for {args.task_id} has no 'sha' to merge")
                merged_sha = git_merge_no_ff(target, str(task_sha), task_id=args.task_id)
                _ok(f"Merged {str(task_sha)[:12]} into {target} — {merged_sha[:12]}")

            # Re-read: the SIGNED append moved the head. Reusing current_head
            # here would append a row whose prev_hash is two rows stale, and
            # break the chain this gate exists to protect.
            head_after_signed = read_head(ledger_repo / "ledger.jsonl")
            merged_head = append_row(
                ledger_repo,
                build_row(
                    action="MERGED",
                    task_id=args.task_id,
                    prev_hash=head_after_signed,
                    author_seat=queue.get("author_seat"),
                    reviewer_seat=queue.get("reviewer_seat"),
                    sha=merged_sha or queue.get("sha"),
                    evidence=(
                        f"git merge --no-ff into {args.repo} at {merged_sha}"
                        if merged_sha
                        else "no --repo given: approval recorded, no git merge performed"
                    ),
                ),
            )
            _ok(f"MERGED row appended. Head: {merged_head[:16]}…")
    except LedgerWriteError as exc:
        _fail(f"Ledger write failed: {exc}")

    if not args.repo:
        _warn(
            "No --repo given, so no git merge was performed. The ledger records "
            "the approval only."
        )
    print(f"✅ {args.task_id} merged and recorded.", flush=True)
    sys.exit(0)
