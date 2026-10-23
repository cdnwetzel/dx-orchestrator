"""The three detector views over one corpus (run-intent stratification).

Ingestion is complete and unchanged: every readable run enters the corpus and
the all-runs accounting, whatever its intent. A view only selects which
attempts the detectors see — it never drops, relabels or rewrites a record,
and exclusion from a view never touches ingestion, all-runs accounting,
resolution tracking or efficacy-source presence.

* ``all-runs`` — every attempt. The continuity baseline; the only view whose
  proposals predate views, and the only one whose ``rec_key`` derivation is
  unchanged byte-for-byte.
* ``operational-analysis`` (CONSERVATIVE) — excludes only first-party,
  non-contradictory probe/eval/drill declarations. Everything unverified or
  contradictory stays: a caller cannot remove evidence from this view by
  labeling it.
* ``declared-intent-filtered`` (EXPLORATORY) — excludes every positively
  declared probe/eval/drill run regardless of provenance. Labeled exploratory
  wherever shown; its proposals carry no decision weight and are refused by
  the forwarding and task-generation paths.

``unknown`` is never excluded from any view.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .learn_model import Attempt, Corpus

ALL_RUNS = "all-runs"
CONSERVATIVE = "operational-analysis"
EXPLORATORY = "declared-intent-filtered"
VIEWS = (ALL_RUNS, CONSERVATIVE, EXPLORATORY)

VIEW_LABELS = {
    ALL_RUNS: "all runs (continuity baseline)",
    CONSERVATIVE: "operational analysis (conservative: only first-party, "
                  "non-contradictory probe/eval/drill declarations are excluded)",
    EXPLORATORY: "EXPLORATORY — declared-intent filtering; proposals from this view "
                 "carry no decision weight",
}

# The intents a view may exclude. "operational" is never excluded (it is the
# subject of the analysis), and "unknown" can never be excluded by intent rules.
EXCLUDED_INTENTS = frozenset({"probe", "eval", "drill"})

# The exclusion-policy versions, deliberately two constants:
#
# * GENESIS_POLICY — the policy in force before a view's FIRST seam. It is
#   never bumped: policy_at() resolves times before the first seam to it, so
#   a seam always splits the span into the version before and the version
#   after. (If the pre-seam default followed the current version instead,
#   landing the first seam and bumping the current version would make
#   before == after == the new version, and a comparison straddling the seam
#   would silently compute across it — what A5 forbids.)
# * CURRENT_POLICY — what NEW adoptions bind at proposal time. Landing a seam
#   means two edits: append it to POLICY_SEAMS and bump CURRENT_POLICY[view]
#   (and POLICY_VERSION below, its scalar source). GENESIS_POLICY stays.
#
# With no seams recorded, genesis == current == "v1".
GENESIS_POLICY = "v1"
POLICY_VERSION = "v1"          # current version new adoptions bind; bump at seam-landing
CURRENT_POLICY = {view: POLICY_VERSION for view in VIEWS}


@dataclass(frozen=True)
class PolicySeam:
    """One exclusion-policy change: ``version`` took effect at ``at``."""
    version: str
    at: datetime
    description: str            # what changed ("new excluded intent value: …")


# view -> seams in effect-time order. Empty: no view definition has ever changed.
POLICY_SEAMS: dict[str, list[PolicySeam]] = {}


@dataclass(frozen=True)
class ViewReport:
    """Per-pass accounting for one view. Exclusions are measured, never silent."""
    view: str
    included: int
    excluded: int
    # intent/provenance -> {"included": n, "excluded": n}
    by_intent: dict[str, dict[str, int]] = field(default_factory=dict)
    by_provenance: dict[str, dict[str, int]] = field(default_factory=dict)
    # Excluded non-COMPLETED runs, by run id: a failed operational-looking job
    # must not disappear without a trace.
    excluded_but_failed: tuple[tuple[str, str], ...] = ()   # (run_id, terminal code)
    # Mixed-provenance warnings, surfaced by run id in EVERY view's report.
    mixed_provenance: dict[str, str] = field(default_factory=dict)


def policy_at(view: str, t: datetime, seams: dict[str, list[PolicySeam]] | None = None) -> str:
    """The exclusion-policy version in force for ``view`` at time ``t``.

    Times before the first seam resolve to GENESIS_POLICY — never to the
    current version — so a seam always voids comparisons straddling it."""
    version = GENESIS_POLICY
    for s in sorted((POLICY_SEAMS if seams is None else seams).get(view, []), key=lambda s: s.at):
        if s.at <= t:
            version = s.version
    return version


def is_first_party(attempt: Attempt) -> bool:
    """A clean first-party record. "first-party" identifies a code path; it is
    not cryptographic attestation, and anything not cleanly first-party was
    already treated as not-first-party at read time (learn_sources)."""
    return attempt.run_intent_provenance == "first-party"


def mixed_provenance(corpus: Corpus) -> dict[str, str]:
    """Runs declared probe/eval/drill that are contradicted: their project/scope
    also carries operational-declared runs, or they link to a ledger task.
    Returned run_id -> reason; such runs are excluded ONLY in the exploratory
    view and stay in the conservative one. No auto-relabeling either way."""
    operational_classes = {a.task_class for a in corpus.attempts if a.run_intent == "operational"}
    out: dict[str, str] = {}
    for a in corpus.attempts:
        if a.run_intent not in EXCLUDED_INTENTS:
            continue
        reasons = []
        if a.task_class in operational_classes:
            reasons.append(f"scope {a.task_class!r} also carries operational-declared runs")
        if a.dx_task_id:
            reasons.append(f"linked to ledger task {a.dx_task_id}")
        if reasons:
            out[a.run_id] = "; ".join(reasons)
    return out


def select(corpus: Corpus, view: str, mixed: dict[str, str] | None = None) -> tuple[list[Attempt], ViewReport]:
    """The attempts the detectors see for ``view``, plus the view's accounting."""
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}")
    if mixed is None:
        mixed = mixed_provenance(corpus)
    included: list[Attempt] = []
    excluded_failed: list[tuple[str, str]] = []
    by_intent: dict[str, dict[str, int]] = defaultdict(lambda: {"included": 0, "excluded": 0})
    by_provenance: dict[str, dict[str, int]] = defaultdict(lambda: {"included": 0, "excluded": 0})
    n_excluded = 0
    for a in corpus.attempts:
        excluded = False
        if a.run_intent in EXCLUDED_INTENTS:
            if view == EXPLORATORY:
                excluded = True                    # positively declared, any provenance
            elif view == CONSERVATIVE:
                excluded = is_first_party(a) and a.run_id not in mixed
        outcome = "excluded" if excluded else "included"
        if excluded:
            n_excluded += 1
            if not a.success:
                excluded_failed.append((a.run_id, a.code))
        else:
            included.append(a)
        by_intent[a.run_intent][outcome] += 1
        by_provenance[a.run_intent_provenance or "none"][outcome] += 1
    report = ViewReport(view=view, included=len(included), excluded=n_excluded,
                        by_intent=dict(by_intent), by_provenance=dict(by_provenance),
                        excluded_but_failed=tuple(sorted(excluded_failed)),
                        mixed_provenance=dict(mixed))
    return included, report
