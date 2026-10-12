"""Regression tests for the second independent review of dx learn (commit 90cf5e9).

One class per finding. Every test here fails against 90cf5e9 and passes now,
except test_acquired_applock_is_released, a positive control for finding 8.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from dx import cmd_learn, learn_bridge
from dx.learn_engine import LearnConfig, Learner
from dx.learn_forward import forward_proposals, reconcile_forwards
from dx.learn_model import Adoption, Corpus
from dx.learn_sources import read_ledger
from dx.learn_store import JsonStore, MssqlStore, StoreError, Tx
from learn_helpers import (
    NOW,
    FakeSqlServer,
    _corpus,
    _dx,
    _learn,
    ago,
    lint_world,
    row,
    variant_world,
    write_ledger,
)

PENDING = ("inbox", "human-review-required")


def _accepted(tmp_path, pxx, monkeypatch, capsys) -> tuple[Path, str]:
    """A JSON store holding one ACCEPTED proposal, via the real CLI."""
    lint_world(pxx)
    store_dir = tmp_path / "store"
    monkeypatch.setenv("DX_LEARN_STORE", str(store_dir))
    monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
    _dx(monkeypatch, "learn", "run", "--no-ledger", "--json", "--now", NOW.isoformat())
    key = json.loads(capsys.readouterr().out)["proposed"][0]
    _dx(monkeypatch, "learn", "accept", key, "--by", "Chris")
    capsys.readouterr()
    return store_dir, key


def _audit_actions(store_dir: Path) -> list[str]:
    return [json.loads(line)["action"] for line in (store_dir / "audit.jsonl").read_text().splitlines()]


# --------------------------------------------------------------------------- finding 1 (P0)
class TestF1ConcurrentLinksAndStaleReads:
    def test_two_real_processes_linking_at_once_one_wins(self, tmp_path, pxx, monkeypatch, capsys):
        store_dir, key = _accepted(tmp_path, pxx, monkeypatch, capsys)
        dx = Path(sys.executable).with_name("dx")
        procs = [subprocess.Popen([str(dx), "learn", "link", key, "--task", task, "--by", who],
                                  env=dict(os.environ), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for task, who in (("T-1", "Chris"), ("T-2", "Pat"))]
        codes = sorted(p.wait(timeout=60) for p in procs)
        assert codes == [0, 1]
        assert _audit_actions(store_dir).count("link") == 1

    def test_relink_must_name_what_it_replaces(self, tmp_path, pxx, monkeypatch, capsys):
        store_dir, key = _accepted(tmp_path, pxx, monkeypatch, capsys)
        _dx(monkeypatch, "learn", "link", key, "--task", "T-1", "--by", "Chris")
        for argv in (("--task", "T-1"), ("--task", "T-1", "--replace-task", "T-1"),
                     ("--task", "T-2"), ("--task", "T-2", "--replace-task", "T-9")):
            with pytest.raises(SystemExit):
                _dx(monkeypatch, "learn", "link", key, *argv, "--by", "Pat")
        _dx(monkeypatch, "learn", "link", key, "--task", "T-2", "--replace-task", "T-1", "--by", "Pat")
        assert JsonStore(store_dir).load_adoptions()[key].task_id == "T-2"
        assert _audit_actions(store_dir).count("link") == 2

    def test_decision_bound_to_what_the_human_saw(self, tmp_path, pxx, monkeypatch, capsys):
        lint_world(pxx)
        monkeypatch.setenv("DX_LEARN_STORE", str(tmp_path / "store"))
        monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
        _dx(monkeypatch, "learn", "run", "--no-ledger", "--json", "--now", NOW.isoformat())
        key = json.loads(capsys.readouterr().out)["proposed"][0]
        _dx(monkeypatch, "learn", "show", key, "--json")
        seen = json.loads(capsys.readouterr().out)["seen_token"]
        # the evidence moves under the human (another pass rewrites the proposal)
        _dx(monkeypatch, "learn", "run", "--no-ledger", "--json", "--now", ago(-1).isoformat())
        capsys.readouterr()
        with pytest.raises(SystemExit):
            _dx(monkeypatch, "learn", "accept", key, "--by", "Chris", "--seen", seen)
        assert "changed since you looked" in capsys.readouterr().err
        _dx(monkeypatch, "learn", "show", key, "--json")
        fresh = json.loads(capsys.readouterr().out)["seen_token"]
        _dx(monkeypatch, "learn", "accept", key, "--by", "Chris", "--seen", fresh)
        assert JsonStore(tmp_path / "store").load_recs()[key].status == "ACCEPTED"


# --------------------------------------------------------------------------- finding 2
class TestF2CycleAutoRejectDoesNotHideOurEntry:
    def _auto_reject(self, pxx, slug: str) -> None:
        """What pxx's cycle writes into rejected/: a proposal, a reason, no disposition."""
        path = pxx.root / "inbox" / "rejected" / f"{slug}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"target": "model", "operation": "switch_model",
                                    "hypothesis": "pxx's own", "reason": "cluster has a candidate"}))

    def test_reject_withdraws_even_with_a_cycle_auto_rejection_present(self, tmp_path, pxx):
        variant_world(pxx)
        store = JsonStore(tmp_path / "s")
        recs = [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=recs, audit=False))
        key = recs[0].rec_key
        forward_proposals(store, pxx.root, NOW)
        slug = store.load_forwards()[key].slug
        self._auto_reject(pxx, slug)
        rec = store.load_recs()[key]
        store.commit(Tx("Chris", "reject", key, NOW, writes=[replace(rec, status="REJECTED")]))
        [r] = reconcile_forwards(store, pxx.root, NOW)
        assert r["state"] == "WITHDRAWN"
        assert not list(pxx.root.joinpath(*PENDING).glob("*.json"))

    def test_a_cycle_auto_rejection_does_not_block_or_masquerade_as_a_human(self, tmp_path, pxx):
        variant_world(pxx)
        store = JsonStore(tmp_path / "s")
        recs = [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=recs, audit=False))
        slug = learn_bridge.build_proposal(recs[0])[0]
        self._auto_reject(pxx, slug)
        [res] = forward_proposals(store, pxx.root, NOW)
        assert res["state"] == "WRITTEN" and "human already decided" not in res["note"]


