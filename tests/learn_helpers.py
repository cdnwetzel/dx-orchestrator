"""Shared fixtures for the dx learn tests: pxx run dirs in pxx's real layout,
disposable ledgers with a real chain verifier, psoperator audit chains, fakes.

Per CONTRIBUTING: a gate without a test is a claim. dx learn's gates are the
fail-closed source readers, the human-only decisions, and the rule that a
rejected proposal stays down. Each has a test for the way it could wrongly
pass, not only the way it correctly refuses.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dx.cli import build_parser
from dx.learn_engine import LearnConfig, Learner
from dx.learn_model import Adoption, Corpus
from dx.learn_sources import (
    read_pxx_runs,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


# --------------------------------------------------------------------------- fixtures: pxx
class PxxState:
    """Writes run directories in the shape pxx actually produces (checked against
    real `pxx.loop.run_loop` output in TestPxxContract). By default a run is a
    `pxx loop` — what `dx run` drives: a `*-loop-*` directory holding the loop's
    true outcome and gates (no manifest), plus one directory per round whose own
    outcome may say COMPLETED even when the loop failed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "runs").mkdir(parents=True, exist_ok=True)
        self.n = 0

    def _write(self, d: Path, files: dict[str, Any], events: list[dict[str, Any]]) -> None:
        d.mkdir()
        for name, body in files.items():
            (d / name).write_text(json.dumps(body))
        with (d / "events.jsonl").open("w") as fh:
            for e in events:
                fh.write(json.dumps({"session_id": "s", "seq": 1, "ts": 0, **e}) + "\n")

    def run(self, *, when: datetime, project: str = "svc-a", task: str | None = None,
            model: str = "glm", code: str = "COMPLETED", summary: str = "",
            gates: list[dict[str, Any]] | None = None, contributing: list[str] | None = None,
            loop: bool = True, round_code: str = "COMPLETED") -> str:
        self.n += 1
        ts = when.timestamp()
        task_id = task or f"task-{self.n}"
        round_id = f"20261001T000000Z-{self.n:08x}"
        start = {"kind": "session_start", "data": {
            "run_id": round_id, "provider": "local", "model": model, "project": project,
            "task_id": task_id, "agent_version_id": "a" * 16, "backend": "native"}}
        gate_events = [{"kind": "gate_decision", "data": g} for g in gates or []]
        session_files = {
            "outcome.json": {"run_id": round_id, "code": round_code if loop else code,
                             "summary": "" if loop else summary, "ts": ts,
                             "contributing_codes": [] if loop else contributing or [],
                             "agent_version_id": "a" * 16},
            "task.json": {"run_id": round_id, "task_id": task_id, "project": project, "ts": ts},
            "manifest.json": {"provider": "local", "model": model}}
        if not loop:
            self._write(self.root / "runs" / round_id, session_files, [start, *gate_events])
            return round_id
        loop_id = f"20261001T000000Z-loop-{self.n:08x}"
        self._write(self.root / "runs" / round_id, session_files, [start])
        self._write(self.root / "runs" / loop_id, {
            "task.json": {"mode": "loop", "run_id": loop_id, "task_preview": "t", "root": "/w"},
            "outcome.json": {"mode": "loop", "run_id": loop_id, "code": code, "summary": summary,
                             "ts": ts, "contributing_codes": contributing or []}},
            [{"kind": "run_created", "data": {"project": project}}, start, *gate_events])
        return loop_id


@pytest.fixture
def pxx(tmp_path: Path) -> PxxState:
    return PxxState(tmp_path / "pxx-state")


# --------------------------------------------------------------------------- fixtures: ledger
_VERIFIER = '''import hashlib, json, sys
prev = "0" * 64
n = 0
for i, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    row = json.loads(line)
    if row.get("prev_hash") != prev:
        print(f"STOP: chain broken at line {i}")
        sys.exit(1)
    prev = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()
    n += 1
print(f"OK: {n} row(s) verified. Ledger head hash: {prev}")
'''


