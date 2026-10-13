"""Run-intent stratification: three detector views over one complete corpus.

Cross-repo spec RUN-INTENT-SPEC-20261012 (v3). Ingestion stays complete and
unchanged — every readable run enters the corpus and all-runs accounting,
whatever its declared intent. Views only select which attempts the detectors
see. The rules under test:

* ``unknown`` is never excluded, from any view, for any reason.
* The conservative view excludes only clean first-party, non-contradictory
  probe/eval/drill declarations; anything that is not a clean first-party
  record (declared, missing, malformed or conflicting provenance) is treated
  as not-first-party and retained. "first-party" names a code path, not
  cryptographic attestation.
* The exploratory view excludes every positively declared probe/eval/drill run
  regardless of provenance — and its proposals carry no decision weight:
  the forwarding path and task generation both refuse them, loudly.
* Conflicting intent records read as ``unknown`` and are surfaced by run id
  (a notice, never a quarantine); the convenient value never wins.
* The all-runs rec_key derivation is byte-for-byte the pre-views one, so the
  three deferred pilot proposals keep their keys; a proposal stales only in
  its own view; efficacy across an exclusion-policy seam is voided.
"""
from __future__ import annotations

import json

import pytest

from dx.learn_engine import LearnConfig, Learner, _key, observations, reconcile
from dx.learn_forward import EXPLORATORY_REFUSAL, forward_proposals
from dx.learn_model import (
    Adoption,
    Corpus,
    Recommendation,
    adoption_from_json,
    rec_from_json,
)
from dx.learn_signature import fingerprint
from dx.learn_sources import link_attempts, read_ledger
from dx.learn_store import JsonStore, MssqlStore, Tx
from dx.learn_views import (
    ALL_RUNS,
    CONSERVATIVE,
    EXPLORATORY,
    VIEWS,
    PolicySeam,
    mixed_provenance,
    select,
)
from learn_helpers import (
    NOW,
    FakeSqlServer,
    _corpus,
    _dx,
    ago,
    lint_world,
    row,
    write_ledger,
)

CFG = LearnConfig(mc_draws=4000)


def _learn_view(corpus: Corpus, view: str, adoptions: list[Adoption] | None = None):
    return Learner(CFG, NOW, view=view).learn(corpus, adoptions or [])


def _view_membership(corpus: Corpus) -> dict[str, set[str]]:
    return {view: {a.run_id for a in select(corpus, view)[0]} for view in VIEWS}


# --------------------------------------------------------------------------- baseline 1
class TestLegacyIngestion:
    def test_875_unmarked_runs_ingest_as_unknown_and_stay_in_every_view(self, pxx):
        """The frozen-corpus shape: no intent metadata anywhere. Ingestion
        reproduces exactly, and every view's membership equals all-runs."""
        for i in range(875):
            pxx.run(when=ago(i % 60 + 0.5), task=f"t-{i}",
                    code="COMPLETED" if i % 3 else "LINT_BLOCKED")
        c = _corpus(pxx)
        assert len(c.attempts) == 875
        assert all(a.run_intent == "unknown" for a in c.attempts)
        assert all(a.run_intent_provenance is None for a in c.attempts)
        assert c.notices == []
        memberships = _view_membership(c)
        for view in VIEWS:
            assert memberships[view] == memberships[ALL_RUNS]
            _, report = select(c, view)
            assert (report.included, report.excluded) == (875, 0)

    def test_stored_records_without_intent_fields_load(self, tmp_path):
        """JSON-store records written before views (no view/policy keys) load
        with the all-runs defaults — at read time, without a rewrite."""
        store = JsonStore(tmp_path / "s")
        rec = {"rec_key": "a" * 24, "kind": "recurring_failure", "subject": "s",
               "score": 1.0, "evidence_weight": 3.0, "evidence": {}, "draft_text": "d",
               "metric": {"name": "signature_rate", "key": "x", "source": "pxx"},
               "status": "PROPOSED", "revision": 1}
        ad = {"rec_key": "a" * 24, "metric": {"name": "signature_rate", "key": "x",
              "source": "pxx"}, "accepted_by": "Chris",
              "accepted_at": ago(40).isoformat(), "active": True, "revision": 1}
        (store.root / "recommendations.json").write_text(json.dumps({"a" * 24: rec}))
        (store.root / "adoptions.json").write_text(json.dumps({"a" * 24: ad}))
        before_rec = (store.root / "recommendations.json").read_bytes()
        before_ad = (store.root / "adoptions.json").read_bytes()
        loaded_rec = store.load_recs()["a" * 24]
        loaded_ad = store.load_adoptions()["a" * 24]
        assert loaded_rec.view == ALL_RUNS
        assert (loaded_ad.view, loaded_ad.policy_version) == (ALL_RUNS, "v1")
        assert (store.root / "recommendations.json").read_bytes() == before_rec
        assert (store.root / "adoptions.json").read_bytes() == before_ad