# --------------------------------------------------------------------------- finding 3
class TestF3SharedPxxSlug:
    def _two_classes(self, tmp_path, pxx) -> tuple[JsonStore, list[str]]:
        variant_world(pxx, project="svc-a")
        variant_world(pxx, project="svc-b")
        store = JsonStore(tmp_path / "s")
        recs = [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]
        assert len(recs) == 2
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=recs, audit=False))
        return store, sorted(r.rec_key for r in recs)

    def test_second_proposal_on_the_same_slug_does_not_churn(self, tmp_path, pxx):
        store, keys = self._two_classes(tmp_path, pxx)
        for _ in range(3):
            forward_proposals(store, pxx.root, NOW)
        audit = (store.root / "audit.jsonl").read_text().splitlines()
        assert [json.loads(a)["action"] for a in audit].count("forward-blocked") == 1
        blocked = next(k for k in keys if store.load_forwards()[k].state == "NOT_WRITTEN")
        assert store.load_recs()[blocked].revision == 1        # never bumped by blocked passes

    def test_cycle_overwrite_is_surfaced_and_offered_again(self, tmp_path, pxx, monkeypatch, capsys):
        variant_world(pxx)
        store = JsonStore(tmp_path / "s")
        recs = [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=recs, audit=False))
        key = recs[0].rec_key
        forward_proposals(store, pxx.root, NOW)
        pending = pxx.root.joinpath(*PENDING, f"{store.load_forwards()[key].slug}.json")
        pending.write_text(json.dumps({"target": "model", "operation": "switch_model",
                                       "hypothesis": "pxx's own"}))  # cycle's write_text
        [r] = reconcile_forwards(store, pxx.root, NOW)
        assert r["state"] == "SUPERSEDED" and r["info"]
        monkeypatch.setenv("DX_LEARN_STORE", str(store.root))
        _dx(monkeypatch, "learn", "queue")
        assert "pxx's own cycle replaced" in capsys.readouterr().out
        pending.unlink()                                    # the slug clears
        [res] = forward_proposals(store, pxx.root, NOW)
        assert res["state"] == "WRITTEN"


