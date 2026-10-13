"""dx learn store: revision-checked transactions and crash recovery.

Finding R1 (revision-based concurrency): status-only compare-and-set let two
`link` decisions both succeed because the recommendation stayed ACCEPTED.
Every write is now checked against the revision of each record it touches —
recommendations, adoptions and forward records — in both backends.

Finding R2 (recoverable JSON decisions): audit append, adoption update and
recommendation update were three separate writes. They are now one journaled
transaction; these tests kill the store at every write boundary and require
recovery to land the decision exactly once, or refuse every further operation
with an error naming the journal and the reason.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dx import cmd_learn
from dx.learn_model import Adoption, ForwardRecord, Recommendation
from dx.learn_store import JsonStore, MssqlStore, RecoveryError, StaleDecision, StoreError, Tx
from learn_helpers import (
    NOW,
    CrashAt,
    FakeSqlServer,
    SimulatedCrash,
    _corpus,
    _dx,
    _learn,
    lint_world,
)


def _proposal(pxx) -> Recommendation:
    lint_world(pxx)
    return _learn(_corpus(pxx))[0]


def _seed(store, pxx) -> Recommendation:
    """A stored PROPOSED recommendation (revision 1)."""
    rec = _proposal(pxx)
    store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[rec], audit=False))
    return store.load_recs()[rec.rec_key]


def _accept_tx(rec: Recommendation, adoption_rev: int = 0) -> Tx:
    accepted = replace(rec, status="ACCEPTED", decided_by="Chris", decided_at=NOW)
    adoption = Adoption(rec.rec_key, dict(rec.metric), "Chris", NOW, revision=adoption_rev)
    return Tx("Chris", "accept", rec.rec_key, NOW, writes=[accepted, adoption])


def _mssql(db: FakeSqlServer) -> MssqlStore:
    return MssqlStore("DSN=x", connect=db.connect, acknowledge_unqualified=True)


# =========================================================================== R1: revisions
class TestRevisionsJson:
    def test_two_concurrent_links_cannot_both_succeed(self, tmp_path, pxx):
        """Both read ACCEPTED and adoption revision 1; only one link may land."""
        store = JsonStore(tmp_path / "s")
        rec = _seed(store, pxx)
        store.commit(_accept_tx(rec))
        first = store.load_adoptions()[rec.rec_key]     # client A reads
        second = store.load_adoptions()[rec.rec_key]    # client B reads the same revision
        assert store.load_recs()[rec.rec_key].status == "ACCEPTED"
        store.commit(Tx("Chris", "link", rec.rec_key, NOW, writes=[replace(first, task_id="T-1")]))
        with pytest.raises(StaleDecision):
            store.commit(Tx("Pat", "link", rec.rec_key, NOW,
                            writes=[replace(second, task_id="T-2")]))
        assert store.load_adoptions()[rec.rec_key].task_id == "T-1"
        actions = [json.loads(line)["action"]
                   for line in (tmp_path / "s" / "audit.jsonl").read_text().splitlines()]
        assert actions == ["accept", "link"]

    def test_link_after_a_concurrent_retire_is_refused(self, tmp_path, pxx):
        store = JsonStore(tmp_path / "s")
        rec = _seed(store, pxx)
        store.commit(_accept_tx(rec))
        stale_ad = store.load_adoptions()[rec.rec_key]
        live_rec, live_ad = store.load_recs()[rec.rec_key], store.load_adoptions()[rec.rec_key]
        store.commit(Tx("Chris", "retire", rec.rec_key, NOW,
                        writes=[replace(live_rec, status="RETIRED"),
                                replace(live_ad, active=False)]))
        with pytest.raises(StaleDecision):
            store.commit(Tx("Pat", "link", rec.rec_key, NOW,
                            writes=[replace(stale_ad, task_id="T-9")]))

    def test_decision_on_a_stale_read_writes_nothing_at_all(self, tmp_path, pxx):
        store = JsonStore(tmp_path / "s")
        rec = _seed(store, pxx)
        store.commit(Tx("Chris", "reject", rec.rec_key, NOW,
                        writes=[replace(rec, status="REJECTED")]))
        with pytest.raises(StaleDecision):
            store.commit(_accept_tx(rec))               # rec is the pre-reject read
        assert store.load_adoptions() == {}             # the adoption half did not land either
        assert store.verify_audit() == 1

    def test_pass_skips_records_decided_meanwhile_and_writes_the_rest(self, tmp_path, pxx):
        store = JsonStore(tmp_path / "s")
        rec = _seed(store, pxx)
        pass_view = store.load_recs()[rec.rec_key]      # the pass reads...
        store.commit(_accept_tx(store.load_recs()[rec.rec_key]))   # ...a human accepts...
        other = replace(_proposal(pxx), rec_key="z" * 24)
        conflicts = store.commit(Tx("dx learn", "pass", "pass", NOW,
                                    writes=[replace(pass_view, score=99.0), other],
                                    audit=False), skip_conflicts=True)
        assert conflicts == [f"rec:{rec.rec_key}"]
        assert store.load_recs()[rec.rec_key].status == "ACCEPTED"
        assert "z" * 24 in store.load_recs()

    def test_two_passes_creating_the_same_new_record_conflict(self, tmp_path, pxx):
        store = JsonStore(tmp_path / "s")
        a, b = _proposal(pxx), _proposal(pxx)
        assert a.rec_key == b.rec_key and a.revision == b.revision == 0
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[a], audit=False))
        assert store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[b], audit=False),
                            skip_conflicts=True) == [f"rec:{a.rec_key}"]

    def test_revisions_cover_forward_records(self, tmp_path, pxx):
        store = JsonStore(tmp_path / "s")
        rec = _seed(store, pxx)
        fwd = ForwardRecord(rec.rec_key, "abc123abc123", "f" * 64, "INTENT", 1, NOW)
        store.commit(Tx("dx learn", "forward-intent", rec.rec_key, NOW, writes=[fwd]))
        stale = replace(store.load_forwards()[rec.rec_key])
        store.commit(Tx("dx learn", "forward-outcome", rec.rec_key, NOW,
                        writes=[replace(store.load_forwards()[rec.rec_key], state="WRITTEN")]))
        with pytest.raises(StaleDecision):
            store.commit(Tx("dx learn", "forward-outcome", rec.rec_key, NOW,
                            writes=[replace(stale, state="NOT_WRITTEN")]))


class TestRevisionsMssqlParity:
    """The same guarantees through MssqlStore against a revision-aware fake.
    The backend stays UNQUALIFIED: this proves the logic, not the SQL dialect."""

    def test_two_concurrent_links_cannot_both_succeed(self, pxx):
        db = FakeSqlServer()
        store = _mssql(db)
        rec = _proposal(pxx)
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[rec], audit=False))
        store.commit(_accept_tx(rec))
        ad = Adoption(rec.rec_key, dict(rec.metric), "Chris", NOW, revision=1)
        store.commit(Tx("Chris", "link", rec.rec_key, NOW, writes=[replace(ad, task_id="T-1")]))
        with pytest.raises(StaleDecision):
            store.commit(Tx("Pat", "link", rec.rec_key, NOW, writes=[replace(ad, task_id="T-2")]))
        assert [json.loads(a[1])["action"] for a in db.audit] == ["accept", "link"]

    def test_row_changed_between_read_and_update_rolls_back_everything(self, pxx):
        db = FakeSqlServer()
        store = _mssql(db)
        rec = _proposal(pxx)
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[rec], audit=False))

        def sneak() -> None:   # another session bumps the row after our UPDLOCK read
            db.tables["dx_learn.Recommendation"][rec.rec_key]["Revision"] += 1
        db.before_update = sneak
        with pytest.raises(StaleDecision):
            store.commit(_accept_tx(rec))
        assert db.audit == [] and rec.rec_key not in db.tables["dx_learn.Adoption"]
        assert db.rollbacks >= 1

    def test_two_passes_creating_the_same_new_record_conflict(self, pxx):
        """The pre-read revision check is what turns a concurrent INSERT into a
        skippable conflict instead of a duplicate-key failure."""
        db = FakeSqlServer()
        store = _mssql(db)
        a, b = _proposal(pxx), _proposal(pxx)
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[a], audit=False))
        assert store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[b], audit=False),
                            skip_conflicts=True) == [f"rec:{a.rec_key}"]

    def test_unqualified_backend_refuses_without_acknowledgement(self):
        with pytest.raises(StoreError, match="unqualified"):
            MssqlStore("DSN=x", connect=FakeSqlServer().connect)

    def test_cli_refuses_mssql_without_acknowledgement(self, monkeypatch, capsys):
        monkeypatch.setenv("DX_LEARN_MSSQL", "DSN=nowhere")
        with pytest.raises(SystemExit):
            _dx(monkeypatch, "learn", "queue")
        err = capsys.readouterr().err
        assert "UNQUALIFIED" in err and "DX_LEARN_MSSQL_UNQUALIFIED=accept" in err

    def test_audit_chain_edit_is_detected(self, pxx):
        db = FakeSqlServer()
        store = _mssql(db)
        rec = _proposal(pxx)
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[rec], audit=False))
        store.commit(_accept_tx(rec))
        aid, body, prev, digest = db.audit[0]
        db.audit[0] = (aid, body.replace("Chris", "Mallory"), prev, digest)
        with pytest.raises(StoreError, match="content hash"):
            store.verify_audit()

    def test_schema_ships_and_says_unqualified(self):
        import dx
        sql = (Path(dx.__file__).parent / "learn_mssql.sql").read_text()
        assert "UNQUALIFIED" in sql and "Revision" in sql and "dx_learn.Forward" in sql
        assert "INSTEAD OF UPDATE, DELETE" in sql


# =========================================================================== R2: crash recovery
BOUNDARIES = ["journal-tmp-written", "journal-written", "audit-before-append", "audit-partial",
              "audit-appended", "adoption-written", "rec-written"]


def _state(root: Path) -> tuple[str, int, int, list[str]]:
    store = JsonStore(root)
    recs, ads = store.load_recs(), store.load_adoptions()
    (rec,) = recs.values()
    audit = (root / "audit.jsonl").read_text().splitlines() if (root / "audit.jsonl").exists() else []
    return rec.status, rec.revision, len(ads), [json.loads(a)["action"] for a in audit]


class TestCrashRecovery:
    @pytest.mark.parametrize("boundary", BOUNDARIES)
    def test_crash_at_every_boundary_lands_exactly_once_or_not_at_all(self, tmp_path, pxx,
                                                                      boundary):
        root = tmp_path / "s"
        rec = _seed(JsonStore(root), pxx)
        crashing = JsonStore(root, crash_hook=CrashAt(boundary))
        with pytest.raises(SimulatedCrash):
            crashing.commit(_accept_tx(rec))
        status, revision, adoptions, actions = _state(root)   # a fresh process recovers
        if boundary == "journal-tmp-written":
            # the journal never became durable: the decision never happened
            assert (status, revision, adoptions, actions) == ("PROPOSED", 1, 0, [])
        else:
            assert (status, revision, adoptions, actions) == ("ACCEPTED", 2, 1, ["accept"])
        assert not (root / "journal.json").exists()
        assert not (root / ".journal.json.tmp").exists()
        assert JsonStore(root).verify_audit() == len(actions)
        assert JsonStore(root).recover() == "no interrupted transaction"   # idempotent

    @pytest.mark.parametrize("first,second", [("audit-partial", "adoption-written"),
                                              ("journal-written", "audit-partial"),
                                              ("audit-appended", "rec-written")])
    def test_crash_during_recovery_still_lands_exactly_once(self, tmp_path, pxx, first, second):
        root = tmp_path / "s"
        rec = _seed(JsonStore(root), pxx)
        with pytest.raises(SimulatedCrash):
            JsonStore(root, crash_hook=CrashAt(first)).commit(_accept_tx(rec))
        with pytest.raises(SimulatedCrash):
            JsonStore(root, crash_hook=CrashAt(second)).load_recs()   # recovery crashes too
        assert _state(root) == ("ACCEPTED", 2, 1, ["accept"])

    def test_store_is_usable_after_recovery(self, tmp_path, pxx):
        root = tmp_path / "s"
        rec = _seed(JsonStore(root), pxx)
        with pytest.raises(SimulatedCrash):
            JsonStore(root, crash_hook=CrashAt("adoption-written")).commit(_accept_tx(rec))
        store = JsonStore(root)
        ad = store.load_adoptions()[rec.rec_key]
        store.commit(Tx("Chris", "link", rec.rec_key, NOW, writes=[replace(ad, task_id="T-1")]))
        assert [a for *_, a in [_state(root)]][0] == ["accept", "link"]


class TestRecoveryRefuses:
    def _interrupted(self, tmp_path, pxx, boundary: str) -> Path:
        root = tmp_path / "s"
        rec = _seed(JsonStore(root), pxx)
        with pytest.raises(SimulatedCrash):
            JsonStore(root, crash_hook=CrashAt(boundary)).commit(_accept_tx(rec))
        return root

    def test_audit_moved_while_journal_pending(self, tmp_path, pxx):
        root = self._interrupted(tmp_path, pxx, "journal-written")
        # someone else extended the chain before recovery ran
        with (root / "audit.jsonl").open("a") as fh:
            fh.write(json.dumps({"hash": "e" * 64, "prev_hash": "0" * 64}) + "\n")
        with pytest.raises(RecoveryError) as exc:
            JsonStore(root).load_recs()
        msg = str(exc.value)
        assert "journal.json" in msg and "accept" in msg and "audit.jsonl moved" in msg
        assert "dx learn recover" in msg
        with pytest.raises(RecoveryError):                 # every further mutation refused
            JsonStore(root).commit(Tx("Chris", "reject", "x", NOW, writes=[]))
        assert (root / "journal.json").exists()            # evidence is kept, not cleaned up

    def test_record_diverged_while_journal_pending(self, tmp_path, pxx):
        root = self._interrupted(tmp_path, pxx, "audit-appended")
        recs = json.loads((root / "recommendations.json").read_text())
        (key,) = recs
        recs[key]["revision"] = 7
        (root / "recommendations.json").write_text(json.dumps(recs))
        with pytest.raises(RecoveryError, match="revision 7"):
            JsonStore(root).load_adoptions()

    def test_torn_audit_line_that_is_not_ours(self, tmp_path, pxx):
        root = self._interrupted(tmp_path, pxx, "audit-partial")
        with (root / "audit.jsonl").open("ab") as fh:
            fh.write(b"garbage")
        with pytest.raises(RecoveryError, match="partial line"):
            JsonStore(root).load_recs()

    def test_corrupt_journal(self, tmp_path, pxx):
        root = self._interrupted(tmp_path, pxx, "journal-written")
        journal = json.loads((root / "journal.json").read_text())
        journal["action"] = "reject"
        (root / "journal.json").write_text(json.dumps(journal))
        with pytest.raises(RecoveryError, match="checksum"):
            JsonStore(root).load_recs()

    def test_torn_audit_without_a_journal_blocks_decisions(self, tmp_path, pxx):
        root = tmp_path / "s"
        rec = _seed(JsonStore(root), pxx)
        (root / "audit.jsonl").write_text('{"half": ')
        with pytest.raises(StoreError, match="torn write"):
            JsonStore(root).commit(_accept_tx(rec))

    def test_cli_recover_and_queue_report_precisely(self, tmp_path, pxx, monkeypatch, capsys):
        root = self._interrupted(tmp_path, pxx, "journal-written")
        monkeypatch.setenv("DX_LEARN_STORE", str(root))
        _dx(monkeypatch, "learn", "recover")
        assert "completed interrupted transaction" in capsys.readouterr().out
        root2 = self._interrupted(tmp_path / "b", pxx, "journal-written")
        with (root2 / "audit.jsonl").open("a") as fh:
            fh.write(json.dumps({"hash": "e" * 64}) + "\n")
        monkeypatch.setenv("DX_LEARN_STORE", str(root2))
        for argv in (("learn", "recover"), ("learn", "queue")):
            with pytest.raises(SystemExit) as exc:
                _dx(monkeypatch, *argv)
            assert exc.value.code == 1
            assert "cannot be completed safely" in capsys.readouterr().err
        assert cmd_learn  # imported for monkeypatching parity with other suites


def test_cli_pass_reports_and_preserves_a_decision_made_during_it(tmp_path, pxx, monkeypatch,
                                                                  capsys):
    """Between the pass reading the queue and writing it back, a decision lands
    (same process, re-entrant lock — the only way to interleave with the JSON
    lock held). The pass must leave it alone and say so."""
    lint_world(pxx)
    monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
    store = JsonStore(tmp_path / "s")
    monkeypatch.setattr(cmd_learn, "_open_store", lambda _args: store)
    _dx(monkeypatch, "learn", "run", "--no-ledger", "--json", "--now", NOW.isoformat())
    key = json.loads(capsys.readouterr().out)["proposed"][0]
    real_reconcile = cmd_learn.reconcile
    decided = False

    def reconcile_then_decide(*a, **k):   # after the pass read the queue, before it writes
        nonlocal decided
        out = real_reconcile(*a, **k)
        if not decided:   # the pass reconciles once per view; the decision lands once
            decided = True
            store.commit(_accept_tx(store.load_recs()[key]))
        return out
    monkeypatch.setattr(cmd_learn, "reconcile", reconcile_then_decide)
    _dx(monkeypatch, "learn", "run", "--no-ledger", "--json", "--now", NOW.isoformat())
    summary = json.loads(capsys.readouterr().out)
    assert summary["skipped_decided_meanwhile"] == [key] and key not in summary["proposed"]
    assert store.load_recs()[key].status == "ACCEPTED"


# =========================================================================== compatibility
def test_a_store_written_while_pxx_slug_existed_still_loads(tmp_path, pxx):
    """dx learn's first cut persisted a Recommendation.pxx_slug field. It was
    dead — forwarding state lives on ForwardRecord.slug — and is removed. A
    JSON store written while it existed must still load (the key is ignored),
    and the next write must drop it."""
    store = JsonStore(tmp_path / "s")
    rec = _seed(store, pxx)
    path = tmp_path / "s" / "recommendations.json"
    raw = json.loads(path.read_text())
    assert "pxx_slug" not in raw[rec.rec_key]
    raw[rec.rec_key]["pxx_slug"] = "model:switch_model"      # the pre-removal shape
    path.write_text(json.dumps(raw))
    assert store.load_recs()[rec.rec_key] == rec             # ignored, not an error
    store.commit(Tx("Chris", "reject", rec.rec_key, NOW,
                    writes=[replace(rec, status="REJECTED", decided_by="Chris",
                                    decided_at=NOW, weight_at_decision=rec.evidence_weight)]))
    raw = json.loads(path.read_text())
    assert "pxx_slug" not in raw[rec.rec_key]                # the next write drops it
