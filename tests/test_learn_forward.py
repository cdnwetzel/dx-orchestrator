"""Finding R3 (safe pxx forwarding).

Forwarding used to happen before the pass's own concurrency check, so a
proposal a human rejected while the pass ran could still be written into pxx's
inbox. It now runs only inside the mutation lock every decision takes, after
the pass's write, with a durable INTENT committed against the recommendation's
revision before any pxx write and an outcome committed after. These tests
cover the decision race, interruption at each step, duplicate prevention, and
pxx's unlocked triage path (driven with pxx's real `triage.dispose`).
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dx import learn_bridge, learn_forward
from dx.learn_forward import forward_proposals, reconcile_forwards
from dx.learn_store import JsonStore, Tx
from learn_helpers import NOW, SimulatedCrash, _corpus, _dx, _learn, variant_world

PENDING = ("inbox", "human-review-required")


def _seeded(tmp_path, pxx) -> tuple[JsonStore, str]:
    variant_world(pxx)
    store = JsonStore(tmp_path / "s")
    recs = [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]
    store.commit(Tx("dx learn", "pass", "pass", NOW, writes=recs, audit=False))
    return store, recs[0].rec_key


def _pending(pxx) -> list[Path]:
    return sorted((pxx.root.joinpath(*PENDING)).glob("*.json"))


def _actions(store: JsonStore) -> list[str]:
    path = store.root / "audit.jsonl"
    return [json.loads(line)["action"] for line in path.read_text().splitlines()] \
        if path.exists() else []


def _reject(store: JsonStore, key: str) -> None:
    rec = store.load_recs()[key]
    store.commit(Tx("Chris", "reject", key, NOW, writes=[replace(rec, status="REJECTED")]))


class TestForwardOrdering:
    def test_intent_then_write_then_outcome(self, tmp_path, pxx):
        store, key = _seeded(tmp_path, pxx)
        [res] = forward_proposals(store, pxx.root, NOW)
        assert res["state"] == "WRITTEN" and len(_pending(pxx)) == 1
        assert store.load_forwards()[key].state == "WRITTEN"
        assert _actions(store) == ["forward-intent", "forward-outcome"]

    def test_rejected_before_forwarding_is_never_forwarded(self, tmp_path, pxx):
        store, key = _seeded(tmp_path, pxx)
        _reject(store, key)
        assert forward_proposals(store, pxx.root, NOW) == []
        assert _pending(pxx) == [] and key not in store.load_forwards()

    def test_stale_read_cannot_forward_a_rejected_proposal(self, tmp_path, pxx, monkeypatch):
        """Even if the forwarder's view were stale (the race the old order had),
        the INTENT commit is checked against the recommendation's revision."""
        store, key = _seeded(tmp_path, pxx)
        stale = store.load_recs()                     # read before the reject...
        _reject(store, key)                           # ...a human rejects...
        monkeypatch.setattr(store, "load_recs", lambda: stale)   # ...forwarder sees the old view
        [res] = forward_proposals(store, pxx.root, NOW)
        assert res["state"] == "SKIPPED" and "decided while" in res["note"]
        assert _pending(pxx) == [] and key not in store.load_forwards()

    def test_cli_pass_with_forwarding(self, tmp_path, pxx, monkeypatch, capsys):
        variant_world(pxx)
        monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
        _dx(monkeypatch, "learn", "run", "--no-ledger", "--forward-pxx", "--json",
            "--now", NOW.isoformat())
        summary = json.loads(capsys.readouterr().out)
        assert [f["state"] for f in summary["forwarded"]] == ["WRITTEN"]
        assert len(_pending(pxx)) == 1