# --------------------------------------------------------------------------- finding 4
def test_f4_reader_never_sees_a_torn_audit_line(tmp_path, pxx):
    lint_world(pxx)
    root = tmp_path / "s"
    rec = _learn(_corpus(pxx))[0]
    JsonStore(root).commit(Tx("dx learn", "pass", "pass", NOW, writes=[rec], audit=False))
    rec = JsonStore(root).load_recs()[rec.rec_key]
    paused, release = threading.Event(), threading.Event()

    def hook(point: str) -> None:
        if point == "audit-partial":
            paused.set()
            release.wait(timeout=30)
    writer = threading.Thread(target=lambda: JsonStore(root, crash_hook=hook).commit(
        Tx("Chris", "reject", rec.rec_key, NOW, writes=[replace(rec, status="REJECTED")])))
    writer.start()
    assert paused.wait(timeout=30)                       # the writer is mid-append
    result: dict[str, object] = {}
    reader = threading.Thread(target=lambda: result.update(n=JsonStore(root).verify_audit()))
    reader.start()
    reader.join(timeout=1)
    assert reader.is_alive()                             # blocked behind the writer, not reading
    release.set()
    writer.join(timeout=30)
    reader.join(timeout=30)
    assert result == {"n": 1}


# --------------------------------------------------------------------------- finding 5
class TestF5LowBaselines:
    @pytest.mark.parametrize("before,after,n", [(6, 3, 300), (12, 8, 300), (16, 12, 400)])
    def test_a_large_relative_fall_at_a_low_baseline_is_never_a_revert(self, tmp_path, pxx,
                                                                      before, after, n):
        for i in range(n):
            pxx.run(when=ago(31 + i * 25 / n), code="ROUND_CAP" if i < before else "COMPLETED")
            pxx.run(when=ago(29 - i * 25 / n), code="ROUND_CAP" if i < after else "COMPLETED")
        c = _corpus(pxx)
        read_ledger(write_ledger(tmp_path / "ledger", [row("T-FIX", "MERGED", ago(30))]), c)
        ad = Adoption("k" * 24, {"name": "class_failure_rate", "key": "svc-a", "source": "pxx"},
                      "Chris", ago(40), task_id="T-FIX")
        learner = Learner(LearnConfig(mc_draws=4000), NOW)
        [r] = learner.efficacy(c, [ad])
        assert r["status"] in ("inconclusive", "improvement-observed")
        assert not [x for x in learner.learn(c, [ad]) if x.kind == "adoption_no_improvement"]


# --------------------------------------------------------------------------- finding 6
class TestF6OverlapAndWindows:
    def _world(self, tmp_path, pxx, merges: list[tuple[str, int]]) -> Corpus:
        for i in range(160):
            pxx.run(when=ago(1 + i), code="ROUND_CAP")
        c = _corpus(pxx)
        read_ledger(write_ledger(tmp_path / "ledger",
                                 [row(t, "MERGED", ago(d)) for t, d in merges]), c)
        return c

    def _ad(self, key: str, task: str, *, active: bool = True) -> Adoption:
        return Adoption(key * 24, {"name": "class_failure_rate", "key": "svc-a", "source": "pxx"},
                        "Chris", ago(150), task_id=task, active=active)

    def test_another_merge_inside_the_after_window_confounds(self, tmp_path, pxx):
        c = self._world(tmp_path, pxx, [("T-A", 100), ("T-B", 70)])
        [r] = Learner(LearnConfig(mc_draws=4000), NOW).efficacy(
            c, [self._ad("a", "T-A")], [self._ad("a", "T-A"), self._ad("b", "T-B")])
        assert r["status"] == "inconclusive (confounded)"

    REVERT_KINDS = ("adoption_no_improvement", "adoption_ineffective")   # current and old name

    def test_a_retired_adoption_still_confounds(self, tmp_path, pxx):
        c = self._world(tmp_path, pxx, [("T-A", 100), ("T-B", 70)])
        learner = Learner(LearnConfig(mc_draws=4000), NOW)
        # control: on its own, T-A's flat rate does raise the review prompt...
        assert [x for x in learner.learn(c, [self._ad("a", "T-A")])
                if x.kind in self.REVERT_KINDS]
        # ...but a retired adoption on the same metric inside the window voids it
        retired = self._ad("b", "T-B", active=False)
        assert not [x for x in learner.learn(c, [self._ad("a", "T-A"), retired])
                    if x.kind in self.REVERT_KINDS]

    def test_after_window_is_bounded(self, tmp_path, pxx):
        c = self._world(tmp_path, pxx, [("T-A", 100)])
        [r] = Learner(LearnConfig(mc_draws=4000), NOW).efficacy(c, [self._ad("a", "T-A")])
        assert r["runs_after"] == 60        # 60 days of daily runs, not everything since


