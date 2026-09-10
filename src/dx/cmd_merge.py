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
import os
import re
import sys
from pathlib import Path

from ._argtypes import SubParsers
from .cmd_verify import verify_gui
from .config_loader import get_ledger_repo_path
from .evidence import (
    Check,
    EvidenceError,
    MergeGateBundle,
    bundle_digest,
    write_merge_bundle,
)
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
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="Skip the dx.merge_gate.v1 bundle (the gate still runs)",
    )
    parser.add_argument(
        "--evidence-dir",
        help="Where to write the bundle (default: DX_EVIDENCE_DIR, "
        "else ~/.local/state/dx/evidence)",
    )
    parser.set_defaults(func=cmd_merge)


# Kept in sync with cmd_run/cmd_verify — receipts live outside any repo under edit.
DEFAULT_EVIDENCE_ROOT = Path("~/.local/state/dx/evidence").expanduser()


def _evidence_root(args: argparse.Namespace) -> Path:
    if getattr(args, "evidence_dir", None):
        return Path(args.evidence_dir).expanduser()
    env = os.environ.get("DX_EVIDENCE_DIR")
    return Path(env).expanduser() if env else DEFAULT_EVIDENCE_ROOT


def _emit_merge_evidence(
    args: argparse.Namespace, rec: dict[str, object], passed: bool
) -> Path | None:
    """Write a dx.merge_gate.v1 bundle from the accumulated gate record and
    announce it on stderr — stdout is the pinned RL-003 transcript. Returns the
    bundle directory, or None if it could not be written."""
    def _d(key: str) -> dict[str, object]:
        val = rec.get(key)
        return val if isinstance(val, dict) else {}

    checks: dict[str, Check] = {}
    if rec.get("head_before") is not None:
        checks["ledger_chain_verifies"] = Check(ok=True, detail=str(rec.get("head_before"))[:16])
    if rec.get("signer") is not None:
        checks["signature_verified"] = Check(
            ok=rec.get("failure") != "signed_message_mismatch",
            detail=str(_d("signer").get("name")),
        )
    if rec.get("separation_of_duties") is not None:
        checks["separation_of_duties"] = Check(ok=bool(rec.get("separation_of_duties")))
    if rec.get("gui") is not None:
        checks["gui_verification"] = Check(
            ok=bool(_d("gui").get("verified")),
            detail=str(_d("gui").get("answer"))[:120],
        )
    if rec.get("merged") is not None:
        checks["git_merged"] = Check(ok=True, detail=str(_d("merged").get("merge_commit"))[:12])
    bundle = MergeGateBundle(
        task_id=args.task_id,
        title=f"Merge gate — {args.task_id}",
        passed=passed,
        ledger_repo=str(rec.get("ledger_repo") or ""),
        role=rec.get("role"),  # type: ignore[arg-type]
        head_before=rec.get("head_before"),  # type: ignore[arg-type]
        head_after=rec.get("head_after"),  # type: ignore[arg-type]
        signer=rec.get("signer"),  # type: ignore[arg-type]
        author_human=rec.get("author_human"),  # type: ignore[arg-type]
        separation_of_duties=rec.get("separation_of_duties"),  # type: ignore[arg-type]
        gui=rec.get("gui"),  # type: ignore[arg-type]
        merged=rec.get("merged"),  # type: ignore[arg-type]
        failure=rec.get("failure"),  # type: ignore[arg-type]
        source_head=rec.get("head_before"),  # type: ignore[arg-type]
        checks=checks,
    )
    try:
        path = write_merge_bundle(bundle, _evidence_root(args))
        print(f"🧾 Evidence: {path}", file=sys.stderr, flush=True)
        return path
    except EvidenceError as exc:
        print(f"⚠️  merge-gate evidence bundle could not be written: {exc}", file=sys.stderr, flush=True)
        return None


def _bind_bundle_into_ledger(
    args: argparse.Namespace, rec: dict[str, object], bundle_path: Path
) -> None:
    """Append an EVIDENCE row binding the merge-gate bundle's digest into the
    chain, so the append-only ledger — and the RL-003 signature over its head —
    transitively commit to the evidence, not just to the fact a merge happened.

    Rides the existing action enum (EVIDENCE); the digest is a hash, not a secret
    (RL-011). Announced on stderr, so the pinned RL-003 transcript on stdout is
    unchanged. Best-effort: a receipt that cannot be bound warns, it does not undo
    a completed merge."""
    ledger_repo_raw = rec.get("ledger_repo")
    if not isinstance(ledger_repo_raw, str):
        return
    ledger_repo = Path(ledger_repo_raw)
    role = rec.get("role")
    try:
        digest = bundle_digest(bundle_path)
        head = read_head(ledger_repo / "ledger.jsonl")
        row = build_row(
            action="EVIDENCE",
            task_id=args.task_id,
            evidence=f"dx.merge_gate.v1 sha256:{digest}",
            prev_hash=head,
            reviewer_seat=role if isinstance(role, str) else None,
        )
        new_head = append_row(ledger_repo, row)
        print(
            f"🔗 EVIDENCE row binds the merge-gate bundle (sha256:{digest[:16]}…). "
            f"Head: {new_head[:16]}…",
            file=sys.stderr,
            flush=True,
        )
    except (EvidenceError, LedgerWriteError, LedgerError, OSError) as exc:
        print(
            f"⚠️  could not bind the merge-gate bundle into the ledger: {exc}",
            file=sys.stderr,
            flush=True,
        )