# --------------------------------------------------------------------------- baseline 2-5
class TestViewExclusions:
    def test_first_party_probe_eval_drill_excluded_in_both_new_views(self, pxx):
        fp = [pxx.run(when=ago(i + 1), intent=intent, provenance="first-party")
              for i, intent in enumerate(["probe", "eval", "drill"])]
        keep = pxx.run(when=ago(4))                       # unknown
        keep_op = pxx.run(when=ago(5), project="svc-op", intent="operational",
                          provenance="first-party")
        c = _corpus(pxx)
        members = _view_membership(c)
        assert members[ALL_RUNS] == {*fp, keep, keep_op}
        for view in (CONSERVATIVE, EXPLORATORY):
            assert members[view] == {keep, keep_op}
            _, report = select(c, view)
            assert (report.included, report.excluded) == (2, 3)
            assert report.by_intent["probe"]["excluded"] == 1
            assert report.by_provenance["first-party"]["excluded"] == 3

    def test_declared_probe_eval_drill_excluded_only_in_the_exploratory_view(self, pxx):
        decl = [pxx.run(when=ago(i + 1), intent=intent, provenance="declared")
                for i, intent in enumerate(["probe", "eval", "drill"])]
        c = _corpus(pxx)
        members = _view_membership(c)
        for view in (ALL_RUNS, CONSERVATIVE):
            assert members[view] == set(decl)          # unverified labels never suppress
        assert members[EXPLORATORY] == set()
        _, report = select(c, CONSERVATIVE)
        assert report.by_provenance["declared"]["included"] == 3

    def test_operational_is_never_excluded(self, pxx):
        op = [pxx.run(when=ago(i + 1), intent="operational", provenance=prov)
              for i, prov in enumerate(["first-party", "declared"])]
        c = _corpus(pxx)
        for view in VIEWS:
            assert {a.run_id for a in select(c, view)[0]} == set(op)

    def test_excluded_but_failed_is_listed_by_run_id_per_view(self, pxx):
        failed_fp = pxx.run(when=ago(1), intent="probe", provenance="first-party",
                            code="MODEL_UNAVAILABLE")
        failed_decl = pxx.run(when=ago(2), intent="eval", provenance="declared",
                              code="EDIT_TIMEOUT")
        ok_fp = pxx.run(when=ago(3), intent="drill", provenance="first-party")
        c = _corpus(pxx)
        _, cons = select(c, CONSERVATIVE)
        _, expl = select(c, EXPLORATORY)
        _, allr = select(c, ALL_RUNS)
        assert cons.excluded_but_failed == ((failed_fp, "MODEL_UNAVAILABLE"),)
        assert expl.excluded_but_failed == ((failed_fp, "MODEL_UNAVAILABLE"),
                                            (failed_decl, "EDIT_TIMEOUT"))
        assert allr.excluded_but_failed == ()
        assert ok_fp not in dict(cons.excluded_but_failed)

    def test_unrecognized_intent_reads_as_unknown_and_cannot_be_excluded(self, pxx):
        rid = pxx.run(when=ago(1), intent="experiment", provenance="first-party",
                      code="LINT_BLOCKED")
        c = _corpus(pxx)
        [a] = [a for a in c.attempts if a.run_id == rid]
        assert a.run_intent == "unknown"
        assert any(n.kind == "intent-unrecognized" and n.run_id == rid and "experiment"
                   in n.detail for n in c.notices)
        for view in VIEWS:
            assert rid in {x.run_id for x in select(c, view)[0]}


