"""Data types for dx learn.

dx learn reads four independent records of past iterations and joins them:

    pxx run directories      one Attempt per pxx run (the machine's view)
    the devswarm ledger      one TaskRecord per dx task (the humans' view)
    the psoperator audit     one OperatorDecision per gatekeeper decision
    review manifests         one LaneResult per review-lane receipt

The join that matters is EXECUTED rows: dx writes ``run=<pxx run id>`` into
their evidence, which ties a pxx attempt to the task a human later merged,
redlined or abandoned.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# Status-bearing ledger actions that record a decision about a task.
DECISION_ACTIONS = frozenset({"MERGED", "REDLINE", "INCOMPLETE", "ABANDONED", "ESCALATED"})
NEGATIVE_ACTIONS = frozenset({"REDLINE", "INCOMPLETE", "ABANDONED", "ESCALATED"})

# Review-manifest results (sdlc-agent-roles review/RECEIPTS.md). EXCLUDED is a
# deliberate omission, so it is never counted for or against a lane.
LANE_RESULTS = frozenset({"PASS", "PASS_WITH_FINDINGS", "FAIL", "NO_REVIEW", "EXCLUDED"})

# What a learned recommendation may be. Closed on purpose: there is no kind
# that relaxes a gate, a budget or a risk tier, and a test holds that line.
REC_KINDS = frozenset({
    "recurring_failure",       # one failure signature keeps coming back across tasks
    "variant_preference",      # one model credibly outperforms another for a task class
    "gate_regression",         # a pxx gate's pass rate fell versus its baseline
    "human_rejection_theme",   # one kind of human rejection dominates the ledger
    "reviewer_lane",           # a review lane fails to produce reviews too often
    "operator_failure",        # psoperator keeps failing or being denied the same way
    "adoption_ineffective",    # an adopted change did not move its metric
})

REC_STATUSES = frozenset({"PROPOSED", "ACCEPTED", "REJECTED", "RETIRED", "STALE"})

# Ranking weight for where a problem surfaces: later and more human = costlier.
SURFACE_COST = {
    "pxx": 2.0, "tests": 2.0, "lint": 1.5, "review": 2.0, "scope_recheck": 1.5,
    "diff_budget": 1.5, "ledger": 4.0, "operator": 3.0, "lane": 2.0,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat()


def parse_iso(value: Any, what: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what}: expected an ISO-8601 string, got {value!r}")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"{what}: timestamp {value!r} has no timezone")
    return dt.astimezone(UTC)


def from_epoch(value: Any, what: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what}: expected epoch seconds, got {value!r}")
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{what}: {value!r} is not a usable timestamp") from exc


@dataclass(frozen=True)
class Signature:
    signature_id: str
    gate: str            # where it surfaced: a pxx gate/terminal code, a ledger action, an operator decision
    error_class: str
    normalized_text: str
    sample: str


@dataclass(frozen=True)
class GateObs:
    gate: str
    allowed: bool


@dataclass(frozen=True)
class Attempt:
    """One pxx run, read from ``<state_dir>/runs/<run_id>/``."""
    run_id: str
    ts: datetime
    task_key: str                 # dx task id when linked, else pxx's task_id
    task_class: str               # dx scope when linked, else pxx's project
    model: str                    # provider/model: the variant dx can route
    agent_version_id: str
    code: str                     # pxx TerminalCode
    contributing_codes: tuple[str, ...]
    gates: tuple[GateObs, ...]
    signatures: tuple[str, ...]
    dx_task_id: str | None = None

    @property
    def success(self) -> bool:
        return self.code == "COMPLETED"

    def gate_failed(self, gate: str) -> bool | None:
        """None when the gate never ran in this attempt."""
        seen = [g for g in self.gates if g.gate == gate]
        if not seen:
            return None
        return any(not g.allowed for g in seen)


@dataclass(frozen=True)
class LedgerEvent:
    ts: datetime
    task_id: str
    action: str
    evidence: str
    author_human: str | None
    reviewer_seat: str | None
    signature: str | None = None   # set for negative actions


@dataclass
class TaskRecord:
    task_id: str
    events: list[LedgerEvent] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)   # from EXECUTED run=
    scope: str | None = None                           # from EXECUTED scope=

    def merged_at(self, not_before: datetime | None = None) -> datetime | None:
        """The first MERGED row at or after ``not_before`` (the first at all if None)."""
        merged = sorted(e.ts for e in self.events if e.action == "MERGED"
                        and (not_before is None or e.ts >= not_before))
        return merged[0] if merged else None


@dataclass(frozen=True)
class OperatorDecision:
    seq: int
    ts: datetime
    decision: str
    tier: int
    approver: str
    action_kind: str
    signature: str | None


@dataclass(frozen=True)
class LaneResult:
    release: str
    review_id: str
    lane: str
    result: str
    ts: datetime | None            # manifest commit time; None if unknown


@dataclass(frozen=True)
class Quarantined:
    source: str
    where: str
    error: str


@dataclass
class Corpus:
    attempts: list[Attempt] = field(default_factory=list)
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    operator: list[OperatorDecision] = field(default_factory=list)
    lanes: list[LaneResult] = field(default_factory=list)
    signatures: dict[str, Signature] = field(default_factory=dict)
    quarantined: list[Quarantined] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # source -> what was read, for the report
    ledger_head: str | None = None

    def ledger_events(self) -> list[LedgerEvent]:
        return sorted((e for t in self.tasks.values() for e in t.events), key=lambda e: e.ts)


@dataclass
class Recommendation:
    rec_key: str
    kind: str
    subject: str
    score: float
    evidence_weight: float
    evidence: dict[str, Any]
    draft_text: str
    # The pre-registered metric: how acceptance will be judged later.
    # {"name", "key", "source"} — always a "bad event" rate, read from one named
    # source (pxx | ledger | operator | review), never from a fallback.
    metric: dict[str, str]
    targets: tuple[str, ...] = ()       # paths a change would likely touch, if known
    suggested_role: str = "tech-lead"
    status: str = "PROPOSED"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    weight_at_decision: float | None = None
    pxx_slug: str | None = None          # set once forwarded to pxx's improve inbox
    # Optimistic-concurrency revision: 0 = never stored. Every committed write
    # bumps it; a write naming a stale revision is refused (see learn_store).
    revision: int = 0


@dataclass
class Adoption:
    """An accepted recommendation, tracked until its metric says whether it worked."""
    rec_key: str
    metric: dict[str, str]
    accepted_by: str
    accepted_at: datetime
    note: str = ""
    task_id: str | None = None       # the dx task implementing it; efficacy starts at its MERGED row
    active: bool = True
    retired_at: datetime | None = None
    revision: int = 0


# Forwarding lifecycle (dx's side) and what dx last saw in pxx's inbox.
FORWARD_STATES = frozenset({
    "INTENT",        # durable before any pxx write; a crash here is resolved by reconcile
    "WRITTEN",       # our entry was created in pxx's human-review inbox
    "NOT_WRITTEN",   # refused: dispositioned already, slug pending, or cycle.lock busy
    "INTERRUPTED",   # intent recorded, no entry found afterwards: never written
    "WITHDRAWN",     # our own pending entry removed after dx rejected/retired
})
PXX_VIEWS = frozenset({"pending-ours", "pending-other", "qualified", "rejected",
                       "qualified-other", "rejected-other", "absent"})


@dataclass
class ForwardRecord:
    """dx's durable record of one forwarding to pxx, per recommendation."""
    rec_key: str
    slug: str
    payload_sha256: str
    state: str
    rec_revision: int                 # the recommendation revision the intent was made against
    updated_at: datetime
    pxx_view: str | None = None       # last observation of pxx's inbox for this slug
    divergence: str | None = None     # dx and pxx humans decided differently; never auto-resolved
    note: str = ""
    revision: int = 0


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return iso(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    return obj


def rec_from_json(d: dict[str, Any]) -> Recommendation:
    def dt(k: str) -> datetime | None:
        v = d.get(k)
        return parse_iso(v, k) if v else None
    return Recommendation(
        rec_key=d["rec_key"], kind=d["kind"], subject=d["subject"], score=float(d["score"]),
        evidence_weight=float(d["evidence_weight"]), evidence=dict(d["evidence"]),
        draft_text=d["draft_text"], metric=dict(d["metric"]), targets=tuple(d.get("targets") or ()),
        suggested_role=d.get("suggested_role") or "tech-lead", status=d["status"],
        created_at=dt("created_at"), updated_at=dt("updated_at"), decided_by=d.get("decided_by"),
        decided_at=dt("decided_at"), decision_note=d.get("decision_note"),
        weight_at_decision=d.get("weight_at_decision"), pxx_slug=d.get("pxx_slug"),
        revision=int(d.get("revision", 0)))


def adoption_from_json(d: dict[str, Any]) -> Adoption:
    return Adoption(
        rec_key=d["rec_key"], metric=dict(d["metric"]), accepted_by=d["accepted_by"],
        accepted_at=parse_iso(d["accepted_at"], "accepted_at"), note=d.get("note") or "",
        task_id=d.get("task_id"), active=bool(d.get("active", True)),
        retired_at=parse_iso(d["retired_at"], "retired_at") if d.get("retired_at") else None,
        revision=int(d.get("revision", 0)))


def forward_from_json(d: dict[str, Any]) -> ForwardRecord:
    return ForwardRecord(
        rec_key=d["rec_key"], slug=d["slug"], payload_sha256=d["payload_sha256"],
        state=d["state"], rec_revision=int(d["rec_revision"]),
        updated_at=parse_iso(d["updated_at"], "updated_at"), pxx_view=d.get("pxx_view"),
        divergence=d.get("divergence"), note=d.get("note") or "",
        revision=int(d.get("revision", 0)))