class TestInterruption:
    def test_crash_after_intent_before_write_resolves_and_writes_once(self, tmp_path, pxx,
                                                                     monkeypatch):
        store, key = _seeded(tmp_path, pxx)

        def die(*_a, **_k):
            raise SimulatedCrash("before pxx write")
        monkeypatch.setattr(learn_forward, "write_pending", die)
        with pytest.raises(SimulatedCrash):
            forward_proposals(store, pxx.root, NOW)
        monkeypatch.undo()
        store = JsonStore(store.root)
        assert store.load_forwards()[key].state == "INTENT" and _pending(pxx) == []
        [row] = reconcile_forwards(store, pxx.root, NOW)
        assert row["state"] == "INTERRUPTED"
        [res] = forward_proposals(store, pxx.root, NOW)     # retried once, written once
        assert res["state"] == "WRITTEN" and len(_pending(pxx)) == 1

    def test_crash_after_write_before_outcome_never_duplicates(self, tmp_path, pxx, monkeypatch):
        store, key = _seeded(tmp_path, pxx)
        real_commit = store.commit

        def commit(tx, **kw):
            if tx.action == "forward-outcome":
                raise SimulatedCrash("before outcome")
            return real_commit(tx, **kw)
        monkeypatch.setattr(store, "commit", commit)
        with pytest.raises(SimulatedCrash):
            forward_proposals(store, pxx.root, NOW)
        monkeypatch.undo()
        store = JsonStore(store.root)
        assert store.load_forwards()[key].state == "INTENT" and len(_pending(pxx)) == 1
        [row] = reconcile_forwards(store, pxx.root, NOW)
        assert row["state"] == "WRITTEN"
        assert forward_proposals(store, pxx.root, NOW) == []   # not eligible: no second write
        assert len(_pending(pxx)) == 1

    def test_interrupted_intent_whose_entry_a_pxx_human_already_decided(self, tmp_path, pxx,
                                                                         monkeypatch):
        from pxx.improve.triage import dispose
        store, key = _seeded(tmp_path, pxx)
        real_commit = store.commit

        def commit(tx, **kw):
            if tx.action == "forward-outcome":
                raise SimulatedCrash("before outcome")
            return real_commit(tx, **kw)
        monkeypatch.setattr(store, "commit", commit)
        with pytest.raises(SimulatedCrash):
            forward_proposals(store, pxx.root, NOW)
        monkeypatch.undo()
        store = JsonStore(store.root)
        dispose(pxx.root, store.load_forwards()[key].slug, qualify=True, by="Pat")
        [row] = reconcile_forwards(store, pxx.root, NOW)
        assert row["state"] == "WRITTEN" and row["pxx_view"] == "qualified"
        assert forward_proposals(store, pxx.root, NOW) == []   # never re-raised in pxx

    def test_reject_whose_withdraw_was_interrupted_is_settled_next_pass(self, tmp_path, pxx):
        store, key = _seeded(tmp_path, pxx)
        forward_proposals(store, pxx.root, NOW)
        _reject(store, key)                       # decision landed; withdraw never ran
        assert len(_pending(pxx)) == 1
        [row] = reconcile_forwards(store, pxx.root, NOW)
        assert row["state"] == "WITHDRAWN" and _pending(pxx) == []


