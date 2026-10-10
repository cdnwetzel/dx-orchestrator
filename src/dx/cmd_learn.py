"""dx learn — continuous improvement from past iterations. Recommend-only.

Reads what already happened — pxx run directories, the devswarm ledger,
psoperator's audit chain, review-lane manifests — and proposes changes ranked
by evidence. It changes nothing: a human accepts or rejects each proposal, an
accepted one becomes a dx task spec that goes through ``dx run`` and a signed
``dx merge`` like any other change, and once that task merges, ``dx learn
efficacy`` judges it against the metric fixed when it was proposed. Changes
that did not work come back as proposals to revise or revert.

Separation of duties: dx learn is the author of every recommendation, so it
is never the one to accept it — every decision names a human with ``--by``.
It never writes to the ledger (RL-008), and no recommendation kind relaxes a
gate. Owning role: ``scrum-master`` (retro actions specific, owned, dated,
and the last set checked before new ones are added) — which is why ``queue``
prints the state of earlier adoptions before any new proposal.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ._argtypes import SubParsers
from .config_loader import get_ledger_repo_path
from .learn_bridge import FORWARDABLE_KINDS, ForwardError, forward_to_pxx, render_spec
from .learn_engine import LearnConfig, Learner, reconcile
from .learn_model import Adoption, Corpus, Recommendation, parse_iso, to_jsonable, utcnow
from .learn_sources import (
    LearnSourceError,
    link_attempts,
    read_ledger,
    read_operator_audit,
    read_pxx_runs,
    read_review_manifests,
)
from .learn_store import JsonStore, MssqlStore, StaleDecision, Store, StoreError

DEFAULT_STORE = Path("~/.local/state/dx/learn").expanduser()
# Words that name the tools and models in this stack, not people. A --by made
# only of these (e.g. "dx.run", "Claude Code", "claude-opus", "pxx") is refused.
_AGENT_WORDS = frozenset({
    "dx", "pxx", "learn", "run", "orchestrator", "psoperator", "psguard", "claude", "code",
    "codex", "kimi", "gpt", "gemini", "copilot", "coderabbit", "opus", "sonnet", "haiku",
    "fable", "mythos", "llm", "ai", "agent", "bot", "assistant", "model", "cli", "review",
    "reviewer", "localhost", "invalid", "example"})


def register_learn_subcommand(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "learn", help="Learn from past iterations and propose ranked improvements "
        "(recommend-only; a human decides, changes go through dx run/merge)")
    sub = parser.add_subparsers(dest="learn_command", required=True)

    def sources(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pxx-state-dir", help="pxx state dir (default: DX_LEARN_PXX_STATE, "
                       "then pxx's own state_dir)")
        p.add_argument("--no-pxx", action="store_true", help="Do not read pxx runs")
        p.add_argument("--no-ledger", action="store_true", help="Do not read the ledger")
        p.add_argument("--allow-reference-ledger", action="store_true",
                       help="Learn from the synthetic reference ledger (demos only)")
        p.add_argument("--psoperator-audit", help="psoperator audit log to read "
                       "(default: DX_LEARN_PSOPERATOR_AUDIT; skipped when unset)")
        p.add_argument("--review-dir", action="append", default=[],
                       help="A review/ directory holding <release>/manifest.tsv (repeatable)")
        p.add_argument("--now", help=argparse.SUPPRESS)  # fixed clock for reproducible runs

    def store(p: argparse.ArgumentParser) -> None:
        p.add_argument("--store-dir", help="JSON store directory (default: DX_LEARN_STORE, "
                       f"then {DEFAULT_STORE}); ignored when DX_LEARN_MSSQL is set")
        p.add_argument("--json", action="store_true", help="Machine-readable output")

    run = sub.add_parser("run", help="One learning pass: read sources, update the queue")
    sources(run)
    store(run)
    run.add_argument("--forward-pxx", action="store_true",
                     help="Also write model-routing proposals into pxx's human-review inbox")
    run.set_defaults(func=cmd_learn_run)

    q = sub.add_parser("queue", help="Earlier adoptions first, then open proposals by score")
    store(q)
    q.add_argument("--all", action="store_true", help="Include decided and stale entries")
    q.set_defaults(func=cmd_learn_queue)

    show = sub.add_parser("show", help="One recommendation with its full evidence")
    store(show)
    show.add_argument("key", help="Recommendation key (a unique prefix of 6+ chars is enough)")
    show.set_defaults(func=cmd_learn_show)

    acc = sub.add_parser("accept", help="Adopt a proposal and write its dx task spec")
    store(acc)
    acc.add_argument("key")
    acc.add_argument("--by", required=True, help="The human deciding (never dx itself)")
    acc.add_argument("--note", default="")
    acc.add_argument("--task", help="dx task id that will implement it (or link later)")
    acc.add_argument("--spec-out", help="Where to write the task spec (default: stdout)")
    acc.set_defaults(func=cmd_learn_accept)

    rej = sub.add_parser("reject", help="Reject a proposal; it stays down until its "
                         "evidence grows by the re-proposal factor")
    store(rej)
    rej.add_argument("key")
    rej.add_argument("--by", required=True)
    rej.add_argument("--note", required=True, help="Why — kept with the decision")
    rej.set_defaults(func=cmd_learn_reject)

    link = sub.add_parser("link", help="Name the dx task implementing an adoption")
    store(link)
    link.add_argument("key")
    link.add_argument("--task", required=True)
    link.add_argument("--by", required=True)
    link.set_defaults(func=cmd_learn_link)

    ret = sub.add_parser("retire", help="Stop tracking an adoption (revised or reverted)")
    store(ret)
    ret.add_argument("key")
    ret.add_argument("--by", required=True)
    ret.add_argument("--note", required=True)
    ret.set_defaults(func=cmd_learn_retire)

    eff = sub.add_parser("efficacy", help="Did adopted changes move their pre-registered metric?")
    sources(eff)
    store(eff)
    eff.set_defaults(func=cmd_learn_efficacy)


# --------------------------------------------------------------------------- helpers
def _die(msg: str, code: int = 1) -> None:
    print(f"❌ {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _open_store(args: argparse.Namespace) -> Store:
    conn = os.environ.get("DX_LEARN_MSSQL")
    try:
        if conn:
            return MssqlStore(conn)
        root = args.store_dir or os.environ.get("DX_LEARN_STORE") or str(DEFAULT_STORE)
        return JsonStore(Path(root).expanduser())
    except StoreError as exc:
        _die(str(exc))
        raise  # unreachable; keeps mypy honest


def _now(args: argparse.Namespace) -> datetime:
    return parse_iso(args.now, "--now") if getattr(args, "now", None) else utcnow()


def _pxx_state_dir(args: argparse.Namespace) -> Path:
    explicit = args.pxx_state_dir or os.environ.get("DX_LEARN_PXX_STATE")
    if explicit:
        return Path(explicit).expanduser()
    from pxx.config import load_settings
    return Path(load_settings().state_dir).expanduser()


def _human(by: str) -> str:
    """The decider must be a person. This is a guard against an automated caller
    writing its own name, not authentication: the OS account running the
    command is recorded beside it, and the authority that changes software is
    still the signed `dx merge`."""
    name = by.strip()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    email_local = re.findall(r"([a-z0-9._-]+)@", name.lower())
    agentish = (not tokens
                or all(t in _AGENT_WORDS or t.isdigit() for t in tokens)
                or any(set(re.split(r"[._-]+", e)) <= _AGENT_WORDS for e in email_local))
    if agentish:
        _die("--by must name the human deciding; dx learn proposed this and may not decide it "
             "(separation of duties: author ≠ approver)")
    return name


def _who(by: str) -> dict[str, str]:
    try:
        account = getpass.getuser()
    except (KeyError, OSError):
        account = "unknown"
    return {"os_user": account, "by": by}


def _decide(store: Store, rec: Recommendation, adoption: Adoption | None, *, by: str,
            action: str, detail: dict[str, Any], now: datetime, expected: str) -> None:
    try:
        store.decide(rec, adoption, actor=by, action=action, now=now,
                     detail={**detail, **_who(by)}, expected_status=expected)
    except StaleDecision as exc:
        _die(f"{exc}. Nothing was recorded.")
    except StoreError as exc:
        _die(f"decision store refused the write: {exc}. Nothing was recorded.")


def _gather(args: argparse.Namespace) -> Corpus:
    corpus = Corpus()
    try:
        if not args.no_pxx:
            read_pxx_runs(_pxx_state_dir(args), corpus)
        if not args.no_ledger:
            read_ledger(get_ledger_repo_path(), corpus, allow_reference=args.allow_reference_ledger)
        audit = args.psoperator_audit or os.environ.get("DX_LEARN_PSOPERATOR_AUDIT")
        if audit:
            read_operator_audit(Path(audit).expanduser(), corpus)
        for d in args.review_dir:
            read_review_manifests(Path(d).expanduser(), corpus)
    except LearnSourceError as exc:
        _die(f"{exc}\n   Nothing was learned and the queue is unchanged.")
    linked = link_attempts(corpus)
    corpus.sources["links"] = f"{linked} pxx runs tied to dx tasks via EXECUTED run="
    return corpus


def _find(store: Store, key: str) -> Recommendation:
    recs = store.load_recs()
    if key in recs:
        return recs[key]
    if len(key) < 6:
        _die("give at least 6 characters of the recommendation key")
    hits = [r for k, r in recs.items() if k.startswith(key)]
    if len(hits) != 1:
        _die(f"{'no' if not hits else 'more than one'} recommendation matches {key!r}")
    return hits[0]


def _emit(args: argparse.Namespace, payload: Any, text: list[str]) -> None:
    if args.json:
        print(json.dumps(to_jsonable(payload), sort_keys=True), flush=True)
    else:
        print("\n".join(text), flush=True)


def _line(r: Recommendation) -> str:
    return f"  [{r.rec_key[:8]}] {r.score:8.2f}  {r.kind:22} {r.subject}"


# --------------------------------------------------------------------------- commands
def cmd_learn_run(args: argparse.Namespace) -> None:
    now = _now(args)
    store = _open_store(args)
    corpus = _gather(args)
    cfg = LearnConfig()
    adoptions = store.load_adoptions()
    fresh = Learner(cfg, now).learn(corpus, adoptions.values())
    existing = store.load_recs()
    # What this pass read: a record is only written back if it is still in that
    # state, so a decision made while the pass ran is never undone.
    expected: dict[str, str | None] = {k: r.status for k, r in existing.items()}
    changed = reconcile(fresh, existing, cfg, now)

    forwarded: list[dict[str, str]] = []
    if args.forward_pxx:
        state_dir = _pxx_state_dir(args)
        for rec in changed:
            if rec.status == "PROPOSED" and rec.kind in FORWARDABLE_KINDS and not rec.pxx_slug:
                try:
                    slug, outcome = forward_to_pxx(rec, state_dir)
                except ForwardError as exc:
                    outcome, slug = f"not forwarded: {exc}", ""
                if outcome.startswith("forwarded"):
                    rec.pxx_slug = slug
                forwarded.append({"rec_key": rec.rec_key, "outcome": outcome})

    skipped = store.save_recs(changed, now, expected)
    changed = [r for r in changed if r.rec_key not in skipped]
    stale = [r.rec_key for r in changed if r.status == "STALE"]
    summary: dict[str, Any] = {"sources": corpus.sources, "attempts": len(corpus.attempts),
               "tasks": len(corpus.tasks), "operator_decisions": len(corpus.operator),
               "lane_results": len(corpus.lanes),
               "quarantined": [to_jsonable(q) for q in corpus.quarantined],
               "proposed": [r.rec_key for r in changed if r.status == "PROPOSED"],
               "stale": stale,
               "forwarded": forwarded,
               "skipped_decided_meanwhile": skipped}
    store.record_pass(summary, corpus, now)

    text = ["dx learn — pass complete (recommend-only; nothing was changed)"]
    text += [f"  {k}: {v}" for k, v in sorted(corpus.sources.items())]
    if corpus.quarantined:
        text.append(f"  ⚠ {len(corpus.quarantined)} record(s) quarantined and not learned from:")
        text += [f"    - {q.source} {q.where}: {q.error}" for q in corpus.quarantined[:10]]
    open_recs = sorted((r for r in changed if r.status == "PROPOSED"), key=lambda r: -r.score)
    text.append(f"  {len(open_recs)} open proposal(s) updated, "
                f"{len(stale)} gone stale")
    text += [_line(r) for r in open_recs[:10]]
    text += [f"  pxx: {f['rec_key'][:8]} {f['outcome']}" for f in forwarded]
    text += [f"  left alone (decided while this pass ran): {k[:8]}" for k in skipped]
    text.append("  next: dx learn queue · dx learn show <key> · dx learn accept <key> --by <you>")
    _emit(args, summary, text)


def cmd_learn_queue(args: argparse.Namespace) -> None:
    store = _open_store(args)
    recs = store.load_recs()
    ads = store.load_adoptions()
    shown = [r for r in recs.values() if args.all or r.status == "PROPOSED"]
    shown.sort(key=lambda r: (r.status != "PROPOSED", -r.score, r.rec_key))
    active = [a for a in ads.values() if a.active]
    try:
        audit = f"decision audit: {store.verify_audit()} row(s), chain verifies"
    except StoreError as exc:
        audit = f"⚠ decision audit DOES NOT VERIFY: {exc} — no decision will be recorded"
    text = [audit, "Earlier adoptions (review these before adding new ones):"]
    text += [f"  [{a.rec_key[:8]}] task={a.task_id or '— not linked —'} by {a.accepted_by} "
             f"on {a.accepted_at.date()} metric={a.metric['name']}" for a in active] or ["  (none)"]
    text.append("Open proposals:" if not args.all else "All recommendations:")
    text += [_line(r) + (f"  ({r.status})" if args.all else "") for r in shown] or ["  (none)"]
    _emit(args, {"adoptions": active, "recommendations": shown}, text)


def cmd_learn_show(args: argparse.Namespace) -> None:
    rec = _find(_open_store(args), args.key)
    text = [f"{rec.rec_key}  {rec.kind}  {rec.status}  score {rec.score}",
            f"  {rec.subject}", "", f"  {rec.draft_text}", "",
            f"  judged by: {rec.metric['name']} for {rec.metric['key']}",
            f"  suggested role: {rec.suggested_role}",
            "  evidence:", json.dumps(to_jsonable(rec.evidence), indent=2, sort_keys=True)]
    if rec.decided_by:
        text.append(f"  decided: {rec.status} by {rec.decided_by} — {rec.decision_note or ''}")
    _emit(args, rec, text)


def cmd_learn_accept(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    rec = _find(store, args.key)
    read_status = rec.status
    if rec.status not in ("PROPOSED", "STALE"):
        _die(f"{rec.rec_key[:8]} is {rec.status}; only an open proposal can be accepted")
    if rec.status == "STALE":
        print("⚠ this proposal is STALE: the last pass no longer saw the evidence for it",
              file=sys.stderr, flush=True)
    out = Path(args.spec_out).expanduser() if args.spec_out else None
    if out is not None and (out.exists() or not out.parent.is_dir()):
        # Checked before the decision is recorded: no accepted proposal without its spec.
        _die(f"{out} already exists or its directory does not; nothing was accepted")
    rec.status, rec.decided_by, rec.decided_at = "ACCEPTED", by, now
    rec.decision_note, rec.weight_at_decision = args.note, rec.evidence_weight
    adoption = Adoption(rec_key=rec.rec_key, metric=dict(rec.metric), accepted_by=by,
                        accepted_at=now, note=args.note, task_id=args.task)
    spec = render_spec(rec, args.task or "<TASK-ID>", accepted_by=by)
    _decide(store, rec, adoption, by=by, action="accept", now=now, expected=read_status,
            detail={"note": args.note, "task_id": args.task, "metric": rec.metric})
    if out is not None:
        with out.open("x", encoding="utf-8") as fh:    # never overwrite someone's spec
            fh.write(spec)
        print(f"✅ accepted {rec.rec_key[:8]}; task spec written to {out}", flush=True)
    else:
        print(spec, flush=True)


def cmd_learn_reject(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    rec = _find(store, args.key)
    if rec.status not in ("PROPOSED", "STALE"):
        _die(f"{rec.rec_key[:8]} is {rec.status}; only an open proposal can be rejected")
    read_status = rec.status
    rec.status, rec.decided_by, rec.decided_at = "REJECTED", by, now
    rec.decision_note, rec.weight_at_decision = args.note, rec.evidence_weight
    _decide(store, rec, None, by=by, action="reject", now=now, expected=read_status,
            detail={"note": args.note})
    print(f"✅ rejected {rec.rec_key[:8]}; it returns only if its evidence grows "
          f"{LearnConfig().reproposal_growth:g}x", flush=True)
    if rec.pxx_slug:
        print(f"   It was forwarded to pxx as {rec.pxx_slug}; dx learn does not decide pxx's "
              f"inbox. To reject it there too: pxx improve triage reject {rec.pxx_slug}",
              flush=True)


def _adoption(store: Store, rec: Recommendation) -> Adoption:
    ad = store.load_adoptions().get(rec.rec_key)
    if ad is None or not ad.active:
        _die(f"{rec.rec_key[:8]} has no active adoption")
        raise AssertionError  # unreachable
    return ad


def cmd_learn_link(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    rec = _find(store, args.key)
    ad = _adoption(store, rec)
    previous, ad.task_id = ad.task_id, args.task
    _decide(store, rec, ad, by=by, action="link", now=now, expected="ACCEPTED",
            detail={"task_id": args.task, "previous_task_id": previous})
    print(f"✅ {rec.rec_key[:8]} is implemented by {args.task}; efficacy starts at its MERGED row",
          flush=True)


def cmd_learn_retire(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    rec = _find(store, args.key)
    ad = _adoption(store, rec)
    ad.active, ad.retired_at = False, now
    rec.status, rec.decided_by, rec.decided_at, rec.decision_note = "RETIRED", by, now, args.note
    _decide(store, rec, ad, by=by, action="retire", now=now, expected="ACCEPTED",
            detail={"note": args.note})
    print(f"✅ retired {rec.rec_key[:8]}", flush=True)


def cmd_learn_efficacy(args: argparse.Namespace) -> None:
    now = _now(args)
    store = _open_store(args)
    ads = [a for a in store.load_adoptions().values() if a.active]
    rows = Learner(LearnConfig(), now).efficacy(_gather(args), ads) if ads else []
    payload = [{"rec_key": r["adoption"].rec_key, "task_id": r["adoption"].task_id,
                "metric": r["adoption"].metric, "status": r["status"],
                "before": None if r["before"] is None else round(r["before"].mean, 4),
                "after": None if r["after"] is None else round(r["after"].mean, 4),
                "runs_after": r["runs_after"],
                "p_rate_fell": None if r["p_rate_fell"] is None else round(r["p_rate_fell"], 4)}
               for r in rows]
    text = ["Adoption efficacy (rates are bad-event rates; lower is better):"]
    for p in payload:
        rates = (f" {p['before']:.0%} → {p['after']:.0%}"
                 if p["before"] is not None and p["after"] is not None else "")
        text.append(f"  [{p['rec_key'][:8]}] {p['status']:<16} task={p['task_id'] or '—'}"
                    f"{rates}  {p['metric']['name']}")
    if not payload:
        text.append("  (no active adoptions)")
    _emit(args, payload, text)