# --------------------------------------------------------------------------- baseline 6-8
class TestExclusionChangesNothingElse:
    def test_malformed_evidence_quarantines_identically_across_intents(self, pxx):
        bad_gate = [{"gate": "scope", "reason": "no verdict here"}]
        pxx.run(when=ago(1), gates=bad_gate)
        pxx.run(when=ago(2), gates=bad_gate, intent="probe", provenance="first-party")
        c = _corpus(pxx)
        assert c.attempts == []                      # both quarantined, intent irrelevant
        assert len(c.quarantined) == 2
        assert all("without a verdict" in q.error for q in c.quarantined)

    def test_exclusion_never_rewrites_the_record(self, pxx):
        rid = pxx.run(when=ago(1), task="t-1", intent="probe", provenance="first-party",
                      code="LINT_BLOCKED", summary="E999 broken")
        c = _corpus(pxx)
        before = {a.run_id: (a.code, a.signatures) for a in c.attempts}
        sigs_before = set(c.signatures)
        for view in VIEWS:
            select(c, view)
        assert {a.run_id: (a.code, a.signatures) for a in c.attempts} == before
        assert set(c.signatures) == sigs_before
        assert "pxx" in c.sources                   # efficacy-source presence unchanged
        [a] = [a for a in c.attempts if a.run_id == rid]
        assert a.code == "LINT_BLOCKED"             # the recorded terminal code stands
        metric = {"name": "signature_rate", "key": a.signatures[0], "source": "pxx"}
        assert any(bad for _, bad in observations(metric, c))   # still observable
        select(c, EXPLORATORY)                      # excluded there, observed here
        assert any(bad for _, bad in observations(metric, c))

    def test_pass_output_names_every_view_and_labels_the_exploratory(
            self, tmp_path, pxx, monkeypatch, capsys):
        failed = pxx.run(when=ago(1), intent="probe", provenance="first-party",
                         code="MODEL_UNAVAILABLE")
        pxx.run(when=ago(2), intent="eval", provenance="declared")
        pxx.run(when=ago(3))
        monkeypatch.setenv("DX_LEARN_PXX_STATE", str(pxx.root))
        monkeypatch.setenv("DX_LEDGER_REPO", str(write_ledger(tmp_path / "ledger", [])))
        _dx(monkeypatch, "learn", "run", "--json", "--now", NOW.isoformat())
        summary = json.loads(capsys.readouterr().out)
        assert set(summary["views"]) == set(VIEWS)
        assert summary["views"][ALL_RUNS]["included"] == 3
        assert summary["views"][CONSERVATIVE]["included"] == 2
        assert summary["views"][EXPLORATORY]["included"] == 1
        assert "EXPLORATORY" in summary["views"][EXPLORATORY]["label"]
        listed = summary["views"][CONSERVATIVE]["excluded_but_failed"]
        assert listed == [{"run_id": failed, "code": "MODEL_UNAVAILABLE"}]

        _dx(monkeypatch, "learn", "run", "--now", NOW.isoformat())
        text = capsys.readouterr().out
        for view in VIEWS:
            assert f"view {view} " in text            # every printed metric names its view
        assert "EXPLORATORY" in text
        assert f"excluded-but-failed: {failed} (MODEL_UNAVAILABLE)" in text


