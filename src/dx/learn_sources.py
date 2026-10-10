"""Readers for the four records dx learn joins. Fail-closed, read-only.

Each reader either returns verified data or refuses. The rules differ by how
much each record can be trusted, because they are not equally trustworthy:

* The devswarm ledger is hash-chained. dx's own ``get_ledger_head`` (which
  runs the ledger's ``tools/verify_chain.py``) must pass before a single row
  is read; a broken chain stops the whole pass. The public reference ledger is
  refused unless explicitly allowed, because its rows attest to no real work
  and learning from them would be learning fiction. Tasks carrying a
  CORRECTION row are set aside: which earlier row the correction supersedes is
  a judgment no parser should guess.
* The psoperator audit log is hash-chained too, and is re-verified here with
  psoperator's own rule before use. A broken chain stops the pass.
* pxx run directories are best-effort telemetry with no chain. A run whose
  files are malformed is quarantined (named in the report) and skipped; a
  run without ``outcome.json`` is still in flight or crashed and is counted,
  not guessed at.
* Review manifests (``review/<release>/manifest.tsv``) are plain TSV. A row
  with an unknown result is quarantined.

Nothing here writes anywhere.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .learn_model import (
    LANE_RESULTS,
    NEGATIVE_ACTIONS,
    Attempt,
    Corpus,
    GateObs,
    LaneResult,
    LedgerEvent,
    OperatorDecision,
    Quarantined,
    TaskRecord,
    from_epoch,
    parse_iso,
)
from .learn_signature import fingerprint
from .ledger_state import is_reference_ledger
from .ledger_utils import LedgerError, get_ledger_head

GIT_TIMEOUT_S = 15

# psoperator decisions that say nothing about how well the agent worked;
# executed/done stay in as the good outcomes that rates are measured against.
# kill-switched is excluded because drills write the identical row (psoperator
# has no drill marker yet), so a real stop and a drill cannot be told apart.
_OPERATOR_IGNORED = frozenset({"dry-run", "kill-switched"})
_OPERATOR_BAD = frozenset({"denied", "rejected-stale", "rejected-target", "execution-failed", "failed"})
_KV = re.compile(r"(?:^|\s)(run|model|pxx|identity|scope)=(\S+)")


class LearnSourceError(Exception):
    """A record dx learn may not learn from. Names the record and the fix."""


# --------------------------------------------------------------------------- pxx runs
def _json_obj(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def _str(d: dict[str, Any], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return v


def read_pxx_runs(state_dir: Path, corpus: Corpus) -> None:
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        raise LearnSourceError(
            f"no pxx runs directory at {runs_dir}; pass --pxx-state-dir or set "
            "DX_LEARN_PXX_STATE, or exclude the source with --no-pxx")
    in_flight = 0
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        if not (run_dir / "outcome.json").exists():
            in_flight += 1
            continue
        try:
            corpus.attempts.append(_read_attempt(run_dir, corpus))
        except (OSError, ValueError, TypeError) as exc:
            corpus.quarantined.append(Quarantined("pxx", str(run_dir), str(exc)))
    corpus.sources["pxx"] = (f"{runs_dir}: {len(corpus.attempts)} finished runs, "
                             f"{in_flight} without outcome.json (skipped)")


def _read_attempt(run_dir: Path, corpus: Corpus) -> Attempt:
    outcome = _json_obj(run_dir / "outcome.json")
    task = _json_obj(run_dir / "task.json")
    manifest = _json_obj(run_dir / "manifest.json")
    run_id = _str(outcome, "run_id", "outcome.json")
    if run_id != run_dir.name:
        raise ValueError(f"outcome.json run_id {run_id!r} does not match its directory")
    code = _str(outcome, "code", "outcome.json")
    model = f"{manifest.get('provider') or '?'}/{_str(manifest, 'model', 'manifest.json')}"
    contributing = outcome.get("contributing_codes") or []
    if not isinstance(contributing, list) or not all(isinstance(c, str) for c in contributing):
        raise ValueError("outcome.json: contributing_codes must be a list of strings")

    sigs: list[str] = []
    if code != "COMPLETED":
        sig = fingerprint(code, str(outcome.get("summary") or code))
        corpus.signatures.setdefault(sig.signature_id, sig)
        sigs.append(sig.signature_id)

    gates: list[GateObs] = []
    events = run_dir / "events.jsonl"
    if events.exists():
        for n, line in enumerate(events.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            ev = json.loads(line)
            if not isinstance(ev, dict) or ev.get("kind") != "gate_decision":
                continue
            data = ev.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("gate"), str) \
                    or not isinstance(data.get("allowed"), bool):
                raise ValueError(f"events.jsonl line {n}: malformed gate_decision")
            gates.append(GateObs(data["gate"], data["allowed"]))
            # A test that newly fails is its own recurring-defect signature.
            for test_id in data.get("new_failures") or []:
                if isinstance(test_id, str) and test_id:
                    tsig = fingerprint("tests", test_id)
                    corpus.signatures.setdefault(tsig.signature_id, tsig)
                    if tsig.signature_id not in sigs:
                        sigs.append(tsig.signature_id)

    return Attempt(
        run_id=run_id, ts=from_epoch(outcome.get("ts", task.get("ts")), "outcome.json ts"),
        task_key=_str(task, "task_id", "task.json"), task_class=_str(task, "project", "task.json"),
        model=model, agent_version_id=str(outcome.get("agent_version_id") or ""),
        code=code, contributing_codes=tuple(contributing), gates=tuple(gates),
        signatures=tuple(sigs))


# --------------------------------------------------------------------------- devswarm ledger
def read_ledger(ledger_repo: Path, corpus: Corpus, *, allow_reference: bool = False) -> None:
    if is_reference_ledger(ledger_repo) and not allow_reference:
        raise LearnSourceError(
            f"{ledger_repo} is the public reference ledger; its rows attest to no real "
            "work. Point DX_LEDGER_REPO at your ledger, pass --no-ledger, or pass "
            "--allow-reference-ledger for a demo")
    try:
        head = get_ledger_head(ledger_repo)   # runs verify_chain.py; raises on any break
    except LedgerError as exc:
        raise LearnSourceError(f"ledger chain did not verify, refusing to learn from it: {exc}") from exc
    corpus.ledger_head = head

    corrected: set[str] = set()
    rows = (ledger_repo / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    for n, line in enumerate(rows, 1):
        row = json.loads(line)   # verify_chain already proved every line parses
        action, task_id = row.get("action"), row.get("task_id")
        if action in ("GENESIS", "ANCHOR") or not isinstance(task_id, str):
            continue
        if action == "CORRECTION":
            corrected.add(task_id)
            continue
        try:
            ts = parse_iso(row.get("ts"), f"ledger line {n}")
        except ValueError as exc:
            corpus.quarantined.append(Quarantined("ledger", f"line {n}", str(exc)))
            corrected.add(task_id)
            continue
        evidence = row.get("evidence") if isinstance(row.get("evidence"), str) else ""
        rec = corpus.tasks.setdefault(task_id, TaskRecord(task_id))
        sig_id = None
        if action in NEGATIVE_ACTIONS:
            sig = fingerprint(str(action), evidence or str(action))
            corpus.signatures.setdefault(sig.signature_id, sig)
            sig_id = sig.signature_id
        if action == "EXECUTED":
            kv = dict(_KV.findall(evidence))
            if kv.get("run"):
                rec.run_ids.append(kv["run"])
            if kv.get("scope"):
                rec.scope = kv["scope"]
        rec.events.append(LedgerEvent(ts, task_id, str(action), evidence,
                                      row.get("author_human"), row.get("reviewer_seat"), sig_id))

    for task_id in sorted(corrected):
        if corpus.tasks.pop(task_id, None) is not None:
            corpus.quarantined.append(Quarantined(
                "ledger", task_id, "task carries a CORRECTION row; set aside rather than "
                "guess which earlier row it supersedes"))
    corpus.sources["ledger"] = f"{ledger_repo}: head {head[:16]}…, {len(corpus.tasks)} tasks"


def link_attempts(corpus: Corpus) -> int:
    """Tie pxx attempts to dx tasks through EXECUTED run= ids. Returns links made."""
    by_run = {rid: t for t in corpus.tasks.values() for rid in t.run_ids}
    linked = 0
    for i, a in enumerate(corpus.attempts):
        task = by_run.get(a.run_id)
        if task is None:
            continue
        corpus.attempts[i] = Attempt(
            run_id=a.run_id, ts=a.ts, task_key=task.task_id,
            task_class=task.scope or a.task_class, model=a.model,
            agent_version_id=a.agent_version_id, code=a.code,
            contributing_codes=a.contributing_codes, gates=a.gates,
            signatures=a.signatures, dx_task_id=task.task_id)
        linked += 1
    return linked


# --------------------------------------------------------------------------- psoperator audit
_GENESIS = "0" * 64


def _operator_hash(entry: dict[str, Any]) -> str:
    # psoperator/gatekeeper/audit.py: sha256 over json.dumps(sort_keys=True) minus "hash".
    body = json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def read_operator_audit(path: Path, corpus: Corpus) -> None:
    if not path.is_file():
        raise LearnSourceError(f"psoperator audit log not found at {path}")
    prev = _GENESIS
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for i, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LearnSourceError(f"{path} line {i}: invalid JSON; chain unverifiable") from exc
        if not isinstance(entry, dict) or entry.get("prev_hash") != prev:
            raise LearnSourceError(f"{path} line {i}: prev_hash chain broken")
        if entry.get("hash") != _operator_hash(entry):
            raise LearnSourceError(f"{path} line {i}: content hash mismatch (tampered?)")
        if entry.get("seq") != i:
            raise LearnSourceError(f"{path} line {i}: sequence gap (deleted entry?)")
        prev = entry["hash"]
        entries.append(entry)

    for e in entries:
        decision = e.get("decision")
        if not isinstance(decision, str) or decision.startswith("ENVELOPE_") \
                or decision in _OPERATOR_IGNORED:
            continue
        raw_action = e.get("action")
        action: dict[str, Any] = raw_action if isinstance(raw_action, dict) else {}
        kind = str(action.get("type") or action.get("kind") or "unknown")
        try:
            ts = from_epoch(e.get("ts"), f"audit seq {e.get('seq')}")
            tier = int(e.get("tier", -1))
        except (ValueError, TypeError) as exc:
            corpus.quarantined.append(Quarantined("operator", f"seq {e.get('seq')}", str(exc)))
            continue
        sig_id = None
        if decision in _OPERATOR_BAD:
            sig = fingerprint(f"operator:{decision}", f"{kind}: {e.get('reason') or decision}")
            corpus.signatures.setdefault(sig.signature_id, sig)
            sig_id = sig.signature_id
        corpus.operator.append(OperatorDecision(int(e["seq"]), ts, decision, tier,
                                                str(e.get("approver") or ""), kind, sig_id))
    corpus.sources["operator"] = f"{path}: {len(lines)} rows verified, {len(corpus.operator)} used"


# --------------------------------------------------------------------------- review manifests
def _commit_time(path: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(path.parent), "log", "-1", "--format=%cI", "--",
                              path.name], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def read_review_manifests(review_dir: Path, corpus: Corpus) -> None:
    manifests = sorted(review_dir.glob("*/manifest.tsv"))
    if not manifests:
        raise LearnSourceError(f"no <release>/manifest.tsv under {review_dir}")
    count = 0
    for m in manifests:
        stamp = _commit_time(m)
        ts = parse_iso(stamp, str(m)) if stamp else None
        with m.open(encoding="utf-8", newline="") as fh:
            for n, row in enumerate(csv.DictReader(fh, delimiter="\t"), 2):
                result, lane, rid = row.get("result"), row.get("lane"), row.get("id")
                if result not in LANE_RESULTS or not lane or not rid:
                    corpus.quarantined.append(Quarantined(
                        "review", f"{m}:{n}", f"unusable row (lane={lane!r} result={result!r})"))
                    continue
                corpus.lanes.append(LaneResult(m.parent.name, rid, lane, result, ts))
                count += 1
    corpus.sources.setdefault("review", "")
    corpus.sources["review"] += f"{review_dir}: {count} lane results in {len(manifests)} manifests; "
