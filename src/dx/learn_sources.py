"""Readers for the four records dx learn joins. Fail-closed, read-only.

Each reader either returns verified data or refuses. The rules differ by how
much each record can be trusted:

* The devswarm ledger is hash-chained. dx's ``get_ledger_head`` (the ledger's
  own ``tools/verify_chain.py``) must pass first; rows are then re-hashed while
  reading and only rows up to that verified head are used — a row appended
  after verification is not trusted. The public reference ledger is refused
  (by directory name *or* by its published genesis row) unless explicitly
  allowed. A CORRECTION row sets aside the task of the row whose hash it
  names; a correction that names no known row sets aside its own task, and
  one that cannot be attributed at all stops the pass.
* The psoperator audit log is re-verified with psoperator's own rule. A broken
  chain stops the pass.
* pxx run directories are best-effort telemetry with no chain. ``pxx loop``
  (what ``dx run`` drives) writes one directory for the loop — the true
  terminal code and the loop's gates, no manifest — plus one directory per
  round. One loop is one attempt: its rounds are folded in, never counted as
  attempts of their own, because a round can say COMPLETED inside a loop that
  ended ROUND_CAP. Model, task and project come from the loop's own
  ``session_start`` events. A standalone session (``pxx run``) is one attempt.
  A malformed directory is quarantined and named; one without
  ``outcome.json`` is still in flight and is counted, not guessed at.
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
from dataclasses import dataclass, field
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
# executed/done stay in as the good outcomes rates are measured against.
# kill-switched is excluded because drills write the identical row (psoperator
# has no drill marker yet), so a real stop and a drill cannot be told apart.
_OPERATOR_IGNORED = frozenset({"dry-run", "kill-switched"})
_OPERATOR_BAD = frozenset({"denied", "rejected-stale", "rejected-target", "execution-failed", "failed"})

# EXECUTED evidence is `key=value` pairs, but it also embeds `subject="..."`,
# which is built from model-written text. Quoted spans are removed before
# parsing so text inside them can never supply scope= or run=.
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_KV = re.compile(r"(?:^|\s)(run|scope)=(\S+)")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
# The published reference ledger's genesis says this of itself (SCHEMA.md).
_REFERENCE_GENESIS_MARK = "no real work is attested"

# pxx gate_decision events that are endpoint-resilience notifications, not gate
# verdicts (pxx backends/native.py): they never carry `allowed`. Exactly these
# kinds may lack a verdict; any other gate_decision without one is malformed.
_NON_VERDICT_GATES = frozenset({"transport_retry", "fallback"})


class LearnSourceError(Exception):
    """A record dx learn may not learn from. Names the record and the fix."""


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


# --------------------------------------------------------------------------- pxx runs
@dataclass
class _Events:
    gates: list[GateObs] = field(default_factory=list)
    new_failures: list[str] = field(default_factory=list)
    session_starts: list[dict[str, Any]] = field(default_factory=list)


def _read_events(run_dir: Path) -> _Events:
    out = _Events()
    path = run_dir / "events.jsonl"
    if not path.exists():
        return out
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        ev = json.loads(line)
        if not isinstance(ev, dict):
            raise ValueError(f"events.jsonl line {n}: not a JSON object")
        data = ev.get("data")
        if ev.get("kind") == "session_start" and isinstance(data, dict):
            out.session_starts.append(data)
        if ev.get("kind") != "gate_decision":
            continue
        if not isinstance(data, dict) or not isinstance(data.get("gate"), str):
            raise ValueError(f"events.jsonl line {n}: malformed gate_decision")
        if "allowed" not in data:
            if data["gate"] not in _NON_VERDICT_GATES:
                raise ValueError(f"events.jsonl line {n}: gate_decision without a verdict")
            # A known endpoint-resilience notification: no verdict, so no gate
            # observation — and it must not cost the run (see _NON_VERDICT_GATES).
            continue
        if not isinstance(data["allowed"], bool):
            raise ValueError(f"events.jsonl line {n}: malformed gate_decision")
        out.gates.append(GateObs(data["gate"], data["allowed"]))
        failures = data.get("new_failures", [])
        if not isinstance(failures, list) or not all(isinstance(t, str) for t in failures):
            raise ValueError(f"events.jsonl line {n}: new_failures must be a list of strings")
        out.new_failures += [t for t in failures if t]
    return out


def _is_loop(run_dir: Path, task: dict[str, Any]) -> bool:
    return task.get("mode") == "loop" or "-loop-" in run_dir.name


def _round_ids_leniently(run_dir: Path) -> set[str]:
    """Round ids from a loop's session_start events, line by line, tolerating
    damage elsewhere in the file. Used so that a loop quarantined for a bad
    line still claims its rounds: otherwise each round — which may say
    COMPLETED inside a loop that failed — would be counted as a success."""
    found: set[str] = set()
    path = run_dir / "events.jsonl"
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("kind") == "session_start" \
                and isinstance(ev.get("data"), dict) and isinstance(ev["data"].get("run_id"), str):
            found.add(ev["data"]["run_id"])
    return found


def read_pxx_runs(state_dir: Path, corpus: Corpus) -> None:
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        raise LearnSourceError(
            f"no pxx runs directory at {runs_dir}; pass --pxx-state-dir or set "
            "DX_LEARN_PXX_STATE, or exclude the source with --no-pxx")
    dirs = sorted(p for p in runs_dir.iterdir() if p.is_dir())

    # Pass 1: every loop's rounds, whether or not the loop has finished — a
    # round of an in-flight loop must not be mistaken for a standalone run.
    loops: dict[Path, tuple[dict[str, Any], _Events]] = {}
    rounds: set[str] = set()
    for d in dirs:
        try:
            task = _json_obj(d / "task.json") if (d / "task.json").exists() else {}
        except (OSError, ValueError) as exc:
            if "-loop-" in d.name:
                rounds |= _round_ids_leniently(d)
                corpus.quarantined.append(Quarantined("pxx", str(d), str(exc)))
            continue   # a non-loop dir with a bad task.json is quarantined in pass 2
        if not _is_loop(d, task):
            continue
        rounds |= _round_ids_leniently(d)
        try:
            loops[d] = (task, _read_events(d))
        except (OSError, ValueError, TypeError) as exc:
            corpus.quarantined.append(Quarantined("pxx", str(d), str(exc)))

    in_flight = folded = 0
    for d in dirs:
        if d.name in rounds:
            folded += 1
            continue
        if not (d / "outcome.json").exists():
            in_flight += 1
            continue
        if d not in loops and ("-loop-" in d.name or any(q.where == str(d) for q in corpus.quarantined)):
            continue   # a loop dir already quarantined in pass 1
        try:
            if d in loops:
                attempt = _loop_attempt(d, *loops[d], corpus)
            else:
                attempt = _session_attempt(d, corpus)
            corpus.attempts.append(attempt)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            corpus.quarantined.append(Quarantined("pxx", str(d), str(exc)))
    corpus.sources["pxx"] = (f"{runs_dir}: {len(corpus.attempts)} finished attempts "
                             f"({len(loops)} loops, {folded} loop rounds folded in), "
                             f"{in_flight} without outcome.json (skipped)")


def _signatures(code: str, summary: str, new_failures: list[str], corpus: Corpus) -> tuple[str, ...]:
    sigs: list[str] = []
    if code != "COMPLETED":
        sig = fingerprint(code, summary or code)
        corpus.signatures.setdefault(sig.signature_id, sig)
        sigs.append(sig.signature_id)
    for test_id in new_failures:          # a test that newly fails is its own signature
        tsig = fingerprint("tests", test_id)
        corpus.signatures.setdefault(tsig.signature_id, tsig)
        if tsig.signature_id not in sigs:
            sigs.append(tsig.signature_id)
    return tuple(sigs)


def _outcome_fields(outcome: dict[str, Any], run_dir: Path) -> tuple[str, str, tuple[str, ...]]:
    run_id = _str(outcome, "run_id", "outcome.json")
    if run_id != run_dir.name:
        raise ValueError(f"outcome.json run_id {run_id!r} does not match its directory")
    contributing = outcome.get("contributing_codes") or []
    if not isinstance(contributing, list) or not all(isinstance(c, str) for c in contributing):
        raise ValueError("outcome.json: contributing_codes must be a list of strings")
    return run_id, _str(outcome, "code", "outcome.json"), tuple(contributing)


def _loop_attempt(run_dir: Path, task: dict[str, Any], ev: _Events, corpus: Corpus) -> Attempt:
    outcome = _json_obj(run_dir / "outcome.json")
    run_id, code, contributing = _outcome_fields(outcome, run_dir)
    if not ev.session_starts:
        raise ValueError("loop recorded no session_start: model and task are unknown")
    first = ev.session_starts[0]
    models = {f"{s.get('provider') or '?'}/{s.get('model') or '?'}" for s in ev.session_starts}
    if len(models) != 1:
        raise ValueError(f"loop rounds ran on different models {sorted(models)}; "
                         "no single variant to credit")
    return Attempt(
        run_id=run_id, ts=from_epoch(outcome.get("ts"), "outcome.json ts"),
        task_key=_str(first, "task_id", "session_start"),
        task_class=_str(first, "project", "session_start"),
        model=models.pop(), agent_version_id=str(first.get("agent_version_id") or ""),
        code=code, contributing_codes=contributing, gates=tuple(ev.gates),
        signatures=_signatures(code, str(outcome.get("summary") or ""), ev.new_failures, corpus))


def _session_attempt(run_dir: Path, corpus: Corpus) -> Attempt:
    outcome = _json_obj(run_dir / "outcome.json")
    task = _json_obj(run_dir / "task.json")
    manifest = _json_obj(run_dir / "manifest.json")
    run_id, code, contributing = _outcome_fields(outcome, run_dir)
    ev = _read_events(run_dir)
    return Attempt(
        run_id=run_id, ts=from_epoch(outcome.get("ts", task.get("ts")), "outcome.json ts"),
        task_key=_str(task, "task_id", "task.json"), task_class=_str(task, "project", "task.json"),
        model=f"{manifest.get('provider') or '?'}/{_str(manifest, 'model', 'manifest.json')}",
        agent_version_id=str(outcome.get("agent_version_id") or ""),
        code=code, contributing_codes=contributing, gates=tuple(ev.gates),
        signatures=_signatures(code, str(outcome.get("summary") or ""), ev.new_failures, corpus))


# --------------------------------------------------------------------------- devswarm ledger
def _canonical_hash(row: dict[str, Any]) -> str:
    body = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()


def read_ledger(ledger_repo: Path, corpus: Corpus, *, allow_reference: bool = False) -> None:
    refusal = (f"{ledger_repo} is the public reference ledger; its rows attest to no real "
               "work. Point DX_LEDGER_REPO at your ledger, pass --no-ledger, or pass "
               "--allow-reference-ledger for a demo")
    if is_reference_ledger(ledger_repo) and not allow_reference:
        raise LearnSourceError(refusal)
    try:
        head = get_ledger_head(ledger_repo)   # runs verify_chain.py; raises on any break
    except LedgerError as exc:
        raise LearnSourceError(f"ledger chain did not verify, refusing to learn from it: {exc}") from exc

    # Re-hash while reading and stop at the verified head: rows appended since
    # verification (or a torn write in progress) are never trusted.
    rows: list[dict[str, Any]] = []
    prev = "0" * 64
    reached = False
    for n, line in enumerate((ledger_repo / "ledger.jsonl").read_text(encoding="utf-8")
                             .splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LearnSourceError(f"ledger line {n} is not JSON before the verified head") from exc
        if not isinstance(row, dict) or row.get("prev_hash") != prev:
            raise LearnSourceError(f"ledger line {n} does not chain to the row before it")
        prev = _canonical_hash(row)
        rows.append(row)
        if prev == head:
            reached = True
            break
    if not reached:
        raise LearnSourceError(f"the verified head {head[:16]}… is not a row of ledger.jsonl")
    if rows and rows[0].get("action") == "GENESIS" \
            and _REFERENCE_GENESIS_MARK in str(rows[0].get("evidence") or "") and not allow_reference:
        raise LearnSourceError(refusal + " (recognised by its genesis row)")
    corpus.ledger_head = head

    hash_to_task: dict[str, str] = {}
    set_aside: dict[str, str] = {}
    for n, row in enumerate(rows, 1):
        row_hash = _canonical_hash(row)
        action, task_id = row.get("action"), row.get("task_id")
        raw_evidence = row.get("evidence")
        evidence: str = raw_evidence if isinstance(raw_evidence, str) else ""
        if action == "CORRECTION":
            named = [hash_to_task[h] for h in _HEX64.findall(evidence) if h in hash_to_task]
            if named:
                for t in named:
                    set_aside[t] = f"a CORRECTION (line {n}) names one of its rows"
            elif isinstance(task_id, str) and task_id:
                set_aside[task_id] = f"a CORRECTION (line {n}) names no known row hash"
            else:
                raise LearnSourceError(
                    f"ledger line {n}: CORRECTION names neither a known row hash nor a task; "
                    "dx learn cannot tell what it corrects, so it will not learn from this ledger")
            continue
        if not isinstance(task_id, str) or not task_id:
            continue
        hash_to_task[row_hash] = task_id
        if action in ("GENESIS", "ANCHOR"):
            continue
        try:
            ts = parse_iso(row.get("ts"), f"ledger line {n}")
        except ValueError as exc:
            set_aside[task_id] = str(exc)
            continue
        rec = corpus.tasks.setdefault(task_id, TaskRecord(task_id))
        sig_id = None
        if action in NEGATIVE_ACTIONS:
            sig = fingerprint(str(action), evidence or str(action))
            corpus.signatures.setdefault(sig.signature_id, sig)
            sig_id = sig.signature_id
        if action == "EXECUTED":
            pairs = _KV.findall(_QUOTED.sub(" ", evidence))
            runs = [v for k, v in pairs if k == "run"]
            scopes = [v for k, v in pairs if k == "scope"]
            if len(runs) > 1 or len(scopes) > 1 or (runs and not _RUN_ID.match(runs[0])):
                set_aside[task_id] = f"EXECUTED evidence at line {n} has ambiguous run=/scope="
            else:
                rec.run_ids += runs
                rec.scope = scopes[0] if scopes else rec.scope
        rec.events.append(LedgerEvent(ts, task_id, str(action), evidence,
                                      row.get("author_human"), row.get("reviewer_seat"), sig_id))

    for task_id, why in sorted(set_aside.items()):
        corpus.tasks.pop(task_id, None)
        corpus.quarantined.append(Quarantined("ledger", task_id, f"set aside: {why}"))
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
        except (ValueError, TypeError, OverflowError) as exc:
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