def cmd_merge(args: argparse.Namespace) -> None:
    """Run the gate, then emit a dx.merge_gate.v1 bundle from what it decided.

    The gate exits via SystemExit at each verdict (0 pass, 1 fail); the bundle
    is written on the way out, from the record the gate accumulated, unless the
    gate never really started (a --force bypass, which writes nothing) or
    --no-evidence was given."""
    rec: dict[str, object] = {}
    try:
        _run_merge(args, rec)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if rec.get("started") and not args.no_evidence:
            bundle_path = _emit_merge_evidence(args, rec, passed=(code == 0))
            # A successful merge appended rows; bind the bundle digest into the
            # chain so the ledger commits to the evidence, not just the event.
            if code == 0 and bundle_path is not None:
                _bind_bundle_into_ledger(args, rec, bundle_path)
        raise


_HEAD_RE = re.compile(r"^[0-9a-f]{64}$")


def _looks_like_head(value: str) -> bool:
    """A ledger head is a 64-character lowercase hex SHA-256 digest."""
    return bool(_HEAD_RE.match(value))


def _norm(name: str | None) -> str:
    """Case- and whitespace-insensitive compare for author vs signer names."""
    return (name or "").strip().lower()


def _run_merge(args: argparse.Namespace, rec: dict[str, object]) -> None:
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

    rec["started"] = True

    # 1. Optional GUI verification
    if args.verify_gui:
        _info("📷 Running GUI verification...")
        expected = args.expected or "The GUI shows the correct result."
        passed, output = verify_gui(expected)
        rec["gui"] = {"verified": passed, "answer": output}
        if not passed:
            rec["failure"] = "gui_verification_failed"
            _fail(f"GUI verification failed: {output}")
        _ok(f"GUI verification passed: {output}")

    # 2. Ledger-based signature check (RL-003)
    try:
        ledger_repo = get_ledger_repo_path()
        rec["ledger_repo"] = str(ledger_repo)
        if not ledger_repo.exists():
            rec["failure"] = "ledger_not_found"
            _fail(
                f"devswarm-ledger not found at {ledger_repo}. "
                "Clone it or set DX_LEDGER_REPO."
            )

        queue = get_task_queue(args.task_id, ledger_repo)
        role = queue.get("approve_role")
        rec["role"] = role
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
        rec["head_before"] = current_head
        _ok(f"Ledger chain verifies. Head: {current_head[:16]}…")

        # (1) Signature verifies against a currently-valid key in docs/keys/
        signer = verify_detached_signature(sig_path, msg_path, ledger_repo)
        rec["signer"] = {"name": signer.name, "email": signer.email}
        _ok(f"Signature verified. Signer: {signer.name} <{signer.email or 'no-email'}>")

        # (2) Signed message payload must be exactly `task_id + head + role`
        expected_msg = canonical_approval_message(args.task_id, current_head, role)
        actual_msg = msg_path.read_text(encoding="utf-8")
        if actual_msg != expected_msg:
            rec["failure"] = "signed_message_mismatch"
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
        rec["author_human"] = author_human
        if author_human and _norm(author_human) == _norm(signer.name):
            rec["separation_of_duties"] = False
            rec["failure"] = "separation_of_duties"
            _fail(
                f"Separation-of-duties violation: author '{author_human}' "
                f"and signer '{signer.name}' are the same person."
            )
        if not author_human:
            rec["separation_of_duties"] = None
            _warn(
                f"No author_human found in ledger for {args.task_id} — "
                "cannot enforce signer != author. Proceeding."
            )
        else:
            rec["separation_of_duties"] = True
            _ok(
                f"Separation of duties: author '{author_human}' ≠ "
                f"signer '{signer.name}'."
            )

    except LedgerError as exc:
        rec.setdefault("failure", "pre_merge_check_error")
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
                    rec["failure"] = "no_sha_to_merge"
                    _fail(f"queue file for {args.task_id} has no 'sha' to merge")
                merged_sha = git_merge_no_ff(target, str(task_sha), task_id=args.task_id)
                rec["merged"] = {
                    "repo": str(target),
                    "task_sha": str(task_sha),
                    "merge_commit": merged_sha,
                }
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
            rec["head_after"] = merged_head
            _ok(f"MERGED row appended. Head: {merged_head[:16]}…")
    except LedgerWriteError as exc:
        rec["failure"] = "ledger_write_failed"
        _fail(f"Ledger write failed: {exc}")

    if not args.repo:
        _warn(
            "No --repo given, so no git merge was performed. The ledger records "
            "the approval only."
        )
    print(f"✅ {args.task_id} merged and recorded.", flush=True)
    sys.exit(0)
