"""The learning pass: a Corpus in, ranked Recommendations out. Pure and deterministic.

Every quantity is a *bad-event rate* (lower is better) defined once, in
``observations()``. Detectors use it to decide what to raise; efficacy uses the
same definition, split at the adoption's MERGED row, to decide whether an
adopted change worked. One definition for both halves is the point: the
yardstick a recommendation is judged by is fixed when it is proposed
(pre-registered), not chosen after the data is in.

Rates are Beta-Binomial posteriors. Observations decay with a half-life, so old
evidence fades instead of being cut off. Monte Carlo draws use a fixed seed:
the same records produce the same recommendations.

Nothing here changes anything. Recommendations are proposals; a human decides.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .learn_model import (
    DECISION_ACTIONS,
    NEGATIVE_ACTIONS,
    SURFACE_COST,
    Adoption,
    Attempt,
    Corpus,
    Recommendation,
)
from .learn_stats import (
    BetaPost,
    beta_from,
    decay_weight,
    prob_diff_below,
    prob_greater,
    total_variation,
)


class MetricUnavailable(Exception):
    """The metric's source was not read this pass, so it cannot be measured."""


REVIEW_FAILURE_CODES = frozenset({"REVIEW_UNAVAILABLE", "REVIEW_UNPARSEABLE", "REVIEW_EMPTY"})
PXX_REVIEW_LANE = "pxx-review"


@dataclass(frozen=True)
class LearnConfig:
    half_life_days: float = 30.0
    min_eff_n: float = 8.0
    recurrence_min_weight: float = 3.0
    recurrence_min_tasks: int = 2
    variant_confidence: float = 0.95
    regression_recent_days: int = 14
    regression_baseline_days: int = 90
    regression_confidence: float = 0.90
    regression_min_drop: float = 0.10
    rejection_min_weight: float = 2.0
    rejection_min_share: float = 0.25
    # pxx's reviewer-calibration availability bar (pxx/calibration.py), reused so a
    # lane is held to the bar the stack already set rather than one chosen here.
    lane_availability_bar: float = 0.75
    lane_confidence: float = 0.90
    efficacy_window_days: int = 60
    efficacy_min_after: int = 10
    efficacy_min_before: int = 10
    efficacy_confidence: float = 0.90      # P(rate fell) at or above, and raw rate fell -> improvement observed
    # A "no meaningful improvement" verdict is the one that can lead to a revert,
    # so it needs more: larger samples, and strong evidence that any improvement
    # is below a minimum meaningful effect (not merely that none was shown).
    efficacy_min_verdict: int = 30
    efficacy_null_confidence: float = 0.95
    # "Meaningful" is relative to the baseline: a 25% reduction of the before-rate.
    # (An absolute floor would, at low baselines, exceed the whole baseline and
    # call even a halved rate "no meaningful improvement".)
    efficacy_min_effect_rel: float = 0.25
    cohort_shift_tv: float = 0.30          # model/task-class mix change that voids any verdict
    reproposal_growth: float = 2.0
    max_ids: int = 10
    mc_draws: int = 20000


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


def _post(p: BetaPost) -> dict[str, float]:
    return {"rate": round(p.mean, 4), "n_eff": round(p.n_eff, 2)}


# --------------------------------------------------------------------------- metrics
def observations(metric: dict[str, str], corpus: Corpus) -> list[tuple[datetime | None, bool]]:
    """(timestamp, bad) pairs for one pre-registered metric, from its named source only.

    Raises MetricUnavailable when that source was not read: an unmeasured
    metric is never silently measured somewhere else (where it reads 0).
    """
    name, key, source = metric["name"], metric["key"], metric.get("source", "")
    if source not in corpus.sources:
        raise MetricUnavailable(f"{name} reads source {source!r}, which this pass did not read")
    if name == "signature_rate":
        if source == "operator":
            return [(d.ts, d.signature == key) for d in corpus.operator]
        if source == "ledger":
            return [(e.ts, e.signature == key) for e in corpus.ledger_events()
                    if e.action in DECISION_ACTIONS]
        if source == "pxx":
            return [(a.ts, key in a.signatures) for a in corpus.attempts]
    elif name == "class_failure_rate" and source == "pxx":
        return [(a.ts, not a.success) for a in corpus.attempts if a.task_class == key]
    elif name == "gate_fail_rate" and source == "pxx":
        out: list[tuple[datetime | None, bool]] = []
        for a in corpus.attempts:
            failed = a.gate_failed(key)
            if failed is not None:
                out.append((a.ts, failed))
        return out
    elif name == "lane_unavailability":
        if source == "pxx" and key == PXX_REVIEW_LANE:
            return [(a.ts, a.code in REVIEW_FAILURE_CODES
                     or bool(REVIEW_FAILURE_CODES & set(a.contributing_codes)))
                    for a in corpus.attempts if _review_requested(a)]
        if source == "review":
            return [(r.ts, r.result == "NO_REVIEW") for r in corpus.lanes
                    if r.lane == key and r.result != "EXCLUDED"]
    raise ValueError(f"unknown metric {name!r} for source {source!r}")


