"""Where dx learn keeps its queue, adoptions and decision audit.

Two backends with one contract:

* ``JsonStore`` (default) — a directory of JSON files, atomic writes, an
  ``fcntl`` lock around every mutation, and a hash-chained ``audit.jsonl`` in
  the same shape the rest of the stack uses (sha256 over canonical JSON, a
  64-zero genesis). Hermetic and local-first.
* ``MssqlStore`` — SQL Server via pyodbc (optional dependency), schema in
  ``learn_mssql.sql``. Same tables plus fact tables of attempts and ledger
  events per pass, so the history can be queried in T-SQL.

Neither store is the ledger and neither ever writes to it (RL-008). These
records are dx learn's own working state; the decisions that change the
software still go through ``dx run`` and a signed ``dx merge``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .learn_model import (
    Adoption,
    Corpus,
    Recommendation,
    adoption_from_json,
    iso,
    rec_from_json,
    to_jsonable,
)

_GENESIS = "0" * 64
_SQL_DATETIMEOFFSET = -155   # ODBC type code pyodbc has no built-in converter for


def _datetimeoffset(raw: bytes) -> datetime:
    """pyodbc returns DATETIMEOFFSET as raw bytes unless told otherwise."""
    y, mo, d, h, mi, s, ns, oh, om = struct.unpack("<6hI2h", raw)
    return datetime(y, mo, d, h, mi, s, ns // 1000, timezone(timedelta(hours=oh, minutes=om)))


class StoreError(Exception):
    pass


class Store(Protocol):
    def load_recs(self) -> dict[str, Recommendation]: ...
    def save_recs(self, recs: list[Recommendation], now: datetime) -> None: ...
    def load_adoptions(self) -> dict[str, Adoption]: ...
    def decide(self, rec: Recommendation, adoption: Adoption | None, *, actor: str,
               action: str, detail: dict[str, Any], now: datetime) -> None: ...
    def record_pass(self, summary: dict[str, Any], corpus: Corpus, now: datetime) -> None: ...
    def verify_audit(self) -> int: ...


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class JsonStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    # -- plumbing
    @contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self.root / ".lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _read(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path} is not valid JSON ({exc}); fix or restore it — "
                             "dx learn will not overwrite a store it cannot read") from exc

    def _write(self, name: str, data: Any) -> None:
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.root / name)

    def _audit_tail(self) -> str:
        path = self.root / "audit.jsonl"
        if not path.exists() or path.stat().st_size == 0:
            return _GENESIS
        last = path.read_text(encoding="utf-8").splitlines()[-1]
        return str(json.loads(last)["hash"])

    def _append_audit(self, entry: dict[str, Any]) -> None:
        entry = {**entry, "prev_hash": self._audit_tail()}
        entry["hash"] = hashlib.sha256(_canonical(entry).encode()).hexdigest()
        with open(self.root / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")

    # -- contract
    def load_recs(self) -> dict[str, Recommendation]:
        raw = self._read("recommendations.json", {})
        return {k: rec_from_json(v) for k, v in raw.items()}

    def save_recs(self, recs: list[Recommendation], now: datetime) -> None:
        with self._locked():
            raw = self._read("recommendations.json", {})
            for r in recs:
                raw[r.rec_key] = to_jsonable(r)
            self._write("recommendations.json", raw)

    def load_adoptions(self) -> dict[str, Adoption]:
        raw = self._read("adoptions.json", {})
        return {k: adoption_from_json(v) for k, v in raw.items()}

    def decide(self, rec: Recommendation, adoption: Adoption | None, *, actor: str,
               action: str, detail: dict[str, Any], now: datetime) -> None:
        with self._locked():
            recs = self._read("recommendations.json", {})
            recs[rec.rec_key] = to_jsonable(rec)
            ads = self._read("adoptions.json", {})
            if adoption is not None:
                ads[adoption.rec_key] = to_jsonable(adoption)
            # audit first: a decision that is not audited did not happen
            self._append_audit({"ts": iso(now), "actor": actor, "action": action,
                                "target": rec.rec_key, "detail": to_jsonable(detail)})
            self._write("adoptions.json", ads)
            self._write("recommendations.json", recs)

    def record_pass(self, summary: dict[str, Any], corpus: Corpus, now: datetime) -> None:
        with self._locked():
            with open(self.root / "passes.jsonl", "a", encoding="utf-8") as fh:
                fh.write(_canonical({"ts": iso(now), **to_jsonable(summary)}) + "\n")

    def verify_audit(self) -> int:
        """Walk the decision audit chain. Returns rows verified; raises on any break."""
        path = self.root / "audit.jsonl"
        if not path.exists():
            return 0
        prev = _GENESIS
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            entry = json.loads(line)
            claimed = entry.pop("hash", None)
            if entry.get("prev_hash") != prev:
                raise StoreError(f"{path} line {i}: prev_hash chain broken")
            if claimed != hashlib.sha256(_canonical(entry).encode()).hexdigest():
                raise StoreError(f"{path} line {i}: content hash mismatch (edited?)")
            prev = str(claimed)
        return len(lines)


class MssqlStore:
    """SQL Server backend. Requires ``pyodbc`` and the schema in learn_mssql.sql.

    Upserts use UPDATE ... WITH (UPDLOCK, SERIALIZABLE) then INSERT on
    @@ROWCOUNT = 0, not MERGE. Every mutation is one transaction; the audit
    table refuses UPDATE and DELETE by trigger.
    """

    def __init__(self, conn_str: str, connect: Any = None) -> None:
        if connect is None:
            try:
                import pyodbc  # type: ignore[import-not-found]
            except ImportError as exc:
                raise StoreError("the MSSQL store needs pyodbc: pip install pyodbc") from exc
            connect = pyodbc.connect
        self.conn = connect(conn_str, autocommit=False)
        if hasattr(self.conn, "add_output_converter"):
            self.conn.add_output_converter(_SQL_DATETIMEOFFSET, _datetimeoffset)

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        cur = self.conn.cursor()
        try:
            yield cur
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    _UPSERT_REC = """
SET NOCOUNT ON;
UPDATE dx_learn.Recommendation WITH (UPDLOCK, SERIALIZABLE)
   SET Kind=?, Subject=?, Score=?, EvidenceWeight=?, EvidenceJson=?, DraftText=?, MetricName=?,
       MetricKey=?, TargetsJson=?, SuggestedRole=?, Status=?, CreatedAt=?, UpdatedAt=?,
       DecidedBy=?, DecidedAt=?, DecisionNote=?, WeightAtDecision=?, PxxSlug=?
 WHERE RecKey=?;
