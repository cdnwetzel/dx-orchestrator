"""Finding R4 (efficacy claims).

A before/after rate split at a merge shows an OBSERVED change, not that the
change caused it. Equal or worse raw rates on weak evidence must stay
inconclusive; the verdict that proposes revising or reverting
("no-meaningful-improvement") needs larger samples and strong evidence that any
improvement is below a minimum meaningful effect. A shift in the measured
cohort, or another adoption on the same metric, voids every verdict.
"""
from __future__ import annotations

import json

import pytest

from dx.learn_engine import LearnConfig, Learner, observations
from dx.learn_model import Adoption, Corpus
from dx.learn_sources import read_ledger
from learn_helpers import NOW, _corpus, _dx, ago, row, write_ledger

CLASS = {"name": "class_failure_rate", "key": "svc-a", "source": "pxx"}


def _world(tmp_path, pxx, before: list[bool], after: list[bool], *,
           model_before: str = "glm", model_after: str = "glm",
           merges: tuple[str, ...] = ("T-FIX",)) -> Corpus:
    """Attempts in svc-a before and after a merge 30 days ago; True = failed."""
    for i, bad in enumerate(before):
        pxx.run(when=ago(31 + i * 25 / max(len(before), 1)), model=model_before,
                code="ROUND_CAP" if bad else "COMPLETED")
    for i, bad in enumerate(after):
        pxx.run(when=ago(29 - i * 25 / max(len(after), 1)), model=model_after,
                code="ROUND_CAP" if bad else "COMPLETED")
    c = _corpus(pxx)
    read_ledger(write_ledger(tmp_path / "ledger", [row(t, "MERGED", ago(30)) for t in merges]), c)
    return c


def _adopt(task: str = "T-FIX", key: str = "k" * 24, metric: dict | None = None) -> Adoption:
    return Adoption(key, dict(metric or CLASS), "Chris", ago(40), task_id=task)


def _status(c: Corpus, *ads: Adoption) -> list[str]:
    return [r["status"] for r in Learner(LearnConfig(mc_draws=4000), NOW).efficacy(c, list(ads))]


class TestVerdicts:
    def test_clear_fall_is_an_observed_improvement(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 20, [False] * 20)
        assert _status(c, _adopt()) == ["improvement-observed"]

    def test_equal_rates_on_weak_evidence_stay_inconclusive(self, tmp_path, pxx):
        """The first cut called this 'ineffective' and proposed a revert."""
        c = _world(tmp_path, pxx, [True] * 6 + [False] * 4, [True] * 6 + [False] * 4)
        assert _status(c, _adopt()) == ["inconclusive"]

    def test_worse_rates_on_weak_evidence_stay_inconclusive(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 5 + [False] * 7, [True] * 7 + [False] * 5)
        learner = Learner(LearnConfig(mc_draws=4000), NOW)
        assert [r["status"] for r in learner.efficacy(c, [_adopt()])] == ["inconclusive"]
        assert not [r for r in learner.learn(c, [_adopt()]) if r.kind == "adoption_ineffective"]

    def test_no_meaningful_improvement_needs_sufficient_evidence(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 40, [True] * 40)
        learner = Learner(LearnConfig(mc_draws=4000), NOW)
        assert [r["status"] for r in learner.efficacy(c, [_adopt()])] == \
            ["no-meaningful-improvement"]
        [rec] = [r for r in learner.learn(c, [_adopt()]) if r.kind == "adoption_ineffective"]
        assert "not proof" in rec.draft_text and "observed" in rec.subject
        assert rec.evidence["runs_before"] == rec.evidence["runs_after"] == 40
        assert any("not a causal estimate" in cv for cv in rec.evidence["caveats"])

    def test_same_rates_below_the_verdict_sample_size_stay_inconclusive(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 25, [True] * 25)
        assert _status(c, _adopt()) == ["inconclusive"]

    def test_zero_baseline_is_not_a_verdict(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [False] * 60, [False] * 10)
        learner = Learner(LearnConfig(mc_draws=4000), NOW)
        assert [r["status"] for r in learner.efficacy(c, [_adopt()])] == ["no-baseline-problem"]
        assert not [r for r in learner.learn(c, [_adopt()]) if r.kind == "adoption_ineffective"]