# --------------------------------------------------------------------------- A1/A2: provenance is not caller-supplied; conflicts read unknown
class TestProvenanceBoundaries:
    def test_a_non_enum_provenance_is_not_first_party(self, pxx):
        rid = pxx.run(when=ago(1), intent="probe", provenance="verified", loop=False)
        c = _corpus(pxx)
        [a] = [a for a in c.attempts if a.run_id == rid]
        assert (a.run_intent, a.run_intent_provenance) == ("probe", "declared")
        assert any(n.kind == "intent-provenance-malformed" and n.run_id == rid
                   for n in c.notices)
        assert rid in {x.run_id for x in select(c, CONSERVATIVE)[0]}
        assert rid not in {x.run_id for x in select(c, EXPLORATORY)[0]}

    def test_a_first_party_claim_in_one_record_only_is_not_clean(self, pxx):
        """pxx writes one value to both records; a claim in just one cannot be
        cross-checked, so it is treated as declared (conservatively retained)."""
        only_session = pxx.run(when=ago(1), loop=False, session_extra={
            "run_intent": "probe", "run_intent_provenance": "first-party"})
        only_task = pxx.run(when=ago(2), loop=False, task_extra={
            "run_intent": "eval", "run_intent_provenance": "first-party"})
        c = _corpus(pxx)
        by_id = {a.run_id: a for a in c.attempts}
        assert by_id[only_session].run_intent_provenance == "declared"
        assert by_id[only_task].run_intent_provenance == "declared"
        assert {n.run_id for n in c.notices if n.kind == "intent-provenance-demoted"} == \
            {only_session, only_task}
        conservative = {x.run_id for x in select(c, CONSERVATIVE)[0]}
        exploratory = {x.run_id for x in select(c, EXPLORATORY)[0]}
        assert {only_session, only_task} <= conservative
        assert {only_session, only_task}.isdisjoint(exploratory)

    def test_conflicting_intent_records_read_as_unknown_and_are_surfaced(self, pxx):
        rid = pxx.run(when=ago(1), loop=False, intent="probe", provenance="first-party",
                      session_extra={"run_intent": "eval"}, code="LINT_BLOCKED")
        c = _corpus(pxx)
        [a] = [a for a in c.attempts if a.run_id == rid]
        assert a.run_intent == "unknown"             # neither value wins
        assert a.run_intent_provenance is None
        [notice] = [n for n in c.notices if n.kind == "intent-conflict"]
        assert notice.run_id == rid and "probe" in notice.detail and "eval" in notice.detail
        for view in VIEWS:                           # unknown is never excluded
            assert rid in {x.run_id for x in select(c, view)[0]}
        assert not select(c, EXPLORATORY)[1].excluded

    def test_conflicting_provenance_records_read_as_unknown(self, pxx):
        rid = pxx.run(when=ago(1), loop=False, intent="probe", provenance="first-party",
                      task_extra={"run_intent_provenance": "declared"})
        c = _corpus(pxx)
        [a] = [a for a in c.attempts if a.run_id == rid]
        assert a.run_intent == "unknown"
        assert any(n.kind == "intent-conflict" and n.run_id == rid for n in c.notices)

    def test_a_loop_with_a_diverging_task_record_conflicts(self, pxx):
        rid = pxx.run(when=ago(1), intent="probe", provenance="first-party",
                      task_extra={"run_intent": "drill"})
        c = _corpus(pxx)
        [a] = [a for a in c.attempts if a.run_id == rid]
        assert a.run_intent == "unknown"
        assert any(n.kind == "intent-conflict" and n.run_id == rid for n in c.notices)

    def test_missing_provenance_is_not_first_party(self, pxx):
        rid = pxx.run(when=ago(1), intent="probe")   # intent declared, provenance absent
        c = _corpus(pxx)
        [a] = [a for a in c.attempts if a.run_id == rid]
        assert (a.run_intent, a.run_intent_provenance) == ("probe", "declared")
        assert rid in {x.run_id for x in select(c, CONSERVATIVE)[0]}
        assert rid not in {x.run_id for x in select(c, EXPLORATORY)[0]}