# --------------------------------------------------------------------------- findings 7 and 8
class TestF7F8SqlServerDriverRealism:
    def test_updates_read_rowcount_explicitly_under_nocount(self, pxx):
        db = FakeSqlServer()                  # cursor.rowcount is -1, as under NOCOUNT
        store = MssqlStore("DSN=x", connect=db.connect, acknowledge_unqualified=True)
        lint_world(pxx)
        rec = _learn(_corpus(pxx))[0]
        store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[rec], audit=False))
        store.commit(Tx("Chris", "reject", rec.rec_key, NOW, writes=[replace(rec, status="REJECTED")]))
        assert any(q.startswith("UPDATE") and "SELECT @@ROWCOUNT" in q for q in db.log)

    def test_failed_applock_is_reported_and_never_released(self):
        db = FakeSqlServer()
        db.applock_result = -1                # timeout
        store = MssqlStore("DSN=x", connect=db.connect, acknowledge_unqualified=True)
        with pytest.raises(StoreError, match="returned -1"):
            with store.mutation_lock():
                pass
        assert not any("sp_releaseapplock" in q for q in db.log)

    def test_acquired_applock_is_released(self):
        db = FakeSqlServer()
        store = MssqlStore("DSN=x", connect=db.connect, acknowledge_unqualified=True)
        with store.mutation_lock():
            assert db.applock_held == 1
        assert db.applock_held == 0


# --------------------------------------------------------------------------- finding 9
def test_f9_accept_whose_spec_write_fails_can_regenerate_it(tmp_path, pxx, monkeypatch, capsys):
    lint_world(pxx)
    monkeypatch.setenv("DX_LEARN_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
    _dx(monkeypatch, "learn", "run", "--no-ledger", "--json", "--now", NOW.isoformat())
    key = json.loads(capsys.readouterr().out)["proposed"][0]
    out = tmp_path / "spec.md"
    real = cmd_learn.render_spec

    def racing_render(*a, **k):        # another process creates the file after our check
        out.write_text("someone else's")
        return real(*a, **k)
    monkeypatch.setattr(cmd_learn, "render_spec", racing_render)
    with pytest.raises(SystemExit):
        _dx(monkeypatch, "learn", "accept", key, "--by", "Chris", "--spec-out", str(out))
    assert f"dx learn spec {key[:8]}" in capsys.readouterr().err
    assert out.read_text() == "someone else's"
    monkeypatch.undo()
    monkeypatch.setenv("DX_LEARN_STORE", str(tmp_path / "store"))
    _dx(monkeypatch, "learn", "spec", key, "--out", str(tmp_path / "spec2.md"))
    assert "Pre-registered success measure" in (tmp_path / "spec2.md").read_text()


# --------------------------------------------------------------------------- finding 11
def test_f11_stale_forward_is_withdrawn_and_offered_again(tmp_path, pxx):
    variant_world(pxx)
    store = JsonStore(tmp_path / "s")
    recs = [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]
    store.commit(Tx("dx learn", "pass", "pass", NOW, writes=recs, audit=False))
    key = recs[0].rec_key
    forward_proposals(store, pxx.root, NOW)
    rec = store.load_recs()[key]
    store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[replace(rec, status="STALE")],
                    audit=False))
    [r] = reconcile_forwards(store, pxx.root, NOW)
    assert r["state"] == "WITHDRAWN" and not list(pxx.root.joinpath(*PENDING).glob("*.json"))
    rec = store.load_recs()[key]
    store.commit(Tx("dx learn", "pass", "pass", NOW, writes=[replace(rec, status="PROPOSED")],
                    audit=False))
    [res] = forward_proposals(store, pxx.root, NOW)
    assert res["state"] == "WRITTEN"