class TestConfounding:
    def test_model_mix_shift_voids_an_apparent_improvement(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 20, [False] * 20,
                   model_before="qwen", model_after="glm")
        [row_] = Learner(LearnConfig(mc_draws=4000), NOW).efficacy(c, [_adopt()])
        assert row_["status"] == "inconclusive (confounded)"
        assert any("cohort shifted" in cv for cv in row_["caveats"])

    def test_model_mix_shift_voids_a_revert_too(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 40, [True] * 40,
                   model_before="qwen", model_after="glm")
        assert _status(c, _adopt()) == ["inconclusive (confounded)"]

    def test_overlapping_adoptions_on_one_metric_cannot_be_told_apart(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 20, [False] * 20, merges=("T-FIX", "T-OTHER"))
        statuses = _status(c, _adopt("T-FIX", "a" * 24), _adopt("T-OTHER", "b" * 24))
        assert statuses == ["inconclusive (confounded)"] * 2

    def test_every_measured_row_says_it_is_not_causal(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 12, [False] * 12)
        [row_] = Learner(LearnConfig(mc_draws=4000), NOW).efficacy(c, [_adopt()])
        assert "not a causal estimate" in row_["caveats"][0]
        assert "regression to the mean" in row_["caveats"][0]


class TestPreconditions:
    def test_unread_source_is_never_measured_elsewhere(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 12, [False] * 12)
        ad = _adopt(metric={"name": "signature_rate", "key": "s", "source": "operator"})
        assert _status(c, ad) == ["source-not-read"]

    def test_small_baseline_is_still_measuring(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 3, [False] * 40)
        assert _status(c, _adopt())[0].startswith("measuring")

    def test_merge_before_adoption_does_not_count(self, tmp_path, pxx):
        c = _world(tmp_path, pxx, [True] * 12, [False] * 12)
        ad = Adoption("k" * 24, dict(CLASS), "Chris", ago(1), task_id="T-FIX")
        assert _status(c, ad) == ["merged-before-adoption"]

    def test_not_measured_before_the_change_ships(self, pxx):
        pxx.run(when=ago(1), code="LINT_BLOCKED", summary="x")
        c = _corpus(pxx)
        ad = Adoption("k" * 24, dict(CLASS), "Chris", ago(5))
        assert _status(c, ad) == ["awaiting-task"]
        ad.task_id = "T-NOT-MERGED"
        assert _status(c, ad) == ["awaiting-merge"]

    def test_metric_definitions_are_shared(self, pxx):
        pxx.run(when=ago(1), code="LINT_BLOCKED", summary="x",
                gates=[{"gate": "lint", "allowed": False}])
        c = _corpus(pxx)
        assert observations({"name": "gate_fail_rate", "key": "lint", "source": "pxx"},
                            c)[0][1] is True
        with pytest.raises(ValueError):
            observations({"name": "made-up", "key": "x", "source": "pxx"}, c)


def test_cli_output_says_correlational(tmp_path, pxx, monkeypatch, capsys):
    from dx.learn_store import JsonStore, Tx
    c = _world(tmp_path, pxx, [True] * 20, [False] * 20)
    assert c.attempts
    store = JsonStore(tmp_path / "store")
    store.commit(Tx("Chris", "accept", "k" * 24, NOW, writes=[_adopt()]))
    monkeypatch.setenv("DX_LEARN_STORE", str(store.root))
    monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "ledger"))
    _dx(monkeypatch, "learn", "efficacy", "--now", NOW.isoformat())
    out = capsys.readouterr().out
    assert "Correlational" in out and "improvement-observed" in out
    _dx(monkeypatch, "learn", "efficacy", "--json", "--now", NOW.isoformat())
    [payload] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["caveats"] and payload["observed_change"] < 0


def test_moderate_evidence_of_a_small_fall_is_not_a_revert(tmp_path, pxx):
    """n >= 30 on both sides, a fall too uncertain to call an improvement, and
    too uncertain to rule out a meaningful one: inconclusive, never a revert."""
    c = _world(tmp_path, pxx, [True] * 15 + [False] * 15, [True] * 13 + [False] * 17)
    assert _status(c, _adopt()) == ["inconclusive"]
