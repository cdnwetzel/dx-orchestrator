"""Where dx learn keeps its queue, adoptions, forwarding records and decision audit.

Every change is one ``Tx``: an optional audit entry plus record writes
(recommendations, adoptions, forward records). Each record carries the
``revision`` it was read at; a write is accepted only if that is still the
stored revision, and the stored revision is then bumped. Status alone is not
enough — two ``link`` decisions on an ACCEPTED recommendation both see
ACCEPTED, but only one of them can see adoption revision N.

Two backends with one contract:

* ``JsonStore`` (default) — a directory of JSON files under an ``fcntl`` lock.
  A transaction is first written as a durable, checksummed journal
  (``journal.json``: fsync, atomic rename, fsync of the directory); only then
  are the audit line and the record files written, and the journal is removed
  last. Every reader and writer replays a pending journal before doing
  anything else. Replay is idempotent — the audit line is recognised by its
  hash, a record already at its new revision is skipped — so a crash at any
  write boundary yields the decision exactly once. If replay cannot be proven
  safe (the audit chain or a record moved since the journal was written), the
  store raises ``RecoveryError`` on every operation until a human resolves it.
* ``MssqlStore`` — SQL Server via pyodbc, schema in ``learn_mssql.sql``. One
  database transaction per ``Tx`` gives the same all-or-nothing guarantee.
  **Unqualified:** exercised only against a fake connection; it refuses to
  start unless the operator acknowledges that (see ``cmd_learn``).

Neither store is the ledger and neither ever writes to it (RL-008).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import struct
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .learn_model import (
    Adoption,
    Corpus,
    ForwardRecord,
    Recommendation,
    adoption_from_json,
    forward_from_json,
    iso,
    rec_from_json,
    to_jsonable,
)

_GENESIS = "0" * 64
_SQL_DATETIMEOFFSET = -155   # ODBC type code pyodbc has no built-in converter for

Record = Recommendation | Adoption | ForwardRecord
KINDS = ("rec", "adoption", "forward")
_FILES = {"rec": "recommendations.json", "adoption": "adoptions.json", "forward": "forwards.json"}
_APPLY_ORDER = ("adoption", "forward", "rec")   # after the audit line; rec last


def _datetimeoffset(raw: bytes) -> datetime:
    """pyodbc returns DATETIMEOFFSET as raw bytes unless told otherwise."""
    y, mo, d, h, mi, s, ns, oh, om = struct.unpack("<6hI2h", raw)
    return datetime(y, mo, d, h, mi, s, ns // 1000, timezone(timedelta(hours=oh, minutes=om)))


class StoreError(Exception):
    pass


class StaleDecision(StoreError):
    """A record changed after it was read: another decision or pass won."""


class RecoveryError(StoreError):
    """An interrupted transaction cannot be completed safely. Until a human
    resolves it, the store refuses every read and write."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _audit_entry(prev_hash: str, *, ts: datetime, actor: str, action: str, target: str,
                 detail: dict[str, Any]) -> tuple[str, str]:
    """(canonical entry JSON, its sha256) — the same chain rule for both stores."""
    entry = {"ts": iso(ts), "actor": actor, "action": action, "target": target,
             "detail": to_jsonable(detail), "prev_hash": prev_hash}
    body = _canonical(entry)
    return body, hashlib.sha256(body.encode()).hexdigest()


def _kind_of(record: Record) -> str:
    if isinstance(record, Recommendation):
        return "rec"
    if isinstance(record, Adoption):
        return "adoption"
    return "forward"


@dataclass
class Tx:
    """One atomic change. Each record carries the revision it was read at
    (0 = must not exist yet); on commit its revision is bumped in place."""
    actor: str
    action: str
    target: str
    ts: datetime
    writes: list[Record]
    detail: dict[str, Any] = field(default_factory=dict)
    audit: bool = True


class Store(Protocol):
    def load_recs(self) -> dict[str, Recommendation]: ...
    def load_adoptions(self) -> dict[str, Adoption]: ...
    def load_forwards(self) -> dict[str, ForwardRecord]: ...
    def commit(self, tx: Tx, *, skip_conflicts: bool = False) -> list[str]: ...
    def mutation_lock(self) -> AbstractContextManager[None]: ...
    def record_pass(self, summary: dict[str, Any], corpus: Corpus, now: datetime) -> None: ...
    def verify_audit(self) -> int: ...
    def recover(self) -> str: ...