def _review_requested(a: Attempt) -> bool:
    return (a.gate_failed("review") is not None or a.code in REVIEW_FAILURE_CODES
            or bool(REVIEW_FAILURE_CODES & set(a.contributing_codes)))


class Learner:
    def __init__(self, config: LearnConfig, now: datetime) -> None:
        self.cfg = config
        self.now = now

    def _w(self, ts: datetime | None) -> float:
        return 1.0 if ts is None else decay_weight(ts, self.now, self.cfg.half_life_days)

    def _decayed(self, obs: Iterable[tuple[datetime | None, bool]]) -> BetaPost:
        return beta_from((bad, self._w(ts)) for ts, bad in obs)

    def learn(self, corpus: Corpus, adoptions: Iterable[Adoption]) -> list[Recommendation]:
        adoptions = list(adoptions)
        active = [a for a in adoptions if a.active]
        covered = {(a.metric["name"], a.metric["key"]) for a in active}
        recs: list[Recommendation] = []
        recs += self.recurring_failures(corpus)
        recs += self.variant_preferences(corpus)
        recs += self.gate_regressions(corpus)
        recs += self.human_rejection_themes(corpus)
        recs += self.reviewer_lanes(corpus)
        recs += self.operator_failures(corpus)
        # An adopted change owns its metric: efficacy speaks for it, not a fresh proposal.
        recs = [r for r in recs if (r.metric["name"], r.metric["key"]) not in covered]
        recs += [r for r in (self._ineffective(e)
                             for e in self.efficacy(corpus, active, adoptions)) if r]
        return sorted(recs, key=lambda r: (-r.score, r.rec_key))

    # ------------------------------------------------------------------ recurring pxx failures
    def recurring_failures(self, corpus: Corpus) -> list[Recommendation]:
        hits: dict[str, list[Attempt]] = defaultdict(list)
        by_task: dict[str, list[Attempt]] = defaultdict(list)
        for a in corpus.attempts:
            by_task[a.task_key].append(a)
            for s in a.signatures:
                hits[s].append(a)
        out = []
        for sig_id, runs in hits.items():
            sig = corpus.signatures[sig_id]
            weight = sum(self._w(a.ts) for a in runs)
            tasks = {a.task_key for a in runs}
            if weight < self.cfg.recurrence_min_weight or len(tasks) < self.cfg.recurrence_min_tasks:
                continue
            resolutions = []
            for task in sorted(tasks):
                seq = sorted(by_task[task], key=lambda a: a.ts)
                first = next(i for i, a in enumerate(seq) if sig_id in a.signatures)
                fix = next((a for a in seq[first + 1:] if a.success), None)
                if fix:
                    resolutions.append({"task": task, "failed_run": seq[first].run_id,
                                        "resolved_by": fix.run_id, "model_before": seq[first].model,
                                        "model_after": fix.model})
            is_test = sig.gate == "tests"
            what = (f"test `{sig.sample}` newly fails" if is_test
                    else f"pxx ends {sig.gate} ({sig.error_class}): \"{sig.normalized_text}\"")
            fixed = (f" A later attempt succeeded in {len(resolutions)} task(s); compare those runs."
                     if resolutions else " No later attempt ever succeeded.")
            draft = (f"Recurring: {what} — {len(runs)} runs across {len(tasks)} tasks.{fixed} "
                     + ("Fix or quarantine the test, or add the missing fixture/contract "
                        "to the task specs that keep tripping it." if is_test else
                        "Find the shared cause and remove it at the source (spec template, "
                        "fixture, tooling) rather than retrying past it."))
            out.append(Recommendation(
                rec_key=_key("recurring", sig_id), kind="recurring_failure",
                subject=f"{'test' if is_test else sig.gate}: {sig.normalized_text[:60]}",
                score=round(weight * SURFACE_COST.get(sig.gate, SURFACE_COST["pxx"]), 3),
                evidence_weight=round(weight, 3),
                evidence={"where": sig.gate, "error_class": sig.error_class,
                          "pattern": sig.normalized_text, "sample": sig.sample[:400],
                          "runs": len(runs), "tasks": len(tasks), "decayed_weight": round(weight, 2),
                          "last_seen": max(a.ts for a in runs).isoformat(),
                          "example_runs": sorted(a.run_id for a in runs)[-self.cfg.max_ids:],
                          "resolutions": resolutions[:self.cfg.max_ids]},
                draft_text=draft, metric={"name": "signature_rate", "key": sig_id, "source": "pxx"},
                suggested_role="sdet" if is_test else "tech-lead"))
        return out

    # ------------------------------------------------------------------ model variants
    def variant_preferences(self, corpus: Corpus) -> list[Recommendation]:
        groups: dict[str, dict[str, list[tuple[datetime | None, bool]]]] = \
            defaultdict(lambda: defaultdict(list))
        run_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
        for a in corpus.attempts:
            groups[a.task_class][a.model].append((a.ts, not a.success))
            run_ids[(a.task_class, a.model)].append(a.run_id)
        out = []
        for task_class, models in sorted(groups.items()):
            posts = {m: self._decayed(obs) for m, obs in models.items()}
            trusted = {m: p for m, p in posts.items() if p.n_eff >= self.cfg.min_eff_n}
            if len(trusted) < 2:
                continue
            best = min(trusted, key=lambda m: (trusted[m].mean, m))   # lowest failure rate
            beaten: dict[str, Any] = {}
            for m, p in sorted(trusted.items()):
                if m == best:
                    continue
                pg = prob_greater(p, trusted[best], self.cfg.mc_draws)  # P(other fails more)
                if pg >= self.cfg.variant_confidence:
                    beaten[m] = {"p_best_better": round(pg, 4), **_post(p)}
            if not beaten:
                continue
            lift = min(trusted[m].mean for m in beaten) - trusted[best].mean
            out.append(Recommendation(
                rec_key=_key("variant", task_class, best), kind="variant_preference",
                subject=f"{task_class}: prefer {best}",
                score=round(lift * trusted[best].n_eff * 2.0, 3),
                evidence_weight=round(trusted[best].n_eff, 3),
                evidence={"task_class": task_class, "best": best, "best_failure": _post(trusted[best]),
                          "dominates": beaten,
                          "too_few_runs": sorted(set(posts) - set(trusted)),
                          "example_runs": sorted(run_ids[(task_class, best)])[-self.cfg.max_ids:]},
                draft_text=(f"In {task_class}, {best} fails ~{trusted[best].mean:.0%} of pxx runs and "
                            f"beats {', '.join(sorted(beaten))} with >= "
                            f"{self.cfg.variant_confidence:.0%} probability. Route {task_class} work "
                            f"to {best} (the dx role route in the hardware manifest, or pxx's model "
                            f"setting), then let efficacy confirm it."),
                metric={"name": "class_failure_rate", "key": task_class, "source": "pxx"},
                suggested_role="platform-engineer"))
        return out

    # ------------------------------------------------------------------ gate regressions
    def gate_regressions(self, corpus: Corpus) -> list[Recommendation]:
        recent_from = self.now - timedelta(days=self.cfg.regression_recent_days)
        base_from = self.now - timedelta(days=self.cfg.regression_baseline_days)
        gates = sorted({g.gate for a in corpus.attempts for g in a.gates})
        out = []
        for gate in gates:
            obs = observations({"name": "gate_fail_rate", "key": gate, "source": "pxx"}, corpus)
            recent = [(bad, 1.0) for ts, bad in obs if ts and ts >= recent_from]
            base = [(bad, 1.0) for ts, bad in obs if ts and base_from <= ts < recent_from]
            rp, bp = beta_from(recent), beta_from(base)
            if rp.n_eff < self.cfg.min_eff_n or bp.n_eff < self.cfg.min_eff_n:
                continue
            rise = rp.mean - bp.mean
            p_up = prob_greater(rp, bp, self.cfg.mc_draws)
            if rise < self.cfg.regression_min_drop or p_up < self.cfg.regression_confidence:
                continue
            out.append(Recommendation(
                rec_key=_key("regression", gate), kind="gate_regression",
                subject=f"pxx {gate} gate regression",
                score=round(rise * rp.n_eff * SURFACE_COST.get(gate, 2.0), 3),
                evidence_weight=round(rp.n_eff, 3),
                evidence={"gate": gate, "recent_fail": _post(rp), "baseline_fail": _post(bp),
                          "p_regressed": round(p_up, 4),
                          "recent_days": self.cfg.regression_recent_days},
                draft_text=(f"pxx's {gate} gate now fails ~{rp.mean:.0%} of the time, up from "
                            f"~{bp.mean:.0%} (P={p_up:.2f}). Look at what changed in the last "
                            f"{self.cfg.regression_recent_days} days: model or route, pxx version, "
                            f"prompts, the test suite itself. Do not relax the gate to recover."),
                metric={"name": "gate_fail_rate", "key": gate, "source": "pxx"}, suggested_role="sre"))
        return out

    # ------------------------------------------------------------------ human rejections (ledger)
    def human_rejection_themes(self, corpus: Corpus) -> list[Recommendation]:
        weights: dict[str, float] = defaultdict(float)
        tasks: dict[str, set[str]] = defaultdict(set)
        total = 0.0
        for e in corpus.ledger_events():
            if e.action in NEGATIVE_ACTIONS and e.signature:
                w = self._w(e.ts)
                weights[e.signature] += w
                tasks[e.signature].add(e.task_id)
                total += w
        green: dict[str, str] = {}   # task -> a COMPLETED pxx run that preceded the rejection
        for a in corpus.attempts:
            if a.dx_task_id and a.success:
                green.setdefault(a.dx_task_id, a.run_id)
        out = []
        for sig_id, w in weights.items():
            share = w / total if total else 0.0
            if w < self.cfg.rejection_min_weight or share < self.cfg.rejection_min_share:
                continue
            sig = corpus.signatures[sig_id]
            honest_green = sorted(t for t in tasks[sig_id] if t in green)
            gap = (f" pxx reported COMPLETED on {len(honest_green)} of these tasks: the automated "
                   f"gates passed work a human then refused." if honest_green else "")
            out.append(Recommendation(
                rec_key=_key("rejection", sig_id), kind="human_rejection_theme",
                subject=f"{sig.gate}: {sig.normalized_text[:60]}",
                score=round(w * SURFACE_COST["ledger"], 3), evidence_weight=round(w, 3),
                evidence={"action": sig.gate, "pattern": sig.normalized_text,
                          "sample": sig.sample[:400], "decayed_weight": round(w, 2),
                          "share_of_negative_rows": round(share, 4),
                          "tasks": sorted(tasks[sig_id])[-self.cfg.max_ids:],
                          "pxx_green_then_refused": honest_green[-self.cfg.max_ids:]},
                draft_text=(f"{sig.gate} rows of this shape are ~{share:.0%} of negative ledger "
                            f"outcomes across {len(tasks[sig_id])} tasks: \"{sig.normalized_text}\"."
                            f"{gap} A human is catching this at the most expensive point. Encode it "
                            f"as an automated check or a spec-template rule so it fails before review."),
                metric={"name": "signature_rate", "key": sig_id, "source": "ledger"},
                suggested_role="sdet"))
        return out

    # ------------------------------------------------------------------ reviewer lanes
    def reviewer_lanes(self, corpus: Corpus) -> list[Recommendation]:
        lanes = sorted({r.lane for r in corpus.lanes} | (
            {PXX_REVIEW_LANE} if any(_review_requested(a) for a in corpus.attempts) else set()))
        out = []
        bar = 1.0 - self.cfg.lane_availability_bar   # max acceptable unavailability
        for lane in lanes:
            metric = {"name": "lane_unavailability", "key": lane,
                      "source": "pxx" if lane == PXX_REVIEW_LANE else "review"}
            p = self._decayed(observations(metric, corpus))
            if p.n_eff < self.cfg.min_eff_n:
                continue
            # P(unavailability > bar) via the posterior against a point bar.
            p_bad = prob_greater(p, BetaPost(bar * 1e6, (1 - bar) * 1e6), self.cfg.mc_draws)
            if p_bad < self.cfg.lane_confidence:
                continue
            out.append(Recommendation(
                rec_key=_key("lane", lane), kind="reviewer_lane",
                subject=f"review lane {lane} unreliable",
                score=round(p.mean * p.n_eff * SURFACE_COST["lane"], 3),
                evidence_weight=round(p.n_eff, 3),
                evidence={"lane": lane, "no_review": _post(p), "p_below_bar": round(p_bad, 4),
                          "availability_bar": self.cfg.lane_availability_bar},
                draft_text=(f"The {lane} review lane returns no review ~{p.mean:.0%} of the time "
                            f"(P={p_bad:.2f} it is below the {self.cfg.lane_availability_bar:.0%} "
                            f"availability bar). A lane that rarely answers is not coverage. Fix "
                            f"its timeout, invocation or output contract. Until it answers, keep "
                            f"recording NO_REVIEW: it never counts toward a required lane."),
                metric=metric, suggested_role="release-manager"))
        return out

    # ------------------------------------------------------------------ psoperator
    def operator_failures(self, corpus: Corpus) -> list[Recommendation]:
        hits: dict[str, list[datetime]] = defaultdict(list)
        for d in corpus.operator:
            if d.signature:
                hits[d.signature].append(d.ts)
        out = []
        for sig_id, stamps in hits.items():
            w = sum(self._w(t) for t in stamps)
            if w < self.cfg.recurrence_min_weight:
                continue
            sig = corpus.signatures[sig_id]
            decision = sig.gate.split(":", 1)[1]
            advice = {
                "denied": "The agent keeps proposing actions a human refuses. Teach the planner "
                          "not to propose them; never widen auto-approval to absorb them.",
                "rejected-stale": "Actions keep going stale between capture and execution. Find "
                                  "the latency; do not widen the freshness window.",
                "rejected-target": "Actions keep targeting elements the frame does not hold. "
                                   "Look at grounding and locator layers.",
            }.get(decision, "Find the shared cause in the executor or skill and fix it there.")
            out.append(Recommendation(
                rec_key=_key("operator", sig_id), kind="operator_failure",
                subject=f"psoperator {decision}: {sig.normalized_text[:50]}",
                score=round(w * SURFACE_COST["operator"], 3), evidence_weight=round(w, 3),
                evidence={"decision": decision, "pattern": sig.normalized_text,
                          "sample": sig.sample[:400], "count": len(stamps),
                          "decayed_weight": round(w, 2),
                          "last_seen": max(stamps).isoformat()},
                draft_text=f"psoperator {decision} x{len(stamps)}: \"{sig.normalized_text}\". {advice}",
                metric={"name": "signature_rate", "key": sig_id, "source": "operator"},
                suggested_role="tech-lead"))
        return out

    # ------------------------------------------------------------------ efficacy (the meta-loop)
    def efficacy(self, corpus: Corpus, adoptions: Iterable[Adoption],
                 all_adoptions: Iterable[Adoption] | None = None) -> list[dict[str, Any]]:
        """Before/after comparison of each adoption's pre-registered bad-event rate,
        split at the first MERGED row of its implementing task at or after acceptance.

        This is an OBSERVED change, not a causal estimate. Anything else that
        changed across the merge — the mix of tasks or models, the pxx version,
        another adopted change on the same metric, or plain regression to the
        mean (a proposal is raised *because* a rate was high, so it tends to
        fall anyway) — moves the same numbers. So:

        * "improvement-observed" needs P(rate fell) >= 0.90 with the raw rate lower;
        * "no-meaningful-improvement" — the only status that prompts a review of
          revising or reverting — needs >= 30 observations on each side and
          P >= 0.95 that any improvement is below 25% of the before-rate;
        * "after" is the 60 days from the merge, not open-ended, and any other
          adoption on the same metric (active or retired) merged in the 60 days
          before or after voids the comparison;
        * a cohort shift (model or task-class mix) or an overlapping adoption on
          the same metric voids both verdicts;
        * everything else, including equal or worse rates on weak evidence, is
          "inconclusive".
        """
        cfg = self.cfg
        window = timedelta(days=cfg.efficacy_window_days)
        adoptions = list(adoptions)
        context = list(all_adoptions) if all_adoptions is not None else adoptions
        starts: dict[str, datetime] = {}
        for ad in {a.rec_key: a for a in [*context, *adoptions]}.values():
            task = corpus.tasks.get(ad.task_id) if ad.task_id else None
            merged = task.merged_at(not_before=ad.accepted_at) if task else None
            if merged is not None:
                starts[ad.rec_key] = merged
        report = []
        for ad in adoptions:
            row: dict[str, Any] = {"adoption": ad, "status": "", "before": None, "after": None,
                                   "p_rate_fell": None, "runs_after": 0, "runs_before": 0,
                                   "observed_change": None, "caveats": []}
            report.append(row)
            if not ad.task_id:
                row["status"] = "awaiting-task"
                continue
            task = corpus.tasks.get(ad.task_id)
            if task is None:
                row["status"] = "awaiting-merge"
                continue
            start = starts.get(ad.rec_key)
            if start is None:
                row["status"] = ("merged-before-adoption" if task.merged_at() is not None
                                 else "awaiting-merge")
                continue
            try:
                obs = [(ts, bad) for ts, bad in observations(ad.metric, corpus) if ts is not None]
            except MetricUnavailable:
                row["status"] = "source-not-read"
                continue
            before = [bad for ts, bad in obs if start - window <= ts < start]
            after = [bad for ts, bad in obs if start <= ts < start + window]
            bp = beta_from((b, 1.0) for b in before)
            ap = beta_from((b, 1.0) for b in after)
            row.update(before=bp, after=ap, runs_after=len(after), runs_before=len(before))
            caveats: list[str] = row["caveats"]
            caveats.append("observed before/after change, not a causal estimate; includes "
                           "regression to the mean and any concurrent change")
            if len(after) < cfg.efficacy_min_after or len(before) < cfg.efficacy_min_before:
                row["status"] = (f"measuring (before {len(before)}/{cfg.efficacy_min_before}, "
                                 f"after {len(after)}/{cfg.efficacy_min_after})")
                continue
            raw_before = sum(before) / len(before)
            raw_after = sum(after) / len(after)
            row["observed_change"] = round(raw_after - raw_before, 4)
            p_fell = prob_greater(bp, ap, cfg.mc_draws)
            row["p_rate_fell"] = p_fell
            metrics = {a.rec_key: a.metric for a in [*context, *adoptions]}
            overlap = sorted(k[:8] for k, other in starts.items() if k != ad.rec_key
                             and start - window <= other < start + window
                             and metrics[k] == ad.metric)
            shift = self._cohort_shift(ad.metric, corpus, start, window)
            if overlap:
                caveats.append(f"other adoptions on this metric merged within the window: "
                               f"{', '.join(overlap)} — changes cannot be told apart")
            if shift:
                caveats.append(shift)
            if raw_before == 0:
                row["status"] = "no-baseline-problem"   # nothing was failing to begin with
                continue
            if overlap or shift:
                row["status"] = "inconclusive (confounded)"
                continue
            delta = cfg.efficacy_min_effect_rel * raw_before
            if p_fell >= cfg.efficacy_confidence and raw_after < raw_before:
                row["status"] = "improvement-observed"
            elif (min(len(before), len(after)) >= cfg.efficacy_min_verdict
                  and prob_diff_below(bp, ap, delta, cfg.mc_draws) >= cfg.efficacy_null_confidence):
                row["status"] = "no-meaningful-improvement"
                row["min_meaningful_effect"] = round(delta, 4)
            else:
                row["status"] = "inconclusive"
        return report

    def _cohort_shift(self, metric: dict[str, str], corpus: Corpus, start: datetime,
                      window: timedelta) -> str | None:
        """Did the population being measured change across the merge? Only pxx
        attempts carry the descriptors (model, task class) needed to tell."""
        if metric.get("source") != "pxx":
            return None
        def mix(attr: str, lo: datetime, hi: datetime | None) -> dict[str, int]:
            out: dict[str, int] = defaultdict(int)
            for a in corpus.attempts:
                if a.ts >= lo and (hi is None or a.ts < hi) and (
                        metric["name"] != "class_failure_rate" or a.task_class == metric["key"]):
                    out[getattr(a, attr)] += 1
            return out
        for attr in ("model", "task_class"):
            tv = total_variation(mix(attr, start - window, start), mix(attr, start, None))
            if tv > self.cfg.cohort_shift_tv:
                return (f"cohort shifted: the {attr.replace('_', ' ')} mix changed across the "
                        f"merge (total variation {tv:.2f})")
        return None

    def _ineffective(self, row: dict[str, Any]) -> Recommendation | None:
        if row["status"] != "no-meaningful-improvement":
            return None
        ad: Adoption = row["adoption"]
        bp, ap = row["before"], row["after"]
        return Recommendation(
            rec_key=_key("efficacy", ad.rec_key), kind="adoption_no_improvement",
            subject=f"adopted change {ad.rec_key[:8]} ({ad.task_id}): no meaningful improvement observed",
            score=round(ap.n_eff * ap.mean * 3.0, 3), evidence_weight=float(row["runs_after"]),
            evidence={"rec_key": ad.rec_key, "task_id": ad.task_id, "metric": ad.metric,
                      "before": _post(bp), "after": _post(ap),
                      "runs_before": row["runs_before"], "runs_after": row["runs_after"],
                      "observed_change": row["observed_change"],
                      "min_meaningful_effect": row["min_meaningful_effect"],
                      "p_rate_fell": round(row["p_rate_fell"], 4), "caveats": row["caveats"]},
            draft_text=(f"After {ad.task_id} merged (from {ad.rec_key[:8]}), {ad.metric['name']} "
                        f"went from ~{bp.mean:.0%} to ~{ap.mean:.0%} over {row['runs_before']} and "
                        f"{row['runs_after']} observations; any improvement is very likely below "
                        f"{row['min_meaningful_effect']:.0%}. This is an observed before/after "
                        f"comparison, not proof the change has no effect. Review whether to revise "
                        f"or revert it."),
            metric=dict(ad.metric), suggested_role="tech-lead")