# --------------------------------------------------------------------------- A3: mislabel protection
class TestMixedProvenance:
    def test_a_probe_label_contradicted_by_operational_runs_in_scope(self, pxx):
        probe = pxx.run(when=ago(1), project="svc-x", intent="probe",
                        provenance="first-party", code="MODEL_UNAVAILABLE")
        pxx.run(when=ago(2), project="svc-x", intent="operational", provenance="declared")
        c = _corpus(pxx)
        mixed = mixed_provenance(c)
        assert probe in mixed and "svc-x" in mixed[probe]
        conservative = {x.run_id for x in select(c, CONSERVATIVE)[0]}
        exploratory = {x.run_id for x in select(c, EXPLORATORY)[0]}
        assert probe in conservative                 # retained: labels cannot suppress
        assert probe not in exploratory              # excluded only there
        for view in VIEWS:                           # warned by run id in EVERY view
            _, report = select(c, view)
            assert report.mixed_provenance[probe] == mixed[probe]
        _, expl = select(c, EXPLORATORY)
        assert expl.excluded_but_failed == ((probe, "MODEL_UNAVAILABLE"),)

    def test_a_probe_label_contradicted_by_a_ledger_link(self, tmp_path, pxx):
        probe = pxx.run(when=ago(1), task="T-9", intent="drill", provenance="first-party",
                        code="LINT_BLOCKED")
        c = _corpus(pxx)
        read_ledger(write_ledger(tmp_path / "ledger",
                                 [row("T-9", "EXECUTED", ago(1), f"scope=api run={probe}")]), c)
        link_attempts(c)
        mixed = mixed_provenance(c)
        assert probe in mixed and "T-9" in mixed[probe]
        assert probe in {x.run_id for x in select(c, CONSERVATIVE)[0]}
        assert probe not in {x.run_id for x in select(c, EXPLORATORY)[0]}

    def test_an_uncontradicted_declared_probe_raises_no_warning(self, pxx):
        probe = pxx.run(when=ago(1), intent="probe", provenance="declared")
        c = _corpus(pxx)
        assert mixed_provenance(c) == {}
        assert probe in {x.run_id for x in select(c, CONSERVATIVE)[0]}

    def test_unknown_can_never_be_excluded_even_when_contradicted(self, tmp_path, pxx):
        rid = pxx.run(when=ago(1), task="T-9")
        c = _corpus(pxx)
        read_ledger(write_ledger(tmp_path / "ledger",
                                 [row("T-9", "EXECUTED", ago(1), f"scope=api run={rid}")]), c)
        link_attempts(c)
        assert mixed_provenance(c) == {}
        for view in VIEWS:
            assert rid in {x.run_id for x in select(c, view)[0]}