def _canon(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def write_ledger(repo: Path, rows: list[dict[str, Any]]) -> Path:
    (repo / "tools").mkdir(parents=True, exist_ok=True)
    (repo / "tools" / "verify_chain.py").write_text(_VERIFIER)
    prev, out = "0" * 64, []
    base = {"author_human": "Ada Author", "author_seat": "S-Dev", "reviewer_seat": None,
            "sha": None, "evidence": ""}
    for r in [{"action": "GENESIS", "task_id": "GENESIS", "ts": "2026-01-01T00:00:00Z"}, *rows]:
        full = {**base, **r, "prev_hash": prev}
        out.append(_canon(full))
        prev = hashlib.sha256(_canon(full).encode()).hexdigest()
    (repo / "ledger.jsonl").write_text("".join(line + "\n" for line in out))
    return repo


def row(task: str, action: str, when: datetime, evidence: str = "") -> dict[str, Any]:
    return {"task_id": task, "action": action, "evidence": evidence,
            "ts": when.strftime("%Y-%m-%dT%H:%M:%SZ")}


# --------------------------------------------------------------------------- fixtures: psoperator
def write_audit(path: Path, decisions: list[tuple[str, str, str]]) -> Path:
    prev = "0" * 64
    lines = []
    for i, (decision, kind, reason) in enumerate(decisions, 1):
        e: dict[str, Any] = {"seq": i, "ts": ago(5).timestamp() + i, "prev_hash": prev,
                             "frame_id": i, "frame_hash": "f" * 64, "action": {"type": kind},
                             "tier": 1, "decision": decision, "approver": "human:cli",
                             "reason": reason}
        e["hash"] = hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()
        prev = e["hash"]
        lines.append(json.dumps(e, sort_keys=True))
    path.write_text("\n".join(lines) + "\n")
    return path




# --------------------------------------------------------------------------- detectors
def _learn(c: Corpus, adoptions: list[Adoption] | None = None):
    return Learner(LearnConfig(mc_draws=4000), NOW).learn(c, adoptions or [])


def _corpus(pxx: PxxState) -> Corpus:
    c = Corpus()
    read_pxx_runs(pxx.root, c)
    return c




class SimulatedCrash(BaseException):
    """Stands in for the process dying at a write boundary. BaseException, so no
    ordinary `except Exception` on the way out can mistake it for an error."""


class CrashAt:
    """crash_hook for JsonStore: die at the n-th time ``point`` is reached."""

    def __init__(self, point: str, nth: int = 1) -> None:
        self.point, self.nth, self.seen = point, nth, 0

    def __call__(self, point: str) -> None:
        if point == self.point:
            self.seen += 1
            if self.seen == self.nth:
                raise SimulatedCrash(point)


class FakeSqlServer:
    """Just enough of SQL Server to run MssqlStore's transactions for real:
    rows with Revision, UPDLOCK reads, UPDATE ... WHERE Revision = ? with
    @@ROWCOUNT, the audit chain, commit and rollback. Not a substitute for a
    live server — the SQL text is only pattern-matched, never parsed."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {
            "dx_learn.Recommendation": {}, "dx_learn.Adoption": {}, "dx_learn.Forward": {}}
        self.audit: list[tuple[int, str, str, str]] = []
        self.log: list[str] = []
        self.commits = self.rollbacks = 0
        self.before_update: Any = None   # test hook: runs between the revision read and the UPDATE
        self.applock_result = 0          # sp_getapplock return code (<0 = timeout/deadlock/error)
        self.applock_held = 0

    def connect(self, *_a: Any, **_k: Any) -> FakeConn:
        return FakeConn(self)


class FakeConn:
    def __init__(self, db: FakeSqlServer) -> None:
        self.db = db
        self.pending: list[Any] = []    # undo log for rollback

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.pending.clear()
        self.db.commits += 1

    def rollback(self) -> None:
        for undo in reversed(self.pending):
            undo()
        self.pending.clear()
        self.db.rollbacks += 1


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn, self.db = conn, conn.db
        self.rows: list[Any] = []
        self.rowcount = -1
        self.fast_executemany = False

    def _table(self, sql: str) -> str:
        return next(t for t in self.db.tables if t in sql)

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        params = list(params or [])
        assert sql.count("?") == len(params), f"placeholder/param mismatch: {sql[:70]}"
        self.db.log.append(" ".join(sql.split()))
        self.rows, self.rowcount = [], -1
        if "sp_getapplock" in sql:
            self.rows = [[self.db.applock_result]]
            if self.db.applock_result >= 0:
                self.db.applock_held += 1
        elif "sp_releaseapplock" in sql:
            assert self.db.applock_held > 0, "released an applock that was never acquired"
            self.db.applock_held -= 1
        elif sql.startswith("SELECT Revision FROM"):
            row = self.db.tables[self._table(sql)].get(params[0])
            self.rows = [] if row is None else [[row["Revision"]]]
        elif sql.startswith("SELECT TOP (1) Hash"):
            self.rows = [[self.db.audit[-1][3]]] if self.db.audit else []
        elif sql.startswith("SELECT AuditId"):
            self.rows = list(self.db.audit)
        elif sql.startswith("SELECT RecKey, Revision"):
            self.rows = []
        elif sql.startswith("INSERT dx_learn.DecisionAudit"):
            entry = (len(self.db.audit) + 1, params[4], params[5], params[6])
            self.db.audit.append(entry)
            self.conn.pending.append(lambda: self.db.audit.remove(entry))
        elif sql.startswith("INSERT dx_learn."):
            table = self._table(sql)
            key = params[0]
            assert key not in self.db.tables[table], "duplicate key"
            self.db.tables[table][key] = {"Revision": 1, "values": params[1:]}
            self.conn.pending.append(lambda: self.db.tables[table].pop(key))
            self.rowcount = 1
        elif sql.startswith("UPDATE dx_learn."):
            if self.db.before_update:
                hook, self.db.before_update = self.db.before_update, None
                hook()
            table = self._table(sql)
            key, read_rev = params[-2], params[-1]
            row = self.db.tables[table].get(key)
            # Like a real driver under SET NOCOUNT ON: cursor.rowcount is useless
            # (-1); only an explicit SELECT @@ROWCOUNT in the batch says what happened.
            self.rowcount = -1
            if row is None or row["Revision"] != read_rev:
                self.rows = [[0]] if "SELECT @@ROWCOUNT" in sql else []
                return
            old = dict(row)
            row.update(Revision=read_rev + 1, values=params[:-2])
            self.conn.pending.append(lambda: self.db.tables[table].__setitem__(key, old))
            self.rows = [[1]] if "SELECT @@ROWCOUNT" in sql else []
        elif "LearnPass" in sql:
            self.rows = [[7]]

    def executemany(self, sql: str, rows: list[list[Any]]) -> None:
        for r in rows:
            assert sql.count("?") == len(r)
        self.db.log.append(" ".join(sql.split()))

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def close(self) -> None:
        pass


def _dx(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["dx", *argv])
    args = build_parser().parse_args(list(argv))
    args.func(args)




def lint_world(pxx: PxxState, n: int = 4) -> None:
    """Enough recurring LINT_BLOCKED loops across tasks to raise one proposal."""
    for i, t in enumerate(["a", "b", "c", "d", "e", "f"][:n]):
        pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999 broken")


def variant_world(pxx: PxxState, project: str = "svc-a") -> None:
    """glm credibly beats qwen in one task class: raises a forwardable proposal."""
    for i in range(20):
        pxx.run(when=ago(i % 10 + 1), project=project, model="qwen",
                code="COMPLETED" if i % 10 < 3 else "NO_TEST_PROGRESS")
        pxx.run(when=ago(i % 10 + 1), project=project, model="glm",
                code="COMPLETED" if i % 10 < 9 else "NO_TEST_PROGRESS")