class TestPxxTriageIsUnlocked:
    """pxx's `improve triage` takes no lock. dx cannot prevent a pxx human from
    deciding at the same time; it must never hide it."""

    def test_reject_withdraws_our_still_pending_entry(self, tmp_path, pxx, monkeypatch, capsys):
        store, key = _seeded(tmp_path, pxx)
        forward_proposals(store, pxx.root, NOW)
        monkeypatch.setenv("DX_LEARN_STORE", str(store.root))
        monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
        _dx(monkeypatch, "learn", "reject", key, "--by", "Chris", "--note", "not this quarter")
        assert "WITHDRAWN" in capsys.readouterr().out and _pending(pxx) == []
        assert JsonStore(store.root).load_forwards()[key].state == "WITHDRAWN"

    def test_pxx_human_qualified_while_dx_rejected_is_a_recorded_divergence(self, tmp_path, pxx,
                                                                            monkeypatch, capsys):
        from pxx.improve.triage import dispose
        store, key = _seeded(tmp_path, pxx)
        forward_proposals(store, pxx.root, NOW)
        slug = store.load_forwards()[key].slug
        dispose(pxx.root, slug, qualify=True, by="Pat")          # pxx human, no lock
        monkeypatch.setenv("DX_LEARN_STORE", str(store.root))
        monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
        _dx(monkeypatch, "learn", "reject", key, "--by", "Chris", "--note", "no")
        out = capsys.readouterr().out
        assert "DIVERGED" in out and "pxx human qualified" in out
        fwd = JsonStore(store.root).load_forwards()[key]
        assert fwd.divergence and fwd.pxx_view == "qualified"
        # never auto-resolved: pxx's decision is untouched
        assert (pxx.root / "inbox" / "qualified" / f"{slug}.json").exists()
        _dx(monkeypatch, "learn", "queue")
        assert "decided differently" in capsys.readouterr().out
        assert "forward-reconcile" in _actions(JsonStore(store.root))

    def test_triage_that_read_our_entry_before_we_withdrew_it(self, tmp_path, pxx):
        """triage: read pending → [dx withdraws] → write qualified → unlink fails.
        pxx ends up with a qualified copy of a proposal dx rejected."""
        store, key = _seeded(tmp_path, pxx)
        forward_proposals(store, pxx.root, NOW)
        fwd = store.load_forwards()[key]
        entry = json.loads(_pending(pxx)[0].read_text())             # triage reads
        _reject(store, key)
        reconcile_forwards(store, pxx.root, NOW)                      # dx withdraws
        assert store.load_forwards()[key].state == "WITHDRAWN"
        entry["disposition"] = {"verdict": "qualified", "decided_by": "Pat"}
        qualified = pxx.root / "inbox" / "qualified" / f"{fwd.slug}.json"
        qualified.parent.mkdir(parents=True, exist_ok=True)
        qualified.write_text(json.dumps(entry))                       # triage writes
        [row] = reconcile_forwards(store, pxx.root, NOW)
        assert row["divergence"] and "qualified" in row["divergence"]

    def test_human_decides_in_pxx_between_our_create_and_recheck(self, tmp_path, pxx,
                                                                 monkeypatch):
        from pxx.improve.triage import dispose
        store, key = _seeded(tmp_path, pxx)
        real_inspect = learn_bridge.inspect
        calls = {"n": 0}

        def racing_inspect(state_dir, slug, fp):
            calls["n"] += 1
            if calls["n"] == 2:            # after our "x" create, before our re-check
                dispose(state_dir, slug, qualify=False, note="pxx says no", by="Pat")
            return real_inspect(state_dir, slug, fp)
        monkeypatch.setattr(learn_bridge, "inspect", racing_inspect)
        [res] = forward_proposals(store, pxx.root, NOW)
        assert res["state"] == "WRITTEN" and "rejected it at once" in res["note"]
        assert _pending(pxx) == []

    def test_dx_accepted_but_pxx_rejected(self, tmp_path, pxx):
        from pxx.improve.triage import dispose
        store, key = _seeded(tmp_path, pxx)
        forward_proposals(store, pxx.root, NOW)
        dispose(pxx.root, store.load_forwards()[key].slug, qualify=False, note="no", by="Pat")
        rec = store.load_recs()[key]
        store.commit(Tx("Chris", "accept", key, NOW, writes=[replace(rec, status="ACCEPTED")]))
        [row] = reconcile_forwards(store, pxx.root, NOW)
        assert row["divergence"] == "dx ACCEPTED it, but a pxx human rejected it"

    def test_slug_occupied_by_pxx_cycle_entry_is_retried_not_clobbered(self, tmp_path, pxx):
        store, key = _seeded(tmp_path, pxx)
        slug = learn_bridge.build_proposal(store.load_recs()[key])[0]
        theirs = pxx.root.joinpath(*PENDING, f"{slug}.json")
        theirs.parent.mkdir(parents=True)
        theirs.write_text(json.dumps({"target": "model", "operation": "switch_model",
                                      "hypothesis": "pxx's own"}))
        [res] = forward_proposals(store, pxx.root, NOW)
        assert res["state"] == "NOT_WRITTEN" and res["note"].startswith("retry:")
        assert json.loads(theirs.read_text())["hypothesis"] == "pxx's own"
        theirs.unlink()                                  # pxx's entry is triaged away
        [res] = forward_proposals(store, pxx.root, NOW)  # so dx retries
        assert res["state"] == "WRITTEN" and len(_pending(pxx)) == 1

    def test_withdraw_postponed_while_cycle_lock_is_held(self, tmp_path, pxx):
        import fcntl
        store, key = _seeded(tmp_path, pxx)
        forward_proposals(store, pxx.root, NOW)
        _reject(store, key)
        with (pxx.root / "cycle.lock").open("w") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)      # a pxx cycle is running
            [row] = reconcile_forwards(store, pxx.root, NOW)
            assert "withdraw pending" in row["note"] and len(_pending(pxx)) == 1
        [row] = reconcile_forwards(store, pxx.root, NOW)
        assert row["state"] == "WITHDRAWN"


class TestNeverTouchesForeignEntries:
    def test_create_never_clobbers_an_entry_that_appears_after_the_check(self, tmp_path, pxx,
                                                                         monkeypatch):
        store, key = _seeded(tmp_path, pxx)
        slug, payload, fp = learn_bridge.build_proposal(store.load_recs()[key])
        theirs = pxx.root.joinpath(*PENDING, f"{slug}.json")
        real_inspect = learn_bridge.inspect
        calls = {"n": 0}

        def late_writer(state_dir, s, f):
            calls["n"] += 1
            if calls["n"] == 1:          # our pre-check sees nothing...
                theirs.parent.mkdir(parents=True, exist_ok=True)
                theirs.write_text(json.dumps({"hypothesis": "pxx cycle wrote this"}))
                return "absent"          # ...and pxx's cycle writes just after it
            return real_inspect(state_dir, s, f)
        monkeypatch.setattr(learn_bridge, "inspect", late_writer)
        view = learn_bridge.write_pending(pxx.root, slug, payload, fp)
        assert view == "pending-other"
        assert json.loads(theirs.read_text()) == {"hypothesis": "pxx cycle wrote this"}

    def test_withdraw_leaves_a_foreign_pending_entry_alone(self, tmp_path, pxx):
        store, key = _seeded(tmp_path, pxx)
        slug, _payload, fp = learn_bridge.build_proposal(store.load_recs()[key])
        theirs = pxx.root.joinpath(*PENDING, f"{slug}.json")
        theirs.parent.mkdir(parents=True)
        theirs.write_text(json.dumps({"hypothesis": "not ours"}))
        assert learn_bridge.withdraw_if_ours(pxx.root, slug, fp) == "pending-other"
        assert theirs.exists()