# --------------------------------------------------------------------------- A4: proposal identity
class TestProposalIdentity:
    def test_the_three_deferred_pilot_keys_reproduce_byte_for_byte(self):
        """The legacy all-runs derivation is unchanged: the same inputs derive
        the same keys, so the deferred proposals are neither changed nor
        duplicated. Inputs are the signature ids / variant subject stored in
        the pilot queue's own evidence (metric.key / subject)."""
        # MODEL_UNAVAILABLE "all endpoints unreachable…" (157 dead-endpoint probes)
        sig_unavailable = fingerprint(
            "MODEL_UNAVAILABLE",
            "all endpoints unreachable (last: http://127.0.0.1:9): All connection attempts failed")
        assert sig_unavailable.signature_id == "eb36e3c174b7ce16"
        assert _key("recurring", "eb36e3c174b7ce16") == "20ea71e03997a0eb34af30f4"
        # EDIT_TIMEOUT "wall-clock budget exceeded while editing" (mktemp-class projects)
        sig_timeout = fingerprint("EDIT_TIMEOUT", "wall-clock budget exceeded while editing")
        assert sig_timeout.signature_id == "e9a74ac821aefbc8"
        assert _key("recurring", "e9a74ac821aefbc8") == "b45b96b972a202a0e198b7ea"
        # acp-throwaway variant preference for ollama/qwen2.5:14b-instruct-q4_k_m
        assert _key("variant", "acp-throwaway", "ollama/qwen2.5:14b-instruct-q4_k_m") == \
            "326c090c76f841f3f2233655"

    def test_all_runs_derivation_is_the_default_and_untouched(self, pxx):
        lint_world(pxx)
        c = _corpus(pxx)
        [legacy] = _learn_view(c, ALL_RUNS)
        [default] = Learner(CFG, NOW).learn(c, [])
        assert legacy.rec_key == default.rec_key     # default view is all-runs
        assert legacy.rec_key == _key("recurring", legacy.metric["key"])

    def test_new_views_bind_the_view_name_into_their_keys(self, pxx):
        lint_world(pxx)
        c = _corpus(pxx)
        keys = {view: _learn_view(c, view)[0].rec_key for view in VIEWS}
        assert len(set(keys.values())) == 3
        assert keys[ALL_RUNS] == _key("recurring", _learn_view(c, ALL_RUNS)[0].metric["key"])
        for view in (CONSERVATIVE, EXPLORATORY):
            rec = _learn_view(c, view)[0]
            assert rec.view == view
            assert rec.rec_key == _key("recurring", rec.metric["key"], f"view:{view}")

    def test_a_proposal_stales_only_in_its_own_view(self, pxx):
        lint_world(pxx)
        c = _corpus(pxx)
        recs = {view: _learn_view(c, view)[0] for view in VIEWS}
        for rec in recs.values():
            rec.status = "PROPOSED"
        # all-runs no longer finds the condition: only the all-runs proposal stales
        out = reconcile([], dict(recs), CFG, NOW, view=ALL_RUNS)
        assert [r.rec_key for r in out] == [recs[ALL_RUNS].rec_key]
        assert recs[CONSERVATIVE].status == "PROPOSED"
        # and a legacy record (view=all-runs) is staled by the all-runs pass only
        assert reconcile([], {recs[ALL_RUNS].rec_key: recs[ALL_RUNS]}, CFG, NOW,
                         view=CONSERVATIVE) == []

    def test_no_duplicate_is_minted_for_an_existing_all_runs_proposal(self, pxx):
        lint_world(pxx)
        c = _corpus(pxx)
        first = _learn_view(c, ALL_RUNS)[0]
        existing = {first.rec_key: first}
        again = _learn_view(c, ALL_RUNS)[0]
        assert again.rec_key == first.rec_key
        out = reconcile([again], existing, CFG, NOW, view=ALL_RUNS)
        assert len(out) == 1 and out[0].rec_key == first.rec_key