# =========================================================================== JSON
class JsonStore:
    def __init__(self, root: Path, *, crash_hook: Callable[[str], None] | None = None) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock_fh: Any = None
        self._depth = 0
        # Tests inject crashes at named write boundaries; production never sets it.
        self._crash = crash_hook or (lambda point: None)

    @property
    def journal_path(self) -> Path:
        return self.root / "journal.json"

    # ------------------------------------------------------------------ locking
    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Exclusive section shared by passes, decisions and forwarding.
        Re-entrant within one store object (forwarding commits while holding it)."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        fh = open(self.root / ".lock", "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        self._lock_fh, self._depth = fh, 1
        try:
            yield
        finally:
            self._depth, self._lock_fh = 0, None
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    # ------------------------------------------------------------------ durable io
    def _fsync_dir(self) -> None:
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _durable_write(self, name: str, data: bytes, *, before_rename: str | None = None) -> None:
        path, tmp = self.root / name, self.root / f".{name}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if before_rename:
            self._crash(before_rename)
        os.replace(tmp, path)
        self._fsync_dir()

    def _read_json(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path} is not valid JSON ({exc}); fix or restore it — "
                             "dx learn will not overwrite a store it cannot read") from exc

    @staticmethod
    def _json_bytes(data: Any) -> bytes:
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()

    # ------------------------------------------------------------------ recovery
    def _ready(self) -> None:
        """Called under the lock before every operation."""
        stale_tmp = self.root / ".journal.json.tmp"
        if stale_tmp.exists():
            stale_tmp.unlink()   # never renamed into place: that transaction never happened
        if self.journal_path.exists():
            self._replay(self._load_journal())

    def _load_journal(self) -> dict[str, Any]:
        raw = self.journal_path.read_bytes()
        try:
            journal = json.loads(raw)
            claimed = journal.pop("checksum")
        except (json.JSONDecodeError, KeyError, AttributeError) as exc:
            raise self._recovery("?", "?", "the journal itself is unreadable") from exc
        if hashlib.sha256(_canonical(journal).encode()).hexdigest() != claimed:
            raise self._recovery(journal.get("txid", "?"), journal.get("action", "?"),
                                 "the journal's checksum does not match its content")
        return dict(journal)

    def _recovery(self, txid: str, action: str, reason: str) -> RecoveryError:
        return RecoveryError(
            f"{self.root}: transaction {txid} ({action}) in {self.journal_path} was interrupted "
            f"and cannot be completed safely: {reason}. dx learn refuses every read and write "
            f"of this store until it is resolved — compare the journal with the named file, "
            f"restore from backup or finish the change by hand, remove the journal, then run "
            f"`dx learn recover`.")

    def _replay(self, journal: dict[str, Any]) -> None:
        txid, action = journal["txid"], journal["action"]
        if journal.get("audit_line"):
            self._replay_audit(journal)
        for kind in _APPLY_ORDER:
            writes = [w for w in journal["writes"] if w["kind"] == kind]
            if not writes:
                continue
            data = self._read_json(_FILES[kind], {})
            for w in writes:
                current = data.get(w["key"])
                current_rev = int(current.get("revision", 0)) if current else 0
                new_rev = int(w["record"]["revision"])
                if current_rev == new_rev:
                    if current != w["record"]:
                        raise self._recovery(txid, action, f"{_FILES[kind]} holds a different "
                                             f"record for {w['key'][:8]} at revision {new_rev}")
                    continue   # this write already landed
                if current_rev != int(w["from_revision"]):
                    raise self._recovery(txid, action, f"{_FILES[kind]} {w['key'][:8]} is at "
                                         f"revision {current_rev}, expected "
                                         f"{w['from_revision']} or {new_rev}")
                data[w["key"]] = w["record"]
            self._durable_write(_FILES[kind], self._json_bytes(data))
            self._crash(f"{kind}-written")
        self.journal_path.unlink()
        self._fsync_dir()

    def _replay_audit(self, journal: dict[str, Any]) -> None:
        txid, action = journal["txid"], journal["action"]
        line: str = journal["audit_line"]
        target_hash = json.loads(line)["hash"]
        path = self.root / "audit.jsonl"
        raw = path.read_bytes() if path.exists() else b""
        if raw and not raw.endswith(b"\n"):
            # A torn append. Safe to trim only if it is a prefix of OUR pending line.
            cut = raw.rfind(b"\n") + 1
            if not (line + "\n").encode().startswith(raw[cut:]):
                raise self._recovery(txid, action, "audit.jsonl ends in a partial line that is "
                                     "not this transaction's entry")
            with open(path, "r+b") as fh:
                fh.truncate(cut)
                fh.flush()
                os.fsync(fh.fileno())
            raw = raw[:cut]
        lines = raw.decode("utf-8").splitlines()
        tail = json.loads(lines[-1])["hash"] if lines else _GENESIS
        if tail == target_hash:
            return   # already appended
        if tail != journal["audit_prev"]:
            raise self._recovery(txid, action, "audit.jsonl moved past the point this "
                                 "transaction was chained to")
        data = (line + "\n").encode()
        half = len(data) // 2
        with open(path, "ab") as fh:
            self._crash("audit-before-append")
            fh.write(data[:half])
            fh.flush()
            os.fsync(fh.fileno())
            self._crash("audit-partial")
            fh.write(data[half:])
            fh.flush()
            os.fsync(fh.fileno())
        self._crash("audit-appended")

    def recover(self) -> str:
        with self.mutation_lock():
            if not self.journal_path.exists():
                return "no interrupted transaction"
            journal = self._load_journal()
            self._replay(journal)
            return f"completed interrupted transaction {journal['txid']} ({journal['action']})"

    # ------------------------------------------------------------------ reads
    def load_recs(self) -> dict[str, Recommendation]:
        with self.mutation_lock():
            self._ready()
            return {k: rec_from_json(v) for k, v in self._read_json(_FILES["rec"], {}).items()}

    def load_adoptions(self) -> dict[str, Adoption]:
        with self.mutation_lock():
            self._ready()
            raw = self._read_json(_FILES["adoption"], {})
            return {k: adoption_from_json(v) for k, v in raw.items()}

    def load_forwards(self) -> dict[str, ForwardRecord]:
        with self.mutation_lock():
            self._ready()
            raw = self._read_json(_FILES["forward"], {})
            return {k: forward_from_json(v) for k, v in raw.items()}

    # ------------------------------------------------------------------ the one write path
    def commit(self, tx: Tx, *, skip_conflicts: bool = False) -> list[str]:
        """Apply ``tx`` atomically. A write whose read revision is stale is a
        conflict: raise StaleDecision (nothing written), or with
        ``skip_conflicts`` drop just those writes. Returns "kind:key" conflicts."""
        with self.mutation_lock():
            self._ready()
            self.verify_audit()   # never extend a chain that no longer verifies
            state = {k: self._read_json(_FILES[k], {}) for k in KINDS}
            conflicts: list[str] = []
            accepted: list[Record] = []
            for record in tx.writes:
                kind = _kind_of(record)
                stored = state[kind].get(record.rec_key)
                stored_rev = int(stored.get("revision", 0)) if stored else 0
                if record.revision != stored_rev:
                    conflicts.append(f"{kind}:{record.rec_key}")
                else:
                    accepted.append(record)
            if conflicts and not skip_conflicts:
                raise StaleDecision(f"changed since read: {', '.join(c[:16] for c in conflicts)}; "
                                    "nothing was recorded — re-read and decide again")
            if not accepted:
                return conflicts
            writes = []
            for record in accepted:
                body = to_jsonable(record)
                body["revision"] = record.revision + 1
                writes.append({"kind": _kind_of(record), "key": record.rec_key,
                               "from_revision": record.revision, "record": body})
            audit_prev, audit_line = self._audit_tail(), None
            if tx.audit:
                entry_body, digest = _audit_entry(audit_prev, ts=tx.ts, actor=tx.actor,
                                                  action=tx.action, target=tx.target,
                                                  detail=tx.detail)
                entry = json.loads(entry_body)
                entry["hash"] = digest
                audit_line = _canonical(entry)
            journal: dict[str, Any] = {"txid": uuid.uuid4().hex, "action": tx.action,
                                       "target": tx.target, "audit_prev": audit_prev,
                                       "audit_line": audit_line, "writes": writes}
            journal["checksum"] = hashlib.sha256(_canonical(journal).encode()).hexdigest()
            self._durable_write("journal.json", _canonical(journal).encode(),
                                before_rename="journal-tmp-written")
            self._crash("journal-written")
            self._replay({k: v for k, v in journal.items() if k != "checksum"})
            for record in accepted:
                record.revision += 1
            return conflicts

    def _audit_tail(self) -> str:
        path = self.root / "audit.jsonl"
        if not path.exists() or path.stat().st_size == 0:
            return _GENESIS
        return str(json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["hash"])

    def record_pass(self, summary: dict[str, Any], corpus: Corpus, now: datetime) -> None:
        with self.mutation_lock():
            self._ready()
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
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path} line {i}: not JSON (a torn write with no journal "
                                 "to complete it?)") from exc
            claimed = entry.pop("hash", None)
            if entry.get("prev_hash") != prev:
                raise StoreError(f"{path} line {i}: prev_hash chain broken")
            if claimed != hashlib.sha256(_canonical(entry).encode()).hexdigest():
                raise StoreError(f"{path} line {i}: content hash mismatch (edited?)")
            prev = str(claimed)
        return len(lines)