IF @@ROWCOUNT = 0
  INSERT dx_learn.Recommendation (Kind, Subject, Score, EvidenceWeight, EvidenceJson, DraftText,
    MetricName, MetricKey, TargetsJson, SuggestedRole, Status, CreatedAt, UpdatedAt, DecidedBy,
    DecidedAt, DecisionNote, WeightAtDecision, PxxSlug, RecKey)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"""

    _UPSERT_ADOPTION = """
SET NOCOUNT ON;
UPDATE dx_learn.Adoption WITH (UPDLOCK, SERIALIZABLE)
   SET MetricName=?, MetricKey=?, AcceptedBy=?, AcceptedAt=?, Note=?, TaskId=?, Active=?, RetiredAt=?
 WHERE RecKey=?;
IF @@ROWCOUNT = 0
  INSERT dx_learn.Adoption (MetricName, MetricKey, AcceptedBy, AcceptedAt, Note, TaskId, Active,
    RetiredAt, RecKey)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);"""

    @staticmethod
    def _rec_params(r: Recommendation) -> list[Any]:
        p = [r.kind, r.subject, r.score, r.evidence_weight, _canonical(to_jsonable(r.evidence)),
             r.draft_text, r.metric["name"], r.metric["key"], json.dumps(list(r.targets)),
             r.suggested_role, r.status, r.created_at, r.updated_at, r.decided_by, r.decided_at,
             r.decision_note, r.weight_at_decision, r.pxx_slug, r.rec_key]
        return p + p

    @staticmethod
    def _adoption_params(a: Adoption) -> list[Any]:
        p = [a.metric["name"], a.metric["key"], a.accepted_by, a.accepted_at, a.note, a.task_id,
             1 if a.active else 0, a.retired_at, a.rec_key]
        return p + p

    def load_recs(self) -> dict[str, Recommendation]:
        with self._tx() as cur:
            cur.execute("SELECT RecKey, Kind, Subject, Score, EvidenceWeight, EvidenceJson, "
                        "DraftText, MetricName, MetricKey, TargetsJson, SuggestedRole, Status, "
                        "CreatedAt, UpdatedAt, DecidedBy, DecidedAt, DecisionNote, "
                        "WeightAtDecision, PxxSlug FROM dx_learn.Recommendation")
            rows = cur.fetchall()
        out = {}
        for r in rows:
            out[r[0]] = Recommendation(
                rec_key=r[0], kind=r[1], subject=r[2], score=float(r[3]),
                evidence_weight=float(r[4]), evidence=json.loads(r[5]), draft_text=r[6],
                metric={"name": r[7], "key": r[8]}, targets=tuple(json.loads(r[9] or "[]")),
                suggested_role=r[10], status=r[11], created_at=r[12], updated_at=r[13],
                decided_by=r[14], decided_at=r[15], decision_note=r[16],
                weight_at_decision=r[17], pxx_slug=r[18])
        return out

    def save_recs(self, recs: list[Recommendation], now: datetime) -> None:
        with self._tx() as cur:
            for r in recs:
                cur.execute(self._UPSERT_REC, self._rec_params(r))

    def load_adoptions(self) -> dict[str, Adoption]:
        with self._tx() as cur:
            cur.execute("SELECT RecKey, MetricName, MetricKey, AcceptedBy, AcceptedAt, Note, "
                        "TaskId, Active, RetiredAt FROM dx_learn.Adoption")
            rows = cur.fetchall()
        return {r[0]: Adoption(rec_key=r[0], metric={"name": r[1], "key": r[2]},
                               accepted_by=r[3], accepted_at=r[4], note=r[5] or "",
                               task_id=r[6], active=bool(r[7]), retired_at=r[8]) for r in rows}

    def decide(self, rec: Recommendation, adoption: Adoption | None, *, actor: str,
               action: str, detail: dict[str, Any], now: datetime) -> None:
        with self._tx() as cur:
            cur.execute("INSERT dx_learn.DecisionAudit (Ts, Actor, Action, RecKey, DetailJson) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [now, actor, action, rec.rec_key, _canonical(to_jsonable(detail))])
            cur.execute(self._UPSERT_REC, self._rec_params(rec))
            if adoption is not None:
                cur.execute(self._UPSERT_ADOPTION, self._adoption_params(adoption))

    def record_pass(self, summary: dict[str, Any], corpus: Corpus, now: datetime) -> None:
        with self._tx() as cur:
            cur.execute("INSERT dx_learn.LearnPass (Ts, LedgerHead, SummaryJson) "
                        "OUTPUT INSERTED.PassId VALUES (?, ?, ?)",
                        [now, corpus.ledger_head, _canonical(to_jsonable(summary))])
            pass_id = cur.fetchone()[0]
            cur.fast_executemany = True
            if corpus.attempts:
                cur.executemany(
                    "INSERT dx_learn.Attempt (PassId, RunId, Ts, TaskKey, DxTaskId, TaskClass, "
                    "Model, AgentVersionId, Code, Success) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [[pass_id, a.run_id, a.ts, a.task_key, a.dx_task_id, a.task_class, a.model,
                      a.agent_version_id, a.code, 1 if a.success else 0] for a in corpus.attempts])
            events = corpus.ledger_events()
            if events:
                cur.executemany(
                    "INSERT dx_learn.LedgerEvent (PassId, Ts, TaskId, Action, SignatureId) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [[pass_id, e.ts, e.task_id, e.action, e.signature] for e in events])

    def verify_audit(self) -> int:
        with self._tx() as cur:
            cur.execute("SELECT COUNT(*) FROM dx_learn.DecisionAudit")
            return int(cur.fetchone()[0])