# --------------------------------------------------------------------------- A5: efficacy across a policy seam
class TestEfficacyPolicySeam:
    def _world(self, tmp_path, pxx) -> Corpus:
        for i in range(20):                          # failing before, clean after
            pxx.run(when=ago(31 + i), code="ROUND_CAP")
            pxx.run(when=ago(29 - i), code="COMPLETED")
        c = _corpus(pxx)
        read_ledger(write_ledger(tmp_path / "ledger", [row("T-FIX", "MERGED", ago(30))]), c)
        return c

    def _adoption(self, **kw) -> Adoption:
        return Adoption("k" * 24, {"name": "class_failure_rate", "key": "svc-a",
                                   "source": "pxx"}, "Chris", ago(40), task_id="T-FIX", **kw)

    def test_a_seam_inside_the_windows_voids_the_comparison(self, tmp_path, pxx):
        c = self._world(tmp_path, pxx)
        seam = PolicySeam(version="v2", at=ago(10),
                          description="new excluded intent value: smoke")
        learner = Learner(CFG, NOW, view=CONSERVATIVE,
                          policy_seams={CONSERVATIVE: [seam]})
        [r] = learner.efficacy(c, [self._adoption(view=CONSERVATIVE, policy_version="v1")])
        assert r["status"] == "inconclusive (policy-changed)"
        assert any("new excluded intent value: smoke" in cv for cv in r["caveats"])
        assert any("v2" in cv for cv in r["caveats"])

    def test_no_seam_no_void(self, tmp_path, pxx):
        c = self._world(tmp_path, pxx)
        learner = Learner(CFG, NOW, view=CONSERVATIVE)
        [r] = learner.efficacy(c, [self._adoption(view=CONSERVATIVE, policy_version="v1")])
        assert r["status"] == "improvement-observed"

    def test_a_seam_outside_the_windows_does_not_void(self, tmp_path, pxx):
        c = self._world(tmp_path, pxx)
        # policy already v2 before the before-window opens; the adoption bound v2
        seam = PolicySeam(version="v2", at=ago(120), description="view-definition revision")
        learner = Learner(CFG, NOW, view=CONSERVATIVE,
                          policy_seams={CONSERVATIVE: [seam]})
        [r] = learner.efficacy(c, [self._adoption(view=CONSERVATIVE, policy_version="v2")])
        assert r["status"] == "improvement-observed"
        # but an adoption bound to the old policy cannot be measured under the new one
        [r2] = learner.efficacy(c, [self._adoption(view=CONSERVATIVE, policy_version="v1")])
        assert r2["status"] == "inconclusive (policy-changed)"

    def test_a_legacy_adoption_defaults_to_the_all_runs_policy(self, tmp_path, pxx):
        c = self._world(tmp_path, pxx)
        stored = {"rec_key": "k" * 24,
                  "metric": {"name": "class_failure_rate", "key": "svc-a", "source": "pxx"},
                  "accepted_by": "Chris", "accepted_at": ago(40).isoformat(),
                  "task_id": "T-FIX", "active": True}
        ad = adoption_from_json(stored)              # no view/policy keys: read-time default
        assert (ad.view, ad.policy_version) == (ALL_RUNS, "v1")
        learner = Learner(CFG, NOW, view=ALL_RUNS)
        [r] = learner.efficacy(c, [ad])
        assert r["status"] == "improvement-observed"


# --------------------------------------------------------------------------- exploratory carries no decision weight (enforced)
def _exploratory_rec(*, status: str = "PROPOSED") -> Recommendation:
    return Recommendation(
        rec_key="e" * 24, kind="variant_preference", subject="svc-a: prefer glm",
        score=9.0, evidence_weight=9.0,
        evidence={"task_class": "svc-a", "best": "local/glm",
                  "dominates": {"local/qwen": {"p_best_better": 0.97}},
                  "example_runs": ["r1", "r2"]},
        draft_text="prefer glm", view=EXPLORATORY, status=status,
        metric={"name": "class_failure_rate", "key": "svc-a", "source": "pxx"},
        decided_by="Chris" if status == "ACCEPTED" else None,
        decided_at=NOW if status == "ACCEPTED" else None)