# =========================================================================== SQL Server
_SQL_COLUMNS: dict[str, tuple[str, list[str]]] = {
    "rec": ("dx_learn.Recommendation", [
        "Kind", "Subject", "Score", "EvidenceWeight", "EvidenceJson", "DraftText", "MetricJson",
        "TargetsJson", "SuggestedRole", "Status", "CreatedAt", "UpdatedAt", "DecidedBy",
        "DecidedAt", "DecisionNote", "WeightAtDecision", "PxxSlug"]),
    "adoption": ("dx_learn.Adoption", [
        "MetricJson", "AcceptedBy", "AcceptedAt", "Note", "TaskId", "Active", "RetiredAt"]),
    "forward": ("dx_learn.Forward", [
        "Slug", "PayloadSha256", "State", "RecRevision", "UpdatedAt", "PxxView", "Divergence",
        "Note"]),
}


def _sql_values(record: Record) -> list[Any]:
    if isinstance(record, Recommendation):
        r = record
        return [r.kind, r.subject, r.score, r.evidence_weight, _canonical(to_jsonable(r.evidence)),
                r.draft_text, _canonical(r.metric), json.dumps(list(r.targets)),
                r.suggested_role, r.status, r.created_at, r.updated_at, r.decided_by,
                r.decided_at, r.decision_note, r.weight_at_decision, r.pxx_slug]
    if isinstance(record, Adoption):
        a = record
        return [_canonical(a.metric), a.accepted_by, a.accepted_at, a.note, a.task_id,
                1 if a.active else 0, a.retired_at]
    f = record
    return [f.slug, f.payload_sha256, f.state, f.rec_revision, f.updated_at, f.pxx_view,
            f.divergence, f.note]