# --------------------------------------------------------------------------- queue reconciliation
def reconcile(fresh: list[Recommendation], existing: dict[str, Recommendation],
              config: LearnConfig, now: datetime) -> list[Recommendation]:
    """Merge one pass into the queue; return the records to persist.

    PROPOSED/STALE   -> refreshed with this pass's evidence
    ACCEPTED         -> untouched; efficacy owns it from here
    REJECTED/RETIRED -> stays down unless evidence grew reproposal_growth x, then
                        re-raised with the earlier decision attached
    PROPOSED but not produced this pass -> STALE (the condition no longer holds)
    """
    out: list[Recommendation] = []
    seen: set[str] = set()
    for rec in fresh:
        seen.add(rec.rec_key)
        prev = existing.get(rec.rec_key)
        if prev is None:
            rec.status, rec.created_at, rec.updated_at = "PROPOSED", now, now
            out.append(rec)
            continue
        if prev.status == "ACCEPTED":
            continue
        if prev.status in ("REJECTED", "RETIRED"):
            then = prev.weight_at_decision or prev.evidence_weight
            if rec.evidence_weight < then * config.reproposal_growth:
                continue
            rec.evidence = {**rec.evidence, "previous_decision": {
                "status": prev.status, "by": prev.decided_by,
                "at": prev.decided_at.isoformat() if prev.decided_at else None,
                "note": prev.decision_note, "weight_then": then}}
            rec.draft_text = (f"[Re-raised: evidence grew {rec.evidence_weight / max(then, 1e-9):.1f}x "
                              f"since it was {prev.status.lower()}] " + rec.draft_text)
        rec.status, rec.created_at, rec.updated_at = "PROPOSED", prev.created_at or now, now
        rec.revision = prev.revision   # the revision this pass read: the write is checked against it
        out.append(rec)
    for key, prev in existing.items():
        if key not in seen and prev.status == "PROPOSED":
            prev.status, prev.updated_at = "STALE", now
            out.append(prev)
    return out
