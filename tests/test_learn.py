"""dx learn — hermetic tests over synthetic pxx runs, ledgers, audit logs and manifests.

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

from dx import cmd_learn
from dx.cli import build_parser
from dx.learn_bridge import FORWARDABLE_KINDS, ForwardError, forward_to_pxx, render_spec
from dx.learn_engine import LearnConfig, Learner, observations, reconcile
from dx.learn_model import REC_KINDS, Adoption, Corpus
from dx.learn_signature import fingerprint, normalize
from dx.learn_sources import (
    LearnSourceError,
    link_attempts,
    read_ledger,
    read_operator_audit,
    read_pxx_runs,
    read_review_manifests,
)
from dx.learn_store import JsonStore, MssqlStore, StoreError

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


# --------------------------------------------------------------------------- fixtures: pxx
class PxxState:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "runs").mkdir(parents=True, exist_ok=True)
        self.n = 0

    def run(self, *, when: datetime, project: str = "svc-a", task: str | None = None,
            model: str = "glm", code: str = "COMPLETED", summary: str = "",
            gates: list[dict[str, Any]] | None = None, contributing: list[str] | None = None) -> str:
        self.n += 1
        run_id = f"20261001T000000Z-{self.n:08x}"
        d = self.root / "runs" / run_id
        d.mkdir()
        ts = when.timestamp()
        (d / "outcome.json").write_text(json.dumps({
            "run_id": run_id, "code": code, "summary": summary, "ts": ts,
            "contributing_codes": contributing or [], "agent_version_id": "a" * 16}))
        (d / "task.json").write_text(json.dumps({
            "run_id": run_id, "task_id": task or f"task-{self.n}", "project": project, "ts": ts}))
        (d / "manifest.json").write_text(json.dumps({"provider": "local", "model": model}))
        with (d / "events.jsonl").open("w") as fh:
            for g in gates or []:
                fh.write(json.dumps({"kind": "gate_decision", "data": g, "ts": ts,
                                     "session_id": "s", "seq": 1}) + "\n")
        return run_id


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


# --------------------------------------------------------------------------- signatures
class TestSignature:
    def test_volatile_parts_collapse_to_one_signature(self):
        a = fingerprint("TEST_REGRESSION", "round 3: new failures: tests/api/test_x.py::test_a "
                        "line 42 id 1b4e28ba-2fa1-11d2-883f-0016d3cca427")
        b = fingerprint("TEST_REGRESSION", "round 9: new failures: src/other/test_x.py::test_a "
                        "line 7 id 9f4e28ba-2fa1-11d2-883f-0016d3cca427")
        assert a.signature_id == b.signature_id

    def test_where_it_surfaced_is_part_of_the_signature(self):
        assert fingerprint("tests", "boom").signature_id != fingerprint("lint", "boom").signature_id

    def test_last_error_line_wins(self):
        assert "valueerror" in normalize("Traceback\n  File x\nValueError: bad 12")


# --------------------------------------------------------------------------- pxx reader
class TestPxxReader:
    def test_in_flight_runs_are_counted_not_guessed(self, pxx):
        pxx.run(when=ago(1))
        (pxx.root / "runs" / "20261001T000000Z-inflight").mkdir()
        c = Corpus()
        read_pxx_runs(pxx.root, c)
        assert len(c.attempts) == 1 and "1 without outcome.json" in c.sources["pxx"]

    def test_malformed_run_is_quarantined_and_named(self, pxx):
        good = pxx.run(when=ago(1))
        bad = pxx.run(when=ago(1))
        (pxx.root / "runs" / bad / "outcome.json").write_text("{not json")
        c = Corpus()
        read_pxx_runs(pxx.root, c)
        assert [a.run_id for a in c.attempts] == [good]
        assert c.quarantined and bad in c.quarantined[0].where

    def test_run_id_must_match_its_directory(self, pxx):
        rid = pxx.run(when=ago(1))
        p = pxx.root / "runs" / rid / "outcome.json"
        p.write_text(json.dumps({**json.loads(p.read_text()), "run_id": "someone-else"}))
        c = Corpus()
        read_pxx_runs(pxx.root, c)
        assert not c.attempts and c.quarantined

    def test_missing_runs_dir_refuses(self, tmp_path):
        with pytest.raises(LearnSourceError, match="no pxx runs directory"):
            read_pxx_runs(tmp_path / "nowhere", Corpus())


# --------------------------------------------------------------------------- ledger reader
class TestLedgerReader:
    def test_reads_a_verified_chain_and_links_runs(self, tmp_path, pxx):
        rid = pxx.run(when=ago(3))
        repo = write_ledger(tmp_path / "ledger", [
            row("T-1", "ADMITTED", ago(4)),
            row("T-1", "EXECUTED", ago(3), f"mode=DX_RUN scope=billing commit=abc run={rid} model=x"),
            row("T-1", "MERGED", ago(2), "merged")])
        c = Corpus()
        read_pxx_runs(pxx.root, c)
        read_ledger(repo, c)
        assert link_attempts(c) == 1
        assert c.attempts[0].dx_task_id == "T-1" and c.attempts[0].task_class == "billing"
        assert c.tasks["T-1"].merged_at() == ago(2).replace(microsecond=0)

    def test_broken_chain_stops_the_pass(self, tmp_path):
        repo = write_ledger(tmp_path / "ledger", [row("T-1", "ADMITTED", ago(4)),
                                                  row("T-1", "MERGED", ago(2))])
        lines = (repo / "ledger.jsonl").read_text().splitlines()
        lines[1] = lines[1].replace("ADMITTED", "ABANDONED")   # edit a middle row
        (repo / "ledger.jsonl").write_text("\n".join(lines) + "\n")
        with pytest.raises(LearnSourceError, match="did not verify"):
            read_ledger(repo, Corpus())

    def test_reference_ledger_is_refused_unless_allowed(self, tmp_path):
        repo = write_ledger(tmp_path / "devswarm-ledger-reference", [row("T-1", "ADMITTED", ago(1))])
        with pytest.raises(LearnSourceError, match="reference ledger"):
            read_ledger(repo, Corpus())
        c = Corpus()
        read_ledger(repo, c, allow_reference=True)
        assert "T-1" in c.tasks

    def test_corrected_task_is_set_aside_not_guessed(self, tmp_path):
        repo = write_ledger(tmp_path / "ledger", [
            row("T-1", "REDLINE", ago(3), "wrong"), row("T-1", "CORRECTION", ago(2), "row x wrong"),
            row("T-2", "MERGED", ago(1))])
        c = Corpus()
        read_ledger(repo, c)
        assert "T-1" not in c.tasks and "T-2" in c.tasks
        assert any(q.where == "T-1" for q in c.quarantined)


# --------------------------------------------------------------------------- psoperator + review readers
class TestOperatorAndReview:
    def test_verified_audit_is_read(self, tmp_path):
        path = write_audit(tmp_path / "audit.jsonl", [("executed", "click", "ok"),
                                                      ("execution-failed", "click", "boom 1")])
        c = Corpus()
        read_operator_audit(path, c)
        assert [d.decision for d in c.operator] == ["executed", "execution-failed"]

    def test_tampered_audit_stops_the_pass(self, tmp_path):
        path = write_audit(tmp_path / "audit.jsonl", [("executed", "click", "ok"),
                                                      ("denied", "type", "approval denied/timed out")])
        path.write_text(path.read_text().replace("denied", "executed", 1))
        with pytest.raises(LearnSourceError):
            read_operator_audit(path, Corpus())

    def test_drill_indistinguishable_stops_are_ignored(self, tmp_path):
        path = write_audit(tmp_path / "audit.jsonl", [("kill-switched", "click", "stop")])
        c = Corpus()
        read_operator_audit(path, c)
        assert c.operator == []

    def test_review_manifest_rows(self, tmp_path):
        d = tmp_path / "review" / "v1"
        d.mkdir(parents=True)
        (d / "manifest.tsv").write_text("id\tlane\tresult\treceipt\n"
                                        "a\tkimi\tNO_REVIEW\tr.md\nb\tkimi\tMAYBE\tr.md\n")
        c = Corpus()
        read_review_manifests(tmp_path / "review", c)
        assert len(c.lanes) == 1 and len(c.quarantined) == 1


# --------------------------------------------------------------------------- detectors
def _learn(c: Corpus, adoptions: list[Adoption] | None = None):
    return Learner(LearnConfig(mc_draws=4000), NOW).learn(c, adoptions or [])


def _corpus(pxx: PxxState) -> Corpus:
    c = Corpus()
    read_pxx_runs(pxx.root, c)
    return c


class TestDetectors:
    def test_recurring_test_failure_across_tasks(self, pxx):
        for i, t in enumerate(["t1", "t1", "t2", "t3", "t3"]):
            pxx.run(when=ago(i + 1), task=t, code="TEST_REGRESSION", summary="new failures",
                    gates=[{"gate": "tests", "allowed": False,
                            "new_failures": ["tests/test_api.py::test_status"]}])
        pxx.run(when=ago(0.5), task="t3")   # a later attempt that succeeds
        recs = [r for r in _learn(_corpus(pxx)) if r.kind == "recurring_failure"
                and r.evidence["where"] == "tests"]
        assert len(recs) == 1
        r = recs[0]
        assert r.suggested_role == "sdet" and r.evidence["tasks"] == 3
        assert [x["task"] for x in r.evidence["resolutions"]] == ["t3"]

    def test_one_task_retrying_is_not_recurrence(self, pxx):
        for i in range(6):
            pxx.run(when=ago(i + 1), task="same", code="LINT_BLOCKED", summary="E501")
        assert not [r for r in _learn(_corpus(pxx)) if r.kind == "recurring_failure"]

    def test_variant_preference_needs_credible_separation(self, pxx):
        for i in range(20):
            pxx.run(when=ago(i % 10 + 1), model="qwen",
                    code="COMPLETED" if i % 10 < 3 else "NO_TEST_PROGRESS")
            pxx.run(when=ago(i % 10 + 1), model="glm",
                    code="COMPLETED" if i % 10 < 9 else "NO_TEST_PROGRESS")
        recs = [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]
        assert len(recs) == 1 and recs[0].evidence["best"] == "local/glm"
        assert "local/qwen" in recs[0].evidence["dominates"]

    def test_variant_noise_is_not_a_preference(self, pxx):
        for i in range(10):
            pxx.run(when=ago(i + 1), model="qwen", code="COMPLETED" if i % 2 else "ROUND_CAP")
            pxx.run(when=ago(i + 1), model="glm", code="COMPLETED" if i % 3 else "ROUND_CAP")
        assert not [r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference"]

    def test_gate_regression(self, pxx):
        for i in range(40):
            pxx.run(when=ago(20 + i), gates=[{"gate": "lint", "allowed": i % 20 != 0}])
        for i in range(20):
            pxx.run(when=ago(1 + i * 0.5), gates=[{"gate": "lint", "allowed": i % 2 == 0}])
        recs = [r for r in _learn(_corpus(pxx)) if r.kind == "gate_regression"]
        assert [r.evidence["gate"] for r in recs] == ["lint"]
        assert "Do not relax the gate" in recs[0].draft_text

    def test_human_rejection_theme_flags_honest_green_gap(self, tmp_path, pxx):
        rows = []
        for n in range(3):
            rid = pxx.run(when=ago(5 + n), task=f"T-{n}")     # pxx said COMPLETED
            rows += [row(f"T-{n}", "EXECUTED", ago(5 + n), f"scope=web run={rid}"),
                     row(f"T-{n}", "INCOMPLETE", ago(4 + n),
                         f"stale at execution: frame hash moved since approval "
                         f"({n}ab3c4d5e6f… → {n}9f8e7d6c5b…)")]
        rows.append(row("T-9", "MERGED", ago(1)))
        c = _corpus(pxx)
        read_ledger(write_ledger(tmp_path / "ledger", rows), c)
        link_attempts(c)
        recs = [r for r in _learn(c) if r.kind == "human_rejection_theme"]
        assert len(recs) == 1
        assert recs[0].evidence["pxx_green_then_refused"] == ["T-0", "T-1", "T-2"]

    def test_reviewer_lane_against_preregistered_bar(self, tmp_path):
        d = tmp_path / "review" / "v1"
        d.mkdir(parents=True)
        body = "".join(f"k{i}\tkimi\t{'NO_REVIEW' if i < 9 else 'PASS'}\tr\n" for i in range(12))
        body += "".join(f"c{i}\tcodex\t{'FAIL' if i < 6 else 'PASS'}\tr\n" for i in range(12))
        body += "".join(f"x{i}\tcopilot\tEXCLUDED\tr\n" for i in range(12))
        # kimi's deliberate exclusions must not dilute its failures into a pass
        body += "".join(f"e{i}\tkimi\tEXCLUDED\tr\n" for i in range(40))
        (d / "manifest.tsv").write_text("id\tlane\tresult\treceipt\n" + body)
        c = Corpus()
        read_review_manifests(tmp_path / "review", c)
        lanes = [r.evidence["lane"] for r in _learn(c) if r.kind == "reviewer_lane"]
        # kimi rarely answers; codex FAILs are real reviews; EXCLUDED is never counted
        assert lanes == ["kimi"]

    def test_operator_denials_never_suggest_widening_approval(self, tmp_path):
        path = write_audit(tmp_path / "a.jsonl",
                           [("denied", "delete_row", "approval denied/timed out")] * 4)
        c = Corpus()
        read_operator_audit(path, c)
        recs = [r for r in _learn(c) if r.kind == "operator_failure"]
        assert len(recs) == 1 and "never widen auto-approval" in recs[0].draft_text

    def test_same_records_same_recommendations(self, pxx):
        for i in range(12):
            pxx.run(when=ago(i + 1), task=f"t{i % 4}", code="ROUND_CAP", summary="cap at 25")
        c = _corpus(pxx)
        a = [json.dumps(r.evidence, sort_keys=True) for r in _learn(c)]
        b = [json.dumps(r.evidence, sort_keys=True) for r in _learn(c)]
        assert a == b and a


class TestGuardrails:
    def test_recommendation_kinds_are_closed_and_none_relaxes(self):
        assert REC_KINDS == {"recurring_failure", "variant_preference", "gate_regression",
                             "human_rejection_theme", "reviewer_lane", "operator_failure",
                             "adoption_ineffective"}
        assert FORWARDABLE_KINDS == {"variant_preference"}

    def test_spec_forbids_moving_the_metric_by_loosening(self, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999 broken")
        rec = _learn(_corpus(pxx))[0]
        spec = render_spec(rec, "T-77", accepted_by="Chris")
        assert "Do not loosen any gate" in spec and rec.metric["key"] in spec
        assert "dx learn link" in spec and "not the author" in spec


# --------------------------------------------------------------------------- reconcile + efficacy
class TestQueue:
    def _rec(self, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999 broken")
        return _learn(_corpus(pxx))[0]

    def test_rejected_stays_down_until_evidence_doubles(self, pxx):
        rec = self._rec(pxx)
        cfg = LearnConfig()
        rec.status, rec.decided_by, rec.weight_at_decision = "REJECTED", "Chris", rec.evidence_weight
        again = Learner(cfg, NOW).learn(_corpus(pxx), [])[0]
        assert reconcile([again], {rec.rec_key: rec}, cfg, NOW) == []
        again.evidence_weight = rec.evidence_weight * 2.01
        out = reconcile([again], {rec.rec_key: rec}, cfg, NOW)
        assert out[0].status == "PROPOSED" and out[0].evidence["previous_decision"]["by"] == "Chris"
        assert out[0].draft_text.startswith("[Re-raised")

    def test_unseen_proposal_goes_stale_and_accepted_is_untouched(self, pxx):
        rec = self._rec(pxx)
        cfg = LearnConfig()
        assert reconcile([], {rec.rec_key: rec}, cfg, NOW)[0].status == "STALE"
        rec.status = "ACCEPTED"
        assert reconcile([], {rec.rec_key: rec}, cfg, NOW) == []
        assert reconcile([rec], {rec.rec_key: rec}, cfg, NOW) == []


class TestEfficacy:
    def _setup(self, tmp_path, pxx, *, fixed: bool):
        merged = ago(30)
        for i in range(40):
            when = ago(31 + i) if i < 20 else ago(29 - (i - 20))
            bad = i < 20 or not fixed
            pxx.run(when=when, task=f"t{i % 5}", code="LINT_BLOCKED" if bad else "COMPLETED",
                    summary="E999 broken")
        c = _corpus(pxx)
        read_ledger(write_ledger(tmp_path / "ledger", [row("T-FIX", "MERGED", merged)]), c)
        sig = next(iter(c.signatures))
        ad = Adoption(rec_key="k" * 24, metric={"name": "signature_rate", "key": sig},
                      accepted_by="Chris", accepted_at=ago(40), task_id="T-FIX")
        return c, ad

    def test_effective_when_rate_falls_after_merge(self, tmp_path, pxx):
        c, ad = self._setup(tmp_path, pxx, fixed=True)
        row_ = Learner(LearnConfig(mc_draws=4000), NOW).efficacy(c, [ad])[0]
        assert row_["status"] == "effective"

    def test_ineffective_comes_back_as_a_proposal(self, tmp_path, pxx):
        c, ad = self._setup(tmp_path, pxx, fixed=False)
        recs = Learner(LearnConfig(mc_draws=4000), NOW).learn(c, [ad])
        kinds = [r.kind for r in recs]
        assert "adoption_ineffective" in kinds
        # and the adopted metric is not re-proposed as if new
        assert not [r for r in recs if r.kind == "recurring_failure"
                    and r.metric == ad.metric]

    def test_not_measured_before_the_change_ships(self, pxx):
        pxx.run(when=ago(1), code="LINT_BLOCKED", summary="x")
        c = _corpus(pxx)
        ad = Adoption(rec_key="k" * 24, metric={"name": "signature_rate", "key": "x"},
                      accepted_by="Chris", accepted_at=ago(5))
        assert Learner(LearnConfig(), NOW).efficacy(c, [ad])[0]["status"] == "awaiting-task"
        ad.task_id = "T-NOT-MERGED"
        assert Learner(LearnConfig(), NOW).efficacy(c, [ad])[0]["status"] == "awaiting-merge"

    def test_metric_definitions_are_shared(self, pxx):
        pxx.run(when=ago(1), code="LINT_BLOCKED", summary="x", gates=[{"gate": "lint", "allowed": False}])
        c = _corpus(pxx)
        assert observations({"name": "gate_fail_rate", "key": "lint"}, c)[0][1] is True
        with pytest.raises(ValueError):
            observations({"name": "made-up", "key": "x"}, c)


# --------------------------------------------------------------------------- pxx inbox bridge
class TestForward:
    def _variant(self, pxx):
        for i in range(20):
            pxx.run(when=ago(i % 10 + 1), model="qwen",
                    code="COMPLETED" if i % 10 < 3 else "NO_TEST_PROGRESS")
            pxx.run(when=ago(i % 10 + 1), model="glm",
                    code="COMPLETED" if i % 10 < 9 else "NO_TEST_PROGRESS")
        return next(r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference")

    def test_forwarded_entry_is_readable_by_pxx_triage(self, pxx):
        from pxx.improve.triage import pending
        rec = self._variant(pxx)
        slug, outcome = forward_to_pxx(rec, pxx.root)
        assert outcome.startswith("forwarded")
        [entry] = pending(pxx.root)
        assert entry["slug"] == slug and entry["target"] == "model"
        assert entry["operation"] == "switch_model" and "error" not in entry
        assert entry["confidence"] <= 0.9 and entry["basis"] == "correlation"

    def test_never_clobbers_and_respects_human_disposition(self, pxx):
        from pxx.improve.triage import dispose
        rec = self._variant(pxx)
        slug, _ = forward_to_pxx(rec, pxx.root)
        assert "already pending" in forward_to_pxx(rec, pxx.root)[1]
        dispose(pxx.root, slug, qualify=False, note="not now", by="Chris")
        assert "already dispositioned" in forward_to_pxx(rec, pxx.root)[1]

    def test_non_pxx_kinds_are_not_forwarded(self, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        rec = _learn(_corpus(pxx))[0]
        with pytest.raises(ForwardError):
            forward_to_pxx(rec, pxx.root)


# --------------------------------------------------------------------------- stores
class TestJsonStore:
    def test_decision_audit_chain_detects_edits(self, tmp_path, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        store = JsonStore(tmp_path / "s")
        rec = _learn(_corpus(pxx))[0]
        store.save_recs([rec], NOW)
        for action in ("accept", "link"):
            store.decide(rec, None, actor="Chris", action=action, detail={}, now=NOW)
        assert store.verify_audit() == 2
        audit = tmp_path / "s" / "audit.jsonl"
        audit.write_text(audit.read_text().replace("Chris", "Mallory", 1))
        with pytest.raises(StoreError):
            store.verify_audit()

    def test_decision_audit_chain_detects_a_deleted_row(self, tmp_path, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        store = JsonStore(tmp_path / "s")
        rec = _learn(_corpus(pxx))[0]
        for action in ("accept", "link", "retire"):
            store.decide(rec, None, actor="Chris", action=action, detail={}, now=NOW)
        audit = tmp_path / "s" / "audit.jsonl"
        lines = audit.read_text().splitlines()
        audit.write_text("\n".join(lines[1:]) + "\n")   # every remaining row is self-consistent
        with pytest.raises(StoreError, match="prev_hash"):
            store.verify_audit()

    def test_unreadable_store_is_never_overwritten(self, tmp_path):
        (tmp_path / "s").mkdir()
        (tmp_path / "s" / "recommendations.json").write_text("{oops")
        with pytest.raises(StoreError, match="will not overwrite"):
            JsonStore(tmp_path / "s").save_recs([], NOW)


class _FakeCursor:
    def __init__(self, log: list[tuple[str, Any]], fail_on: str | None) -> None:
        self.log, self.fail_on, self.fast_executemany = log, fail_on, False

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        assert sql.count("?") == len(params or []), f"placeholder/param mismatch in: {sql[:60]}"
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        self.log.append(("execute", sql.split()[2] if sql.strip().startswith("SET") else sql.split()[1]))

    def executemany(self, sql: str, rows: list[list[Any]]) -> None:
        for r in rows:
            assert sql.count("?") == len(r)
        self.log.append(("executemany", sql.split()[1]))

    def fetchone(self) -> list[int]:
        return [7]

    def fetchall(self) -> list[Any]:
        return []

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, fail_on: str | None = None) -> None:
        self.log: list[tuple[str, Any]] = []
        self.fail_on = fail_on

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.log, self.fail_on)

    def commit(self) -> None:
        self.log.append(("commit", None))

    def rollback(self) -> None:
        self.log.append(("rollback", None))


class TestMssqlStoreShape:
    """No SQL Server in CI: these pin parameter counts and transaction shape only."""

    def _store(self, conn: _FakeConn) -> MssqlStore:
        return MssqlStore("DSN=x", connect=lambda *a, **k: conn)

    def test_decide_audits_first_in_one_transaction(self, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        rec = _learn(_corpus(pxx))[0]
        conn = _FakeConn()
        ad = Adoption(rec.rec_key, dict(rec.metric), "Chris", NOW)
        self._store(conn).decide(rec, ad, actor="Chris", action="accept", detail={}, now=NOW)
        assert conn.log[0] == ("execute", "dx_learn.DecisionAudit")
        assert conn.log[-1] == ("commit", None)

    def test_failure_rolls_back(self, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        rec = _learn(_corpus(pxx))[0]
        conn = _FakeConn(fail_on="dx_learn.Recommendation WITH")
        with pytest.raises(RuntimeError):
            self._store(conn).decide(rec, None, actor="Chris", action="accept", detail={}, now=NOW)
        assert conn.log[-1] == ("rollback", None)

    def test_record_pass_writes_facts(self, pxx):
        pxx.run(when=ago(1))
        conn = _FakeConn()
        self._store(conn).record_pass({"x": 1}, _corpus(pxx), NOW)
        assert ("executemany", "dx_learn.Attempt") in conn.log

    def test_schema_ships_with_the_package(self):
        import dx
        sql = (Path(dx.__file__).parent / "learn_mssql.sql").read_text()
        assert "INSTEAD OF UPDATE, DELETE" in sql and "dx_learn.Recommendation" in sql


# --------------------------------------------------------------------------- CLI end to end
def _dx(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["dx", *argv])
    args = build_parser().parse_args(list(argv))
    args.func(args)


class TestCli:
    def _world(self, tmp_path, pxx, monkeypatch):
        rows = []
        for i, t in enumerate(["T-1", "T-2", "T-3", "T-4"]):
            rid = pxx.run(when=ago(i + 2), task=t, code="LINT_BLOCKED", summary="E999 syntax")
            rows.append(row(t, "EXECUTED", ago(i + 2), f"scope=api run={rid}"))
        monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
        monkeypatch.setenv("DX_LEDGER_REPO", str(write_ledger(tmp_path / "ledger", rows)))

    def test_run_queue_accept_link_efficacy(self, tmp_path, pxx, monkeypatch, capsys):
        self._world(tmp_path, pxx, monkeypatch)
        _dx(monkeypatch, "learn", "run", "--json", "--now", NOW.isoformat())
        summary = json.loads(capsys.readouterr().out)
        assert summary["proposed"] and summary["sources"]["links"].startswith("4 ")
        key = summary["proposed"][0]

        _dx(monkeypatch, "learn", "queue")
        assert key[:8] in capsys.readouterr().out

        spec = tmp_path / "spec.md"
        spec.write_text("someone's spec")
        with pytest.raises(SystemExit):          # an existing spec is never overwritten...
            _dx(monkeypatch, "learn", "accept", key, "--by", "Chris", "--spec-out", str(spec))
        assert spec.read_text() == "someone's spec"
        _dx(monkeypatch, "learn", "queue")      # ...and nothing was accepted
        assert key[:8] in capsys.readouterr().out.split("Open proposals:")[1]
        spec.unlink()
        _dx(monkeypatch, "learn", "accept", key[:10], "--by", "Chris", "--spec-out", str(spec))
        assert spec.exists() and "Pre-registered success measure" in spec.read_text()
        with pytest.raises(SystemExit):          # an accepted proposal cannot be accepted again
            _dx(monkeypatch, "learn", "accept", key, "--by", "Chris")
        capsys.readouterr()

        _dx(monkeypatch, "learn", "link", key, "--task", "T-FIX", "--by", "Chris")
        _dx(monkeypatch, "learn", "efficacy", "--json", "--now", NOW.isoformat())
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(out)[0]["status"] == "awaiting-merge"

    def test_dx_cannot_decide_its_own_proposals(self, tmp_path, pxx, monkeypatch, capsys):
        self._world(tmp_path, pxx, monkeypatch)
        _dx(monkeypatch, "learn", "run", "--json", "--now", NOW.isoformat())
        key = json.loads(capsys.readouterr().out)["proposed"][0]
        for who in ("dx", " DX learn ", "", "claude"):
            with pytest.raises(SystemExit) as exc:
                _dx(monkeypatch, "learn", "accept", key, "--by", who)
            assert exc.value.code == 1
        assert "separation of duties" in capsys.readouterr().err

    def test_bad_source_leaves_queue_untouched(self, tmp_path, pxx, monkeypatch, capsys):
        self._world(tmp_path, pxx, monkeypatch)
        _dx(monkeypatch, "learn", "run", "--json", "--now", NOW.isoformat())
        capsys.readouterr()
        store = Path(cmd_learn.os.environ["DX_LEARN_STORE"]) / "recommendations.json"
        before = store.read_text()
        ledger = Path(cmd_learn.os.environ["DX_LEDGER_REPO"]) / "ledger.jsonl"
        lines = ledger.read_text().splitlines()
        lines[1] = lines[1].replace("EXECUTED", "MERGED")
        ledger.write_text("\n".join(lines) + "\n")
        with pytest.raises(SystemExit) as exc:
            _dx(monkeypatch, "learn", "run", "--now", NOW.isoformat())
        assert exc.value.code == 1 and "queue is unchanged" in capsys.readouterr().err
        assert store.read_text() == before

    def test_reject_then_cannot_accept(self, tmp_path, pxx, monkeypatch, capsys):
        self._world(tmp_path, pxx, monkeypatch)
        _dx(monkeypatch, "learn", "run", "--json", "--now", NOW.isoformat())
        key = json.loads(capsys.readouterr().out)["proposed"][0]
        _dx(monkeypatch, "learn", "reject", key, "--by", "Chris", "--note", "known, fixing upstream")
        with pytest.raises(SystemExit):
            _dx(monkeypatch, "learn", "accept", key, "--by", "Chris")
        # a second identical pass does not resurrect it
        _dx(monkeypatch, "learn", "run", "--json", "--now", NOW.isoformat())
        assert key not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["proposed"]
        store = JsonStore(Path(cmd_learn.os.environ["DX_LEARN_STORE"]))
        assert store.load_recs()[key].status == "REJECTED"
        assert store.verify_audit() == 1