class MssqlStore:
    """SQL Server backend. UNQUALIFIED: tested only against a fake connection
    (statement shape, parameter counts, transaction order) — never against a
    live server. ``acknowledge_unqualified=True`` is required to construct it.

    Each Tx is one transaction: every touched row is re-read WITH (UPDLOCK,
    HOLDLOCK), its Revision compared, then written with ``Revision = Revision + 1
    WHERE Revision = @read`` and @@ROWCOUNT checked. ``mutation_lock`` is a
    session-owned ``sp_getapplock``.
    """

    def __init__(self, conn_str: str, connect: Any = None, *,
                 acknowledge_unqualified: bool = False) -> None:
        if not acknowledge_unqualified:
            raise StoreError("the SQL Server backend is unqualified (never run against a live "
                             "server); it starts only when that is acknowledged")
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
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        cur = self.conn.cursor()
        try:
            cur.execute("SET NOCOUNT ON; DECLARE @r INT; EXEC @r = sp_getapplock "
                        "@Resource = N'dx_learn.mutation', @LockMode = 'Exclusive', "
                        "@LockOwner = 'Session', @LockTimeout = 60000; SELECT @r;")
            got = cur.fetchone()
            if got is None or int(got[0]) < 0:
                raise StoreError("could not take the dx_learn.mutation application lock")
            yield
        finally:
            cur.execute("EXEC sp_releaseapplock @Resource = N'dx_learn.mutation', "
                        "@LockOwner = 'Session';")
            cur.close()

    def recover(self) -> str:
        return "no interrupted transaction (SQL Server transactions are atomic)"

    # ------------------------------------------------------------------ reads
    def _select(self, kind: str) -> list[Any]:
        table, cols = _SQL_COLUMNS[kind]
        with self._tx() as cur:
            cur.execute(f"SELECT RecKey, Revision, {', '.join(cols)} FROM {table}")
            return list(cur.fetchall())

    def load_recs(self) -> dict[str, Recommendation]:
        out = {}
        for r in self._select("rec"):
            out[r[0]] = Recommendation(
                rec_key=r[0], revision=int(r[1]), kind=r[2], subject=r[3], score=float(r[4]),
                evidence_weight=float(r[5]), evidence=json.loads(r[6]), draft_text=r[7],
                metric=json.loads(r[8]), targets=tuple(json.loads(r[9] or "[]")),
                suggested_role=r[10], status=r[11], created_at=r[12], updated_at=r[13],
                decided_by=r[14], decided_at=r[15], decision_note=r[16],
                weight_at_decision=r[17], pxx_slug=r[18])
        return out

    def load_adoptions(self) -> dict[str, Adoption]:
        return {r[0]: Adoption(rec_key=r[0], revision=int(r[1]), metric=json.loads(r[2]),
                               accepted_by=r[3], accepted_at=r[4], note=r[5] or "",
                               task_id=r[6], active=bool(r[7]), retired_at=r[8])
                for r in self._select("adoption")}

    def load_forwards(self) -> dict[str, ForwardRecord]:
        return {r[0]: ForwardRecord(rec_key=r[0], revision=int(r[1]), slug=r[2],
                                    payload_sha256=r[3], state=r[4], rec_revision=int(r[5]),
                                    updated_at=r[6], pxx_view=r[7], divergence=r[8],
                                    note=r[9] or "")
                for r in self._select("forward")}

    # ------------------------------------------------------------------ the one write path
    def commit(self, tx: Tx, *, skip_conflicts: bool = False) -> list[str]:
        self.verify_audit()
        conflicts: list[str] = []
        accepted: list[Record] = []
        with self._tx() as cur:
            for record in tx.writes:
                kind = _kind_of(record)
                table, _ = _SQL_COLUMNS[kind]
                cur.execute(f"SELECT Revision FROM {table} WITH (UPDLOCK, HOLDLOCK) "
                            "WHERE RecKey = ?", [record.rec_key])
                row = cur.fetchone()
                stored_rev = 0 if row is None else int(row[0])
                if record.revision != stored_rev:
                    conflicts.append(f"{kind}:{record.rec_key}")
                else:
                    accepted.append(record)
            if conflicts and not skip_conflicts:
                raise StaleDecision(f"changed since read: {', '.join(c[:16] for c in conflicts)}")
            if tx.audit and accepted:
                cur.execute("SELECT TOP (1) Hash FROM dx_learn.DecisionAudit "
                            "WITH (UPDLOCK, HOLDLOCK) ORDER BY AuditId DESC")
                tail = cur.fetchone()
                body, digest = _audit_entry(_GENESIS if tail is None else str(tail[0]),
                                            ts=tx.ts, actor=tx.actor, action=tx.action,
                                            target=tx.target, detail=tx.detail)
                cur.execute("INSERT dx_learn.DecisionAudit (Ts, Actor, Action, RecKey, "
                            "EntryJson, PrevHash, Hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [tx.ts, tx.actor, tx.action, tx.target[:24], body,
                             json.loads(body)["prev_hash"], digest])
            for record in accepted:
                table, cols = _SQL_COLUMNS[_kind_of(record)]
                values = _sql_values(record)
                if record.revision == 0:
                    cur.execute(f"INSERT {table} (RecKey, Revision, {', '.join(cols)}) "
                                f"VALUES (?, 1, {', '.join('?' * len(cols))})",
                                [record.rec_key, *values])
                else:
                    sets = ", ".join(f"{c} = ?" for c in cols)
                    cur.execute(f"UPDATE {table} SET {sets}, Revision = Revision + 1 "
                                "WHERE RecKey = ? AND Revision = ?",
                                [*values, record.rec_key, record.revision])
                    if cur.rowcount != 1:
                        raise StaleDecision(f"{record.rec_key[:8]} changed during the write")
        for record in accepted:
            record.revision += 1
        return conflicts

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
        """Walk the audit chain in AuditId order; raises on any break."""
        with self._tx() as cur:
            cur.execute("SELECT AuditId, EntryJson, PrevHash, Hash FROM dx_learn.DecisionAudit "
                        "ORDER BY AuditId")
            rows = cur.fetchall()
        prev = _GENESIS
        for audit_id, body, prev_hash, digest in rows:
            if prev_hash != prev or json.loads(body).get("prev_hash") != prev:
                raise StoreError(f"DecisionAudit {audit_id}: prev_hash chain broken")
            if hashlib.sha256(str(body).encode()).hexdigest() != digest:
                raise StoreError(f"DecisionAudit {audit_id}: content hash mismatch (edited?)")
            prev = str(digest)
        return len(rows)