class TestExploratoryCarriesNoDecisionWeight:
    def test_the_forwarding_path_refuses_exploratory_proposals_loudly(self, tmp_path, pxx):
        store = JsonStore(tmp_path / "s")
        rec = _exploratory_rec()
        store.commit(Tx("test", "seed", rec.rec_key, NOW, writes=[rec]))
        [result] = forward_proposals(store, pxx.root, NOW)
        assert result["state"] == "REFUSED"
        assert result["note"] == EXPLORATORY_REFUSAL
        assert "no decision weight" in result["note"]
        assert store.load_forwards() == {}           # no durable forwarding record
        inbox = pxx.root / "inbox"
        assert not inbox.exists() or not list(inbox.glob("**/*.json"))

    def test_accept_refuses_an_exploratory_proposal(self, tmp_path, monkeypatch, capsys):
        store = JsonStore(tmp_path / "s")
        rec = _exploratory_rec()
        store.commit(Tx("test", "seed", rec.rec_key, NOW, writes=[rec]))
        monkeypatch.setenv("DX_LEARN_STORE", str(store.root))
        with pytest.raises(SystemExit) as exc:
            _dx(monkeypatch, "learn", "accept", rec.rec_key, "--by", "Chris")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "exploratory" in err and "no decision weight" in err
        assert store.load_recs()[rec.rec_key].status == "PROPOSED"   # nothing was recorded
        assert store.load_adoptions() == {}

    def test_spec_generation_refuses_an_exploratory_proposal(self, tmp_path, monkeypatch, capsys):
        store = JsonStore(tmp_path / "s")
        rec = _exploratory_rec(status="ACCEPTED")    # even if one ever landed there
        store.commit(Tx("test", "seed", rec.rec_key, NOW, writes=[rec]))
        monkeypatch.setenv("DX_LEARN_STORE", str(store.root))
        with pytest.raises(SystemExit) as exc:
            _dx(monkeypatch, "learn", "spec", rec.rec_key)
        assert exc.value.code == 1
        assert "exploratory" in capsys.readouterr().err

    def test_forwarding_still_carries_the_all_runs_queue(self, tmp_path, pxx):
        """The forwarding path predates views: an all-runs proposal forwards
        exactly as before. The newer conservative view is not forwarded (pxx
        keys its inbox by slug; two views of one condition would race one
        entry) — forwarding it would be new behavior, a separate decision."""
        store = JsonStore(tmp_path / "s")
        shape = dict(kind="variant_preference", subject="svc-a: prefer glm",
                     score=9.0, evidence_weight=9.0,
                     evidence={"task_class": "svc-a", "best": "local/glm",
                               "dominates": {"local/qwen": {"p_best_better": 0.97}},
                               "example_runs": ["r1", "r2"]},
                     draft_text="prefer glm",
                     metric={"name": "class_failure_rate", "key": "svc-a", "source": "pxx"})
        baseline = Recommendation(rec_key="f" * 24, view=ALL_RUNS, **shape)
        conservative = Recommendation(rec_key="c" * 24, view=CONSERVATIVE, **shape)
        store.commit(Tx("test", "seed", baseline.rec_key, NOW, writes=[baseline]))
        store.commit(Tx("test", "seed", conservative.rec_key, NOW, writes=[conservative]))
        results = forward_proposals(store, pxx.root, NOW)
        assert [(r["rec_key"], r["state"]) for r in results] == [("f" * 24, "WRITTEN")]


# --------------------------------------------------------------------------- store shapes move together
class TestStoreShapes:
    def test_mssql_view_and_policy_round_trip(self):
        db = FakeSqlServer()
        store = MssqlStore("fake", connect=db.connect, acknowledge_unqualified=True)
        rec = Recommendation(
            rec_key="c" * 24, kind="gate_regression", subject="s", score=1.0,
            evidence_weight=2.0, evidence={}, draft_text="d",
            metric={"name": "gate_fail_rate", "key": "scope", "source": "pxx"},
            view=CONSERVATIVE)
        ad = Adoption("c" * 24, {"name": "gate_fail_rate", "key": "scope", "source": "pxx"},
                      "Chris", ago(5), view=CONSERVATIVE, policy_version="v1")
        store.commit(Tx("test", "seed", rec.rec_key, NOW, writes=[rec, ad]))
        assert store.load_recs()["c" * 24].view == CONSERVATIVE
        loaded = store.load_adoptions()["c" * 24]
        assert (loaded.view, loaded.policy_version) == (CONSERVATIVE, "v1")

    def test_mssql_attempt_snapshot_carries_intent(self, pxx):
        db = FakeSqlServer()
        store = MssqlStore("fake", connect=db.connect, acknowledge_unqualified=True)
        pxx.run(when=ago(1), intent="probe", provenance="first-party")
        store.record_pass({}, _corpus(pxx), NOW)
        [stmt] = [line for line in db.log if "dx_learn.Attempt" in line]
        assert "RunIntent" in stmt and "RunIntentProvenance" in stmt

    def test_rec_from_json_defaults_view_for_legacy_records(self):
        rec = rec_from_json({"rec_key": "a" * 24, "kind": "recurring_failure", "subject": "s",
                             "score": 1, "evidence_weight": 1, "evidence": {}, "draft_text": "d",
                             "metric": {}, "status": "PROPOSED"})
        assert rec.view == ALL_RUNS
