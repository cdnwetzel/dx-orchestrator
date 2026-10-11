"""dx learn — hermetic tests: sources, detectors, guardrails, queue, CLI.

Per CONTRIBUTING: a gate without a test is a claim. Store transactions and
crash recovery are in test_learn_store.py, pxx forwarding in
test_learn_forward.py, efficacy claims in test_learn_efficacy.py.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from dx import cmd_learn
from dx.learn_bridge import FORWARDABLE_KINDS, render_spec
from dx.learn_engine import LearnConfig, Learner, reconcile
from dx.learn_model import REC_KINDS, Corpus
from dx.learn_signature import fingerprint, normalize
from dx.learn_sources import (
    LearnSourceError,
    link_attempts,
    read_ledger,
    read_operator_audit,
    read_pxx_runs,
    read_review_manifests,
)
from dx.learn_store import JsonStore
from learn_helpers import (
    NOW,
    _canon,
    _corpus,
    _dx,
    _learn,
    ago,
    lint_world,
    row,
    write_audit,
    write_ledger,
)


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
                             "adoption_no_improvement"}
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

    def test_os_account_is_recorded_beside_the_name(self, tmp_path, pxx, monkeypatch, capsys):
        lint_world(pxx)
        monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
        _dx(monkeypatch, "learn", "run", "--no-ledger", "--json", "--now", NOW.isoformat())
        key = json.loads(capsys.readouterr().out)["proposed"][0]
        _dx(monkeypatch, "learn", "reject", key, "--by", "Chris", "--note", "not now")
        store = Path(cmd_learn.os.environ["DX_LEARN_STORE"])
        entry = json.loads((store / "audit.jsonl").read_text())
        assert entry["detail"]["by"] == "Chris" and entry["detail"]["os_user"]


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


