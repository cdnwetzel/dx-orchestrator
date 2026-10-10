"""dx learn — hermetic tests over synthetic pxx runs, ledgers, audit logs and manifests.

Per CONTRIBUTING: a gate without a test is a claim. dx learn's gates are the
fail-closed source readers, the human-only decisions, and the rule that a
rejected proposal stays down. Each has a test for the way it could wrongly
pass, not only the way it correctly refuses.
"""
from __future__ import annotations

import hashlib
import json
import re
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
        live = pxx.run(when=ago(0.1))
        (pxx.root / "runs" / live / "outcome.json").unlink()      # the loop is still going
        c = Corpus()
        read_pxx_runs(pxx.root, c)
        # its finished round is folded into the in-flight loop, not counted on its own
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
        ad = Adoption(rec_key="k" * 24, metric={"name": "signature_rate", "key": sig, "source": "pxx"},
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
        ad = Adoption(rec_key="k" * 24, metric={"name": "signature_rate", "key": "x", "source": "pxx"},
                      accepted_by="Chris", accepted_at=ago(5))
        assert Learner(LearnConfig(), NOW).efficacy(c, [ad])[0]["status"] == "awaiting-task"
        ad.task_id = "T-NOT-MERGED"
        assert Learner(LearnConfig(), NOW).efficacy(c, [ad])[0]["status"] == "awaiting-merge"

    def test_metric_definitions_are_shared(self, pxx):
        pxx.run(when=ago(1), code="LINT_BLOCKED", summary="x", gates=[{"gate": "lint", "allowed": False}])
        c = _corpus(pxx)
        assert observations({"name": "gate_fail_rate", "key": "lint", "source": "pxx"}, c)[0][1] is True
        with pytest.raises(ValueError):
            observations({"name": "made-up", "key": "x", "source": "pxx"}, c)


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
        store.save_recs([rec], NOW, {})
        for action in ("accept", "link"):
            store.decide(rec, None, actor="Chris", action=action, detail={}, now=NOW,
                         expected_status="PROPOSED")
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
        store.save_recs([rec], NOW, {})
        for action in ("accept", "link", "retire"):
            store.decide(rec, None, actor="Chris", action=action, detail={}, now=NOW,
                         expected_status="PROPOSED")
        audit = tmp_path / "s" / "audit.jsonl"
        lines = audit.read_text().splitlines()
        audit.write_text("\n".join(lines[1:]) + "\n")   # every remaining row is self-consistent
        with pytest.raises(StoreError, match="prev_hash"):
            store.verify_audit()

    def test_unreadable_store_is_never_overwritten(self, tmp_path):
        (tmp_path / "s").mkdir()
        (tmp_path / "s" / "recommendations.json").write_text("{oops")
        with pytest.raises(StoreError, match="will not overwrite"):
            JsonStore(tmp_path / "s").save_recs([], NOW, {})


class _FakeCursor:
    """Records statements; answers the three reads MssqlStore makes."""

    def __init__(self, conn: _FakeConn) -> None:
        self.conn, self.fast_executemany, self.last = conn, False, ""

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        assert sql.count("?") == len(params or []), f"placeholder/param mismatch in: {sql[:60]}"
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")
        self.last = sql
        words = sql.split()
        self.conn.log.append(("execute", words[2] if words[0] in ("SET", "SELECT") else words[1]))
        self.conn.log.append(("sql", " ".join(words)))
        if "INSERT dx_learn.DecisionAudit" in sql:
            self.conn.audit.append((len(self.conn.audit) + 1, params[4], params[5], params[6]))

    def executemany(self, sql: str, rows: list[list[Any]]) -> None:
        for r in rows:
            assert sql.count("?") == len(r)
        self.conn.log.append(("executemany", sql.split()[1]))

    def fetchone(self) -> Any:
        if "SELECT Status" in self.last:
            return None if self.conn.status is None else [self.conn.status]
        if "SELECT TOP (1) Hash" in self.last:
            return [self.conn.audit[-1][3]] if self.conn.audit else None
        return [7]

    def fetchall(self) -> list[Any]:
        if "FROM dx_learn.DecisionAudit" in self.last:
            return list(self.conn.audit)
        return []

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, fail_on: str | None = None) -> None:
        self.log: list[tuple[str, Any]] = []
        self.fail_on = fail_on
        self.status: str | None = None
        self.audit: list[tuple[int, str, str, str]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

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
        conn.status = "PROPOSED"
        self._store(conn).decide(rec, ad, actor="Chris", action="accept", detail={}, now=NOW,
                                 expected_status="PROPOSED")
        sql = [x for kind, x in conn.log if kind == "sql"]
        order = [next(i for i, q in enumerate(sql) if marker in q) for marker in (
            "SELECT AuditId",                    # chain verified first
            "SELECT Status",                     # status re-read under lock (compare-and-set)
            "SELECT TOP (1) Hash",               # chain tail, locked
            "INSERT dx_learn.DecisionAudit",     # audit row
            "UPDATE dx_learn.Recommendation")]   # then state
        assert order == sorted(order)
        assert conn.log[-1] == ("commit", None)
        assert self._store(conn).verify_audit() == 1   # the row it wrote chains from genesis

    def test_failure_rolls_back(self, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        rec = _learn(_corpus(pxx))[0]
        conn = _FakeConn(fail_on="dx_learn.Recommendation WITH (UPDLOCK, SERIALIZABLE)")
        conn.status = "PROPOSED"
        with pytest.raises(RuntimeError):
            self._store(conn).decide(rec, None, actor="Chris", action="accept", detail={}, now=NOW,
                                     expected_status="PROPOSED")
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


# =========================================================================== review findings
# One class per finding from the independent review of the first cut; each
# test fails against that cut and passes now.

class TestPxxContract:
    """Finding 2: dx run drives `pxx loop`. Its run directory has no manifest and
    its rounds can say COMPLETED inside a failed loop. Checked against REAL
    pxx output, not a fixture's idea of it."""

    def _real_loops(self, tmp_path: Path) -> Path:
        import asyncio

        from pxx.backends.base import BackendCapabilities
        from pxx.config import Settings
        from pxx.loop import run_loop
        from pxx.outcome import RunOutcome, TerminalCode
        from pxx.safety import PermissionMode

        class Backend:
            name = "scripted"
            capabilities = BackendCapabilities(streaming=False, tools=False,
                                               interactive=False, headless=True)

            async def run(self, task: str, ctx: Any) -> RunOutcome:
                (Path(ctx.cwd) / "a.py").write_text("x = 1\n")
                return RunOutcome(code=TerminalCode.COMPLETED, summary="ok", tokens=10)

            async def cancel(self) -> None:
                return None

        class Reviewer:
            async def review(self, diff: str, task: str) -> str:
                return "VERDICT: APPROVE"

        state, work = tmp_path / "state", tmp_path / "w"
        work.mkdir()
        for cmd in ("true", "false"):
            asyncio.run(run_loop("task", Settings(permission=PermissionMode.AUTO,
                                                  memory_enabled=False, state_dir=state),
                                 cwd=work, backend_factory=Backend, test_command=cmd,
                                 reviewer=Reviewer(), max_rounds=2))
        return state

    def test_one_loop_is_one_attempt_with_the_loops_true_outcome(self, tmp_path):
        state = self._real_loops(tmp_path)
        c = Corpus()
        read_pxx_runs(state, c)
        assert not c.quarantined, c.quarantined
        assert sorted(a.code for a in c.attempts) == ["COMPLETED", "ROUND_CAP"]
        assert all("-loop-" in a.run_id for a in c.attempts)
        failed = next(a for a in c.attempts if a.code == "ROUND_CAP")
        assert failed.model == "ollama/qwen2.5-coder:7b" or "/" in failed.model
        # pxx semantics, learned from this test: the tests gate's `allowed` means
        # "no NEW failures beyond baseline", not "tests passed". A suite that never
        # passes is not a regression — the loop's terminal code is what says failure.
        assert failed.gate_failed("tests") is False and not failed.success

    def test_dx_run_link_points_at_the_loop_and_links(self, tmp_path):
        state = self._real_loops(tmp_path)
        # Both loops start within the same second, so run ids do not sort by
        # creation; pick the failed loop by what it recorded.
        failed_loop = next(d.name for d in (state / "runs").iterdir() if "-loop-" in d.name
                           and json.loads((d / "outcome.json").read_text())["code"] != "COMPLETED")
        c = Corpus()
        read_pxx_runs(state, c)
        repo = write_ledger(tmp_path / "ledger", [
            row("T-1", "EXECUTED", ago(1), f"mode=DX_RUN scope=svc run={failed_loop} model=m")])
        read_ledger(repo, c)
        assert link_attempts(c) == 1
        assert next(a for a in c.attempts if a.dx_task_id == "T-1").code == "ROUND_CAP"

    def test_failed_loop_is_not_counted_as_its_completed_rounds(self, pxx):
        pxx.run(when=ago(1), code="ROUND_CAP", summary="cap", round_code="COMPLETED")
        c = _corpus(pxx)
        assert [a.code for a in c.attempts] == ["ROUND_CAP"]


class TestConcurrentDecisions:
    """Finding 1: a learn pass running while a human decides must not undo the decision."""

    def test_pass_does_not_overwrite_a_decision_made_meanwhile(self, tmp_path, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        store, cfg = JsonStore(tmp_path / "s"), LearnConfig(mc_draws=2000)
        store.save_recs(_learn(_corpus(pxx)), NOW, {})
        # the pass reads...
        existing = store.load_recs()
        expected = {k: r.status for k, r in existing.items()}
        changed = reconcile(_learn(_corpus(pxx)), existing, cfg, NOW)
        # ...a human accepts meanwhile...
        key = changed[0].rec_key
        rec = store.load_recs()[key]
        rec.status, rec.decided_by, rec.decided_at = "ACCEPTED", "Chris", NOW
        store.decide(rec, None, actor="Chris", action="accept", detail={}, now=NOW,
                     expected_status="PROPOSED")
        # ...and the pass's write-back skips it
        assert key in store.save_recs(changed, NOW, expected)
        assert store.load_recs()[key].status == "ACCEPTED"

    def test_second_decision_on_the_same_read_is_refused(self, tmp_path, pxx):
        from dx.learn_store import StaleDecision
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        store = JsonStore(tmp_path / "s")
        rec = _learn(_corpus(pxx))[0]
        store.save_recs([rec], NOW, {})
        rec.status = "ACCEPTED"
        store.decide(rec, None, actor="Chris", action="accept", detail={}, now=NOW,
                     expected_status="PROPOSED")
        rec.status = "REJECTED"
        with pytest.raises(StaleDecision):
            store.decide(rec, None, actor="Pat", action="reject", detail={}, now=NOW,
                         expected_status="PROPOSED")
        assert store.verify_audit() == 1


class TestWhoDecides:
    """Finding 3."""

    @pytest.mark.parametrize("who", ["dx", "dx.run", "dx <dx@devswarm.local>", "pxx",
                                     "Claude Code", "codex", "claude-opus", "claude-opus-5-5",
                                     "", "  ", "AI agent", "kimi"])
    def test_tool_and_model_names_are_refused(self, who):
        with pytest.raises(SystemExit):
            cmd_learn._human(who)

    @pytest.mark.parametrize("who", ["Chris", "Chris Wetzel", "Kimi Tanaka", "fd@thepslawfirm.com"])
    def test_people_are_accepted(self, who):
        assert cmd_learn._human(who) == who.strip()

    def test_os_account_is_recorded_beside_the_name(self, tmp_path, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        store = JsonStore(tmp_path / "s")
        rec = _learn(_corpus(pxx))[0]
        store.save_recs([rec], NOW, {})
        cmd_learn._decide(store, rec, None, by="Chris", action="reject", detail={}, now=NOW,
                          expected="PROPOSED")
        entry = json.loads((tmp_path / "s" / "audit.jsonl").read_text())
        assert entry["detail"]["by"] == "Chris" and entry["detail"]["os_user"]


class TestEfficacyIsHonest:
    """Findings 4, 5, 6."""

    def _merged(self, tmp_path, c: Corpus, when: datetime) -> None:
        read_ledger(write_ledger(tmp_path / "ledger", [row("T-FIX", "MERGED", when)]), c)

    def test_unread_source_is_never_measured_elsewhere(self, tmp_path, pxx):
        pxx.run(when=ago(1))
        c = _corpus(pxx)
        self._merged(tmp_path, c, ago(5))
        ad = Adoption("k" * 24, {"name": "signature_rate", "key": "s", "source": "operator"},
                      "Chris", ago(10), task_id="T-FIX")
        assert Learner(LearnConfig(), NOW).efficacy(c, [ad])[0]["status"] == "source-not-read"

    def test_zero_versus_zero_is_not_a_verdict(self, tmp_path, pxx):
        for i in range(60):
            pxx.run(when=ago(31 + i * 0.4))
        for i in range(10):
            pxx.run(when=ago(1 + i))
        c = _corpus(pxx)
        self._merged(tmp_path, c, ago(30))
        ad = Adoption("k" * 24, {"name": "class_failure_rate", "key": "svc-a", "source": "pxx"},
                      "Chris", ago(40), task_id="T-FIX")
        learner = Learner(LearnConfig(mc_draws=4000), NOW)
        assert learner.efficacy(c, [ad])[0]["status"] == "no-baseline-problem"
        assert not [r for r in learner.learn(c, [ad]) if r.kind == "adoption_ineffective"]

    def test_tiny_baseline_is_still_measuring(self, tmp_path, pxx):
        for i in range(3):
            pxx.run(when=ago(31 + i), code="ROUND_CAP")
        for i in range(40):
            pxx.run(when=ago(1 + i * 0.5))
        c = _corpus(pxx)
        self._merged(tmp_path, c, ago(30))
        ad = Adoption("k" * 24, {"name": "class_failure_rate", "key": "svc-a", "source": "pxx"},
                      "Chris", ago(40), task_id="T-FIX")
        assert Learner(LearnConfig(), NOW).efficacy(c, [ad])[0]["status"].startswith("measuring")

    def test_merge_before_adoption_does_not_count(self, tmp_path, pxx):
        pxx.run(when=ago(1))
        c = _corpus(pxx)
        self._merged(tmp_path, c, ago(40))
        ad = Adoption("k" * 24, {"name": "class_failure_rate", "key": "svc-a", "source": "pxx"},
                      "Chris", ago(1), task_id="T-FIX")
        assert Learner(LearnConfig(), NOW).efficacy(c, [ad])[0]["status"] == "merged-before-adoption"


class TestNoDraftLoosensAControl:
    """Finding 7: the closed kind list is not enough; the words matter too."""

    LOOSENING = ("drop it from", "remove it from", "disable", "lower the bar", "raise the budget",
                 "increase the budget", "turn off", "skip the", "auto-approve it")

    def test_every_kind_draft(self, tmp_path, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999",
                    gates=[{"gate": "tests", "allowed": False, "new_failures": ["tests/t.py::x"]}])
        for i in range(40):
            pxx.run(when=ago(20 + i), gates=[{"gate": "lint", "allowed": i % 20 != 0}])
        for i in range(20):
            pxx.run(when=ago(1 + i * 0.5), gates=[{"gate": "lint", "allowed": i % 2 == 0}])
        for i in range(20):
            pxx.run(when=ago(i % 10 + 1), project="p2", model="qwen",
                    code="COMPLETED" if i % 10 < 3 else "NO_TEST_PROGRESS")
            pxx.run(when=ago(i % 10 + 1), project="p2", model="glm")
        c = _corpus(pxx)
        d = tmp_path / "review" / "v1"
        d.mkdir(parents=True)
        (d / "manifest.tsv").write_text("id\tlane\tresult\treceipt\n" + "".join(
            f"k{i}\tkimi\tNO_REVIEW\tr\n" for i in range(12)))
        read_review_manifests(tmp_path / "review", c)
        audit = write_audit(tmp_path / "a.jsonl",
                            [("denied", "k", "no")] * 4 + [("rejected-stale", "k", "old")] * 4)
        read_operator_audit(audit, c)
        recs = _learn(c)
        kinds = {r.kind for r in recs}
        assert {"recurring_failure", "variant_preference", "gate_regression", "reviewer_lane",
                "operator_failure"} <= kinds
        for r in recs:
            text = r.draft_text.lower()
            for phrase in self.LOOSENING:
                assert phrase not in text, (r.kind, phrase)
            for m in re.finditer(r"widen", text):
                assert re.search(r"(never|not|do not) widen", text[max(0, m.start() - 12):m.end()])


class TestLedgerAttribution:
    """Findings 8, 9, 11."""

    def test_correction_sets_aside_the_task_of_the_row_it_names(self, tmp_path):
        repo = write_ledger(tmp_path / "ledger", [row("T-1", "MERGED", ago(5)),
                                                  row("T-2", "MERGED", ago(4))])
        lines = (repo / "ledger.jsonl").read_text().splitlines()
        t1_hash = hashlib.sha256(lines[1].encode()).hexdigest()
        rows = [row("T-1", "MERGED", ago(5)), row("T-2", "MERGED", ago(4)),
                {**row("", "CORRECTION", ago(3), f"row {t1_hash} recorded the wrong task"),
                 "task_id": None}]
        c = Corpus()
        read_ledger(write_ledger(tmp_path / "ledger2", rows), c)
        assert "T-1" not in c.tasks and "T-2" in c.tasks

    def test_unattributable_correction_stops_the_pass(self, tmp_path):
        rows = [row("T-1", "MERGED", ago(5)),
                {**row("", "CORRECTION", ago(3), "that merge was wrong"), "task_id": None}]
        with pytest.raises(LearnSourceError, match="CORRECTION"):
            read_ledger(write_ledger(tmp_path / "ledger", rows), Corpus())

    def test_quoted_subject_cannot_supply_scope_or_run(self, tmp_path):
        ev = 'mode=DX_RUN scope=api commit=abc subject="evil scope=auth run=fake-1" run=real-1'
        c = Corpus()
        read_ledger(write_ledger(tmp_path / "ledger", [row("T-1", "EXECUTED", ago(1), ev)]), c)
        assert c.tasks["T-1"].scope == "api" and c.tasks["T-1"].run_ids == ["real-1"]

    def test_ambiguous_provenance_sets_the_task_aside(self, tmp_path):
        c = Corpus()
        read_ledger(write_ledger(tmp_path / "ledger", [
            row("T-1", "EXECUTED", ago(1), "scope=a run=r-1 run=r-2")]), c)
        assert "T-1" not in c.tasks

    def test_rows_after_the_verified_head_are_not_trusted(self, tmp_path, monkeypatch):
        repo = write_ledger(tmp_path / "ledger", [row("T-1", "MERGED", ago(5))])
        lines = (repo / "ledger.jsonl").read_text().splitlines()
        verified_head = hashlib.sha256(lines[-1].encode()).hexdigest()
        with (repo / "ledger.jsonl").open("a") as fh:   # appended after verification
            fh.write(json.dumps({**row("T-2", "MERGED", ago(1)), "prev_hash": "bogus"}) + "\n")
        monkeypatch.setattr("dx.learn_sources.get_ledger_head", lambda _repo: verified_head)
        c = Corpus()
        read_ledger(repo, c)
        assert "T-2" not in c.tasks and "T-1" in c.tasks

    def test_reference_ledger_is_recognised_under_any_name(self, tmp_path):
        rows = [row("T-1", "ADMITTED", ago(1))]
        repo = write_ledger(tmp_path / "my-copy", rows)
        lines = (repo / "ledger.jsonl").read_text().splitlines()
        genesis = json.loads(lines[0])
        genesis["evidence"] = "Reference ledger genesis. Synthetic: no real work is attested here."
        write_ledger(repo, [])   # rebuild with that genesis
        (repo / "ledger.jsonl").write_text(_canon(genesis) + "\n")
        with pytest.raises(LearnSourceError, match="genesis"):
            read_ledger(repo, Corpus())


class TestMalformedInputsQuarantine:
    """Finding 10."""

    def test_absurd_timestamp_is_quarantined_not_a_crash(self, pxx):
        rid = pxx.run(when=ago(1))
        p = pxx.root / "runs" / rid / "outcome.json"
        p.write_text(json.dumps({**json.loads(p.read_text()), "ts": 1e20}))
        c = _corpus(pxx)
        assert not c.attempts and c.quarantined

    def test_new_failures_must_be_a_list(self, pxx):
        pxx.run(when=ago(1), gates=[{"gate": "tests", "allowed": False, "new_failures": "abc"}])
        c = _corpus(pxx)
        assert not c.attempts and not c.signatures and c.quarantined

    def test_a_loop_with_a_torn_task_file_still_claims_its_rounds(self, pxx):
        rid = pxx.run(when=ago(1), code="ROUND_CAP", round_code="COMPLETED")
        (pxx.root / "runs" / rid / "task.json").write_text("{torn")
        c = _corpus(pxx)
        assert c.attempts == [] and len(c.quarantined) == 1

    def test_a_quarantined_loop_still_claims_its_rounds(self, pxx):
        """Otherwise its COMPLETED round would be counted as a success."""
        rid = pxx.run(when=ago(1), code="ROUND_CAP", round_code="COMPLETED")
        (pxx.root / "runs" / rid / "outcome.json").write_text("{torn")
        c = _corpus(pxx)
        assert c.attempts == [] and len(c.quarantined) == 1


class TestAuditAndPxxRaces:
    """Finding 12."""

    def test_tampered_audit_blocks_further_decisions(self, tmp_path, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        store = JsonStore(tmp_path / "s")
        recs = _learn(_corpus(pxx))
        store.save_recs(recs, NOW, {})
        store.decide(recs[0], None, actor="Chris", action="reject", detail={}, now=NOW,
                     expected_status="PROPOSED")
        audit = tmp_path / "s" / "audit.jsonl"
        audit.write_text(audit.read_text().replace("Chris", "Mallory"))
        with pytest.raises(StoreError):
            store.decide(recs[0], None, actor="Chris", action="note", detail={}, now=NOW,
                         expected_status="PROPOSED")

    def test_mssql_audit_chain_detects_an_edit(self, pxx):
        for i, t in enumerate(["a", "b", "c", "d"]):
            pxx.run(when=ago(i + 1), task=t, code="LINT_BLOCKED", summary="E999")
        rec = _learn(_corpus(pxx))[0]
        conn = _FakeConn()
        conn.status = "PROPOSED"
        store = MssqlStore("DSN=x", connect=lambda *a, **k: conn)
        store.decide(rec, None, actor="Chris", action="reject", detail={}, now=NOW,
                     expected_status="PROPOSED")
        aid, body, prev, digest = conn.audit[0]
        conn.audit[0] = (aid, body.replace("Chris", "Mallory"), prev, digest)
        with pytest.raises(StoreError, match="content hash"):
            store.verify_audit()

    def test_forward_withdraws_if_a_human_decided_meanwhile(self, pxx, monkeypatch):
        import pxx.improve.cycle as cycle
        for i in range(20):
            pxx.run(when=ago(i % 10 + 1), model="qwen",
                    code="COMPLETED" if i % 10 < 3 else "NO_TEST_PROGRESS")
            pxx.run(when=ago(i % 10 + 1), model="glm")
        rec = next(r for r in _learn(_corpus(pxx)) if r.kind == "variant_preference")
        answers = iter([None, "rejected"])        # undecided at the check, decided after the write
        monkeypatch.setattr(cycle, "human_disposition", lambda *_a: next(answers))
        slug, outcome = forward_to_pxx(rec, pxx.root)
        assert outcome.startswith("withdrawn")
        assert not (pxx.root / "inbox" / "human-review-required" / f"{slug}.json").exists()
