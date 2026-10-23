"""dx learn — continuous improvement from past iterations. Recommend-only.

Reads what already happened — pxx run directories, the devswarm ledger,
psoperator's audit chain, review-lane manifests — and proposes changes ranked
by evidence. It changes nothing: a human accepts or rejects each proposal, an
accepted one becomes a dx task spec that goes through ``dx run`` and a signed
``dx merge`` like any other change, and once that task merges, ``dx learn
efficacy`` compares the metric fixed at proposal time before and after the
merge. That comparison is an observed change, not proof of cause.

Separation of duties: dx learn is the author of every recommendation, so it
is never the one to accept it — every decision names a human with ``--by``.
It never writes to the ledger (RL-008), and no recommendation kind relaxes a
gate. Owning role: ``scrum-master`` (retro actions specific, owned, dated,
and the last set checked before new ones are added) — which is why ``queue``
prints the state of earlier adoptions before any new proposal.

Concurrency: every command that changes state takes the store's mutation
lock, re-reads what it decides on, and commits one transaction checked
against the revisions it read (see learn_store). Forwarding to pxx happens
only inside that lock, after the pass's own write (see learn_forward).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ._argtypes import SubParsers
from .config_loader import get_ledger_repo_path
from .learn_bridge import render_spec
from .learn_engine import LearnConfig, Learner, reconcile
from .learn_forward import forward_proposals, reconcile_forwards
from .learn_model import Adoption, Corpus, Recommendation, parse_iso, to_jsonable, utcnow
from .learn_sources import (
    LearnSourceError,
    link_attempts,
    read_ledger,
    read_operator_audit,
    read_pxx_runs,
    read_review_manifests,
)
from .learn_store import (
    JsonStore,
    MssqlStore,
    RecoveryError,
    StaleDecision,
    Store,
    StoreError,
    Tx,
)
from .learn_views import (
    CURRENT_POLICY,
    EXPLORATORY,
    POLICY_SEAMS,
    VIEW_LABELS,
    VIEWS,
    ViewReport,
    mixed_provenance,
    select,
)

DEFAULT_STORE = Path("~/.local/state/dx/learn").expanduser()
MSSQL_ACK = "accept"
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

    def seen(p: argparse.ArgumentParser) -> None:
        p.add_argument("--seen", help="The decision token `show`/`queue` printed (rev.rec.adoption): "
                       "refuse if the proposal changed since you looked")

    def pxx_dir(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pxx-state-dir", help="pxx state dir for withdrawing a forwarded "
                       "entry (default: DX_LEARN_PXX_STATE, then pxx's own state_dir)")

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
    seen(acc)
    acc.set_defaults(func=cmd_learn_accept)

    rej = sub.add_parser("reject", help="Reject a proposal; it stays down until its "
                         "evidence grows by the re-proposal factor")
    store(rej)
    pxx_dir(rej)
    rej.add_argument("key")
    rej.add_argument("--by", required=True)
    rej.add_argument("--note", required=True, help="Why — kept with the decision")
    seen(rej)
    rej.set_defaults(func=cmd_learn_reject)

    link = sub.add_parser("link", help="Name the dx task implementing an adoption")
    store(link)
    link.add_argument("key")
    link.add_argument("--task", required=True)
    link.add_argument("--by", required=True)
    link.add_argument("--replace-task", help="Re-link an adoption: name the task it is linked "
                      "to now (refused if that is no longer true)")
    seen(link)
    link.set_defaults(func=cmd_learn_link)

    ret = sub.add_parser("retire", help="Stop tracking an adoption (revised or reverted)")
    store(ret)
    pxx_dir(ret)
    ret.add_argument("key")
    ret.add_argument("--by", required=True)
    ret.add_argument("--note", required=True)
    seen(ret)
    ret.set_defaults(func=cmd_learn_retire)

    eff = sub.add_parser("efficacy", help="Observed before/after change in each adoption's "
                         "pre-registered metric (not a causal estimate)")
    sources(eff)
    store(eff)
    eff.set_defaults(func=cmd_learn_efficacy)

    spec = sub.add_parser("spec", help="(Re)write the task spec of an accepted proposal")
    store(spec)
    spec.add_argument("key")
    spec.add_argument("--task", help="Task id to put in the spec (default: the linked task)")
    spec.add_argument("--out", help="Where to write it (default: stdout; never overwrites)")
    spec.set_defaults(func=cmd_learn_spec)

    rec = sub.add_parser("recover", help="Complete an interrupted store transaction, or say "
                         "precisely why it cannot be completed")
    store(rec)
    rec.set_defaults(func=cmd_learn_recover)


# --------------------------------------------------------------------------- helpers
def _die(msg: str, code: int = 1) -> None:
    print(f"❌ {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _open_store(args: argparse.Namespace) -> Store:
    conn = os.environ.get("DX_LEARN_MSSQL")
    try:
        if conn:
            if os.environ.get("DX_LEARN_MSSQL_UNQUALIFIED") != MSSQL_ACK:
                _die("the SQL Server backend is UNQUALIFIED: it has never run against a live "
                     "server. Qualify it on a dev instance, then set "
                     f"DX_LEARN_MSSQL_UNQUALIFIED={MSSQL_ACK} to acknowledge that, or unset "
                     "DX_LEARN_MSSQL to use the JSON store")
            print("⚠ dx learn: SQL Server backend in use — UNQUALIFIED", file=sys.stderr,
                  flush=True)
            return MssqlStore(conn, acknowledge_unqualified=True)
        root = args.store_dir or os.environ.get("DX_LEARN_STORE") or str(DEFAULT_STORE)
        return JsonStore(Path(root).expanduser())
    except StoreError as exc:
        _die(str(exc))
        raise  # unreachable; keeps mypy honest


def _now(args: argparse.Namespace) -> datetime:
    return parse_iso(args.now, "--now") if getattr(args, "now", None) else utcnow()


def _pxx_state_dir(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "pxx_state_dir", None) or os.environ.get("DX_LEARN_PXX_STATE")
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


def _commit(store: Store, tx: Tx) -> None:
    try:
        store.commit(tx)
    except StaleDecision as exc:
        _die(f"{exc}")
    except StoreError as exc:
        _die(f"the store refused the write: {exc}. Nothing was recorded.")


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


def _find(recs: dict[str, Recommendation], key: str) -> Recommendation:
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


def _token(rec: Recommendation, ad: Adoption | None) -> str:
    """What a human decided on: the revisions of the recommendation and its adoption."""
    return f"{rec.revision}.{ad.revision if ad else 0}"


def _check_seen(args: argparse.Namespace, rec: Recommendation, ad: Adoption | None) -> None:
    seen = getattr(args, "seen", None)
    if seen and seen != _token(rec, ad):
        _die(f"{rec.rec_key[:8]} changed since you looked (you saw {seen}, it is now "
             f"{_token(rec, ad)}); run `dx learn show {rec.rec_key[:8]}` again. Nothing was recorded.")


def _line(r: Recommendation) -> str:
    return f"  [{r.rec_key[:8]}] {r.score:8.2f}  {r.kind:22} {r.subject}  view={r.view}"


def _guard(fn: Any) -> Any:
    """Turn a refused recovery into the precise, actionable error it carries."""
    def wrapped(args: argparse.Namespace) -> None:
        try:
            fn(args)
        except RecoveryError as exc:
            _die(str(exc))
    wrapped.__name__ = fn.__name__
    return wrapped


# --------------------------------------------------------------------------- commands
@_guard
def cmd_learn_run(args: argparse.Namespace) -> None:
    now = _now(args)
    store = _open_store(args)
    corpus = _gather(args)
    cfg = LearnConfig()
    pxx_state = None if args.no_pxx else _pxx_state_dir(args)
    mixed = mixed_provenance(corpus)

    with store.mutation_lock():
        # 1. settle forwarding left over from earlier passes or interrupted decisions
        forwards = reconcile_forwards(store, pxx_state, now) if pxx_state else []
        # 2. learn each view and write back, checked against the revisions this
        #    pass read. Ingestion is complete and shared; a view only selects
        #    which attempts its detectors see, and reconciles its own proposals.
        adoptions = store.load_adoptions()
        existing = store.load_recs()
        changed: list[Recommendation] = []
        view_reports: dict[str, ViewReport] = {}
        for view in VIEWS:
            attempts, view_reports[view] = select(corpus, view, mixed)
            view_corpus = replace(corpus, attempts=attempts)
            learner = Learner(cfg, now, view=view, policy_seams=POLICY_SEAMS)
            fresh = learner.learn(view_corpus,
                                  [a for a in adoptions.values() if a.view == view])
            changed += reconcile(fresh, existing, cfg, now, view=view)
        conflicts = store.commit(Tx("dx learn", "pass", "pass", now, writes=list(changed),
                                    audit=False), skip_conflicts=True)
        skipped = [c.split(":", 1)[1] for c in conflicts if c.startswith("rec:")]
        changed = [r for r in changed if r.rec_key not in skipped]
        # a proposal that just went STALE is withdrawn from pxx in this same pass
        if pxx_state:
            forwards = reconcile_forwards(store, pxx_state, now)
        # 3. only now, and still inside the lock, forward what is still PROPOSED
        forwarded = (forward_proposals(store, pxx_state, now)
                     if args.forward_pxx and pxx_state else [])
        refused = [f for f in forwarded if f["state"] == "REFUSED"]
        forwarded = [f for f in forwarded if f["state"] != "REFUSED"]
        stale = [r.rec_key for r in changed if r.status == "STALE"]
        summary: dict[str, Any] = {
            "sources": corpus.sources, "attempts": len(corpus.attempts),
            "tasks": len(corpus.tasks), "operator_decisions": len(corpus.operator),
            "lane_results": len(corpus.lanes),
            "quarantined": [to_jsonable(q) for q in corpus.quarantined],
            "notices": [to_jsonable(n) for n in corpus.notices],
            "views": {name: _view_summary(r) for name, r in view_reports.items()},
            "proposed": [r.rec_key for r in changed if r.status == "PROPOSED"],
            "stale": stale, "forwarded": forwarded, "forward_refused": refused,
            "skipped_decided_meanwhile": skipped,
            "divergences": [f for f in forwards if f["divergence"]]}
        store.record_pass(summary, corpus, now)

    text = ["dx learn — pass complete (recommend-only; nothing was changed)"]
    text += [f"  {k}: {v}" for k, v in sorted(corpus.sources.items())]
    if corpus.quarantined:
        text.append(f"  ⚠ {len(corpus.quarantined)} record(s) quarantined and not learned from:")
        text += [f"    - {q.source} {q.where}: {q.error}" for q in corpus.quarantined[:10]]
    if corpus.notices:
        text.append(f"  ⚠ {len(corpus.notices)} run-intent notice(s) (read conservatively; "
                    "the runs stay in the corpus):")
        text += [f"    - {n.run_id}: {n.detail}" for n in corpus.notices[:10]]
    text += _view_text(view_reports)
    open_recs = sorted((r for r in changed if r.status == "PROPOSED"), key=lambda r: -r.score)
    text.append(f"  {len(open_recs)} open proposal(s) updated, {len(stale)} gone stale")
    text += [_line(r) for r in open_recs[:10]]
    text += [f"  pxx: {f['rec_key'][:8]} {f['state']} — {f['note']}" for f in forwarded]
    text += [f"  pxx: {f['rec_key'][:8]} REFUSED — {f['note']}" for f in refused]
    text += [f"  ⚠ DIVERGED {f['rec_key'][:8]}: {f['divergence']}" for f in forwards
             if f["divergence"]]
    text += [f"  left alone (decided while this pass ran): {k[:8]}" for k in skipped]
    text.append("  next: dx learn queue · dx learn show <key> · dx learn accept <key> --by <you>")
    _emit(args, summary, text)


def _view_summary(report: ViewReport) -> dict[str, Any]:
    return {"label": VIEW_LABELS[report.view], "included": report.included,
            "excluded": report.excluded, "by_intent": report.by_intent,
            "by_provenance": report.by_provenance,
            "excluded_but_failed": [{"run_id": rid, "code": code}
                                    for rid, code in report.excluded_but_failed],
            "mixed_provenance_warnings": report.mixed_provenance}


def _view_text(reports: dict[str, ViewReport]) -> list[str]:
    text = ["  views (detector inputs; every run stays ingested and accounted in all-runs):"]
    for name, r in reports.items():
        why = ", ".join(f"{i}: {cell['excluded']} excluded"
                        for i, cell in sorted(r.by_intent.items()) if cell["excluded"])
        text.append(f"    view {name} ({VIEW_LABELS[name]}): {r.included} included, "
                    f"{r.excluded} excluded" + (f" — {why}" if why else ""))
        text += [f"      excluded-but-failed: {rid} ({code})" for rid, code in r.excluded_but_failed]
        text += [f"      ⚠ mixed provenance: {rid} — {reason} (excluded only in the "
                 "exploratory view)" for rid, reason in sorted(r.mixed_provenance.items())]
    return text


@_guard
def cmd_learn_queue(args: argparse.Namespace) -> None:
    store = _open_store(args)
    recs, ads, fwds = store.load_recs(), store.load_adoptions(), store.load_forwards()
    shown = [r for r in recs.values() if args.all or r.status == "PROPOSED"]
    shown.sort(key=lambda r: (r.status != "PROPOSED", -r.score, r.rec_key))
    active = [a for a in ads.values() if a.active]
    try:
        audit = f"decision audit: {store.verify_audit()} row(s), chain verifies"
    except StoreError as exc:
        audit = f"⚠ decision audit DOES NOT VERIFY: {exc} — no decision will be recorded"
    text = [audit]
    diverged = [f for f in fwds.values() if f.divergence]
    if diverged:
        text.append("⚠ dx and pxx humans decided differently (resolve in one tool or the other):")
        text += [f"  [{f.rec_key[:8]}] pxx {f.slug}: {f.divergence}" for f in diverged]
    superseded = [f for f in fwds.values() if f.state == "SUPERSEDED"]
    if superseded:
        text.append("⚠ forwarded proposals pxx's own cycle replaced (offered again when the slug clears):")
        text += [f"  [{f.rec_key[:8]}] pxx {f.slug}: {f.note}" for f in superseded]
    text.append("Earlier adoptions (review these before adding new ones):")
    text += [f"  [{a.rec_key[:8]}] task={a.task_id or '— not linked —'} by {a.accepted_by} "
             f"on {a.accepted_at.date()} metric={a.metric['name']}" for a in active] or ["  (none)"]
    text.append("Open proposals:" if not args.all else "All recommendations:")
    text += [_line(r) + f"  seen {_token(r, ads.get(r.rec_key))}"
             + (f"  ({r.status})" if args.all else "")
             + (f"  [pxx {fwds[r.rec_key].state}]" if r.rec_key in fwds else "")
             for r in shown] or ["  (none)"]
    _emit(args, {"adoptions": active, "recommendations": shown,
                 "forwards": list(fwds.values())}, text)


@_guard
def cmd_learn_show(args: argparse.Namespace) -> None:
    store = _open_store(args)
    rec = _find(store.load_recs(), args.key)
    fwd = store.load_forwards().get(rec.rec_key)
    ad = store.load_adoptions().get(rec.rec_key)
    text = [f"{rec.rec_key}  {rec.kind}  {rec.status}  score {rec.score}  "
            f"seen-token {_token(rec, ad)}",
            f"  view: {rec.view} ({VIEW_LABELS.get(rec.view, rec.view)})",
            f"  {rec.subject}", "", f"  {rec.draft_text}", "",
            f"  judged by: {rec.metric['name']} for {rec.metric['key']} "
            f"(source: {rec.metric.get('source', '?')})",
            f"  suggested role: {rec.suggested_role}",
            "  evidence:", json.dumps(to_jsonable(rec.evidence), indent=2, sort_keys=True)]
    if rec.decided_by:
        text.append(f"  decided: {rec.status} by {rec.decided_by} — {rec.decision_note or ''}")
    if fwd:
        text.append(f"  pxx: {fwd.slug} {fwd.state} (inbox: {fwd.pxx_view}) — {fwd.note}"
                    + (f"  ⚠ {fwd.divergence}" if fwd.divergence else ""))
    _emit(args, {"recommendation": rec, "forward": fwd, "adoption": ad,
                 "seen_token": _token(rec, ad)}, text)


@_guard
def cmd_learn_accept(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    out = Path(args.spec_out).expanduser() if args.spec_out else None
    with store.mutation_lock():
        rec = _find(store.load_recs(), args.key)
        prior = store.load_adoptions().get(rec.rec_key)
        _check_seen(args, rec, prior)
        if rec.view == EXPLORATORY:
            _die(f"{rec.rec_key[:8]} is an exploratory-view proposal ({EXPLORATORY}): "
                 "declared-intent filtering carries no decision weight — it cannot be "
                 "accepted, forwarded or turned into a task. Nothing was recorded.")
        if rec.status not in ("PROPOSED", "STALE"):
            _die(f"{rec.rec_key[:8]} is {rec.status}; only an open proposal can be accepted")
        if out is not None and (out.exists() or not out.parent.is_dir()):
            _die(f"{out} already exists or its directory does not; nothing was accepted")
        if rec.status == "STALE":
            print("⚠ this proposal is STALE: the last pass no longer saw the evidence for it",
                  file=sys.stderr, flush=True)
        updated = replace(rec, status="ACCEPTED", decided_by=by, decided_at=now,
                          decision_note=args.note, weight_at_decision=rec.evidence_weight)
        adoption = Adoption(rec_key=rec.rec_key, metric=dict(rec.metric), accepted_by=by,
                            accepted_at=now, note=args.note, task_id=args.task,
                            # The CURRENT policy at proposal time — never the
                            # genesis default policy_at() uses before the first
                            # seam (learn_views: the two are split on purpose).
                            view=rec.view, policy_version=CURRENT_POLICY[rec.view],
                            revision=prior.revision if prior else 0)
        spec = render_spec(updated, args.task or "<TASK-ID>", accepted_by=by)
        _commit(store, Tx(by, "accept", rec.rec_key, now, writes=[updated, adoption],
                          detail={"note": args.note, "task_id": args.task,
                                  "metric": rec.metric, **_who(by)}))
        # Still inside the lock. If this write is interrupted, the decision stands
        # and `dx learn spec` regenerates the spec from the stored proposal.
        _deliver_spec(spec, out, rec.rec_key, accepted=True)


def _deliver_spec(spec: str, out: Path | None, key: str, *, accepted: bool) -> None:
    if out is None:
        print(spec, flush=True)
        return
    try:
        with out.open("x", encoding="utf-8") as fh:    # never overwrite someone's spec
            fh.write(spec)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        _die(f"{'accepted ' + key[:8] + ', but ' if accepted else ''}the spec could not be "
             f"written to {out} ({exc}); write it with `dx learn spec {key[:8]} --out <path>`")
    print(f"✅ {'accepted ' + key[:8] + '; ' if accepted else ''}task spec written to {out}",
          flush=True)


@_guard
def cmd_learn_spec(args: argparse.Namespace) -> None:
    store = _open_store(args)
    rec = _find(store.load_recs(), args.key)
    if rec.view == EXPLORATORY:
        _die(f"{rec.rec_key[:8]} is an exploratory-view proposal ({EXPLORATORY}): "
             "declared-intent filtering carries no decision weight — no task spec "
             "is ever generated from it.")
    if rec.status != "ACCEPTED" or not rec.decided_by:
        _die(f"{rec.rec_key[:8]} is {rec.status}; only an accepted proposal has a spec")
        raise AssertionError  # unreachable
    ad = store.load_adoptions().get(rec.rec_key)
    task = args.task or (ad.task_id if ad else None) or "<TASK-ID>"
    _deliver_spec(render_spec(rec, task, accepted_by=rec.decided_by),
                  Path(args.out).expanduser() if args.out else None, rec.rec_key, accepted=False)


def _settle_forward(store: Store, args: argparse.Namespace, rec: Recommendation) -> None:
    """After reject/retire, inside the same lock: withdraw our pending pxx entry
    if it is still ours, and surface any decision a pxx human already made."""
    if rec.rec_key not in store.load_forwards():
        return
    try:
        state_dir = _pxx_state_dir(args)
    except Exception as exc:   # pxx config unreadable: the next `dx learn run` settles it
        print(f"   pxx state dir unavailable ({exc}); the next `dx learn run` will withdraw "
              "the forwarded entry", flush=True)
        return
    for row in reconcile_forwards(store, state_dir, utcnow(), only=rec.rec_key):
        print(f"   pxx {row['slug']}: {row['state']} (inbox: {row['pxx_view']}) — {row['note']}",
              flush=True)
        if row["divergence"]:
            print(f"   ⚠ DIVERGED: {row['divergence']} — resolve it with "
                  f"`pxx improve triage` or here", flush=True)


@_guard
def cmd_learn_reject(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    with store.mutation_lock():
        rec = _find(store.load_recs(), args.key)
        _check_seen(args, rec, store.load_adoptions().get(rec.rec_key))
        if rec.status not in ("PROPOSED", "STALE"):
            _die(f"{rec.rec_key[:8]} is {rec.status}; only an open proposal can be rejected")
        updated = replace(rec, status="REJECTED", decided_by=by, decided_at=now,
                          decision_note=args.note, weight_at_decision=rec.evidence_weight)
        _commit(store, Tx(by, "reject", rec.rec_key, now, writes=[updated],
                          detail={"note": args.note, **_who(by)}))
        print(f"✅ rejected {rec.rec_key[:8]}; it returns only if its evidence grows "
              f"{LearnConfig().reproposal_growth:g}x", flush=True)
        _settle_forward(store, args, updated)


@_guard
def cmd_learn_link(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    with store.mutation_lock():
        rec = _find(store.load_recs(), args.key)
        ad = store.load_adoptions().get(rec.rec_key)
        _check_seen(args, rec, ad)
        if ad is None or not ad.active:
            _die(f"{rec.rec_key[:8]} has no active adoption")
            raise AssertionError  # unreachable
        # Linking is first-writer-wins: a second link (even from a concurrent
        # command that re-read under the lock) must name what it replaces.
        if ad.task_id == args.task:
            _die(f"{rec.rec_key[:8]} is already linked to {args.task}; nothing was recorded")
        if ad.task_id is not None and args.replace_task != ad.task_id:
            _die(f"{rec.rec_key[:8]} is already linked to {ad.task_id}; to change it, pass "
                 f"--replace-task {ad.task_id}. Nothing was recorded.")
        if ad.task_id is None and args.replace_task:
            _die(f"{rec.rec_key[:8]} is not linked to {args.replace_task}; nothing was recorded")
        updated = replace(ad, task_id=args.task)
        _commit(store, Tx(by, "link", rec.rec_key, now, writes=[updated],
                          detail={"task_id": args.task, "previous_task_id": ad.task_id,
                                  **_who(by)}))
    print(f"✅ {rec.rec_key[:8]} is implemented by {args.task}; the before/after comparison "
          "starts at its first MERGED row after acceptance", flush=True)


@_guard
def cmd_learn_retire(args: argparse.Namespace) -> None:
    by, now = _human(args.by), utcnow()
    store = _open_store(args)
    with store.mutation_lock():
        rec = _find(store.load_recs(), args.key)
        ad = store.load_adoptions().get(rec.rec_key)
        _check_seen(args, rec, ad)
        if ad is None or not ad.active:
            _die(f"{rec.rec_key[:8]} has no active adoption")
            raise AssertionError  # unreachable
        new_ad = replace(ad, active=False, retired_at=now)
        new_rec = replace(rec, status="RETIRED", decided_by=by, decided_at=now,
                          decision_note=args.note)
        _commit(store, Tx(by, "retire", rec.rec_key, now, writes=[new_rec, new_ad],
                          detail={"note": args.note, **_who(by)}))
        print(f"✅ retired {rec.rec_key[:8]}", flush=True)
        _settle_forward(store, args, new_rec)


@_guard
def cmd_learn_efficacy(args: argparse.Namespace) -> None:
    now = _now(args)
    store = _open_store(args)
    every = list(store.load_adoptions().values())     # retired ones still confound
    corpus = _gather(args)
    mixed = mixed_provenance(corpus)
    rows = []
    # Each adoption is measured under the identical view and policy its metric
    # was pre-registered under; a policy seam across the windows voids the
    # comparison (inconclusive (policy-changed)), never a silent cross-seam read.
    for view in VIEWS:
        view_every = [a for a in every if a.view == view]
        ads = [a for a in view_every if a.active]
        if not ads:
            continue
        attempts, _ = select(corpus, view, mixed)
        view_corpus = replace(corpus, attempts=attempts)
        rows += Learner(LearnConfig(), now, view=view,
                        policy_seams=POLICY_SEAMS).efficacy(view_corpus, ads, view_every)
    payload = [{"rec_key": r["adoption"].rec_key, "task_id": r["adoption"].task_id,
                "view": r["adoption"].view, "policy": f"{r['adoption'].view}@"
                                                      f"{r['adoption'].policy_version}",
                "metric": r["adoption"].metric, "status": r["status"],
                "before": None if r["before"] is None else round(r["before"].mean, 4),
                "after": None if r["after"] is None else round(r["after"].mean, 4),
                "runs_before": r["runs_before"], "runs_after": r["runs_after"],
                "observed_change": r["observed_change"],
                "p_rate_fell": None if r["p_rate_fell"] is None else round(r["p_rate_fell"], 4),
                "caveats": r["caveats"]}
               for r in rows]
    text = ["Observed before/after change in each adoption's metric (bad-event rates; lower is "
            "better). Correlational: not proof the change caused it."]
    for p in payload:
        rates = (f" {p['before']:.0%} → {p['after']:.0%}"
                 if p["before"] is not None and p["after"] is not None else "")
        text.append(f"  [{p['rec_key'][:8]}] {p['status']:<26} task={p['task_id'] or '—'}"
                    f"{rates}  {p['metric']['name']}  view={p['view']} policy={p['policy']}")
        text += [f"      · {c}" for c in p["caveats"][1:]]
    if not payload:
        text.append("  (no active adoptions)")
    _emit(args, payload, text)


def cmd_learn_recover(args: argparse.Namespace) -> None:
    store = _open_store(args)
    try:
        print(f"✅ {store.recover()}", flush=True)
    except RecoveryError as exc:
        _die(str(exc))
