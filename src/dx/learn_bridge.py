"""Where an accepted or forwarded recommendation goes next.

Two exits, both into machinery that already has human gates:

* ``forward_to_pxx`` — a model-routing recommendation is something pxx's own
  improvement plane knows how to carry (target ``model``, operation
  ``switch_model``). It is written into pxx's ``inbox/human-review-required/``
  in pxx's exact Proposal shape, under pxx's own ``cycle.lock``, so
  ``pxx improve triage`` and ``pxx promote`` stay the only path to production.
  A signature a human has already dispositioned in pxx is never re-raised
  from here, and an entry pxx itself put in the inbox is never overwritten.
* ``render_spec`` — every accepted recommendation becomes a dx task spec
  (``templates/spec.md`` shape). The change itself is then made by ``dx run``,
  reviewed, and merged with a signature like any other task. dx learn's
  acceptance is permission to do the work, not the work.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

from .approval_tier import CONTROL_PLANE, classify
from .learn_model import Recommendation

FORWARDABLE_KINDS = frozenset({"variant_preference"})


class ForwardError(Exception):
    pass


def forward_to_pxx(rec: Recommendation, state_dir: Path) -> tuple[str, str]:
    """Write ``rec`` into pxx's human-review inbox. Returns (slug, outcome)."""
    if rec.kind not in FORWARDABLE_KINDS:
        raise ForwardError(f"{rec.kind} has no pxx improvement target; it stays in dx learn")
    try:
        from pxx.improve.cycle import INBOX_HUMAN, human_disposition, proposal_slug
        from pxx.improve.mining import Proposal, RootCause
    except ImportError as exc:  # pragma: no cover - pxx is a hard dependency of dx
        raise ForwardError(f"pxx's improvement plane is not importable: {exc}") from exc

    best = str(rec.evidence.get("best", ""))
    runs = tuple(str(r) for r in rec.evidence.get("example_runs", []))
    if not best or not runs:
        raise ForwardError("variant recommendation has no model or run evidence to forward")
    p_values = [float(v["p_best_better"]) for v in rec.evidence.get("dominates", {}).values()]
    proposal = Proposal(
        target="model", operation="switch_model", evidence=runs,
        hypothesis=(f"dx learn: in {rec.evidence.get('task_class')}, {best} has a credibly lower "
                    f"pxx failure rate than {', '.join(sorted(rec.evidence.get('dominates', {})))}"),
        expected_movement=f"lower failure rate for {rec.evidence.get('task_class')} runs",
        risk="medium",                       # pxx classifies a model switch as MEDIUM
        confidence=min(0.9, min(p_values) if p_values else 0.0),
        root_cause=str(RootCause.MODEL_CAPABILITY),
        reason_prompt_change_is_insufficient=(
            "the same tasks succeed more often on another model; the difference is the model"))

    slug = proposal_slug(f"{proposal.target}:{proposal.operation}")
    lock_path = state_dir / "cycle.lock"
    state_dir.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ForwardError("a pxx improvement cycle holds cycle.lock; try again later") from exc
        try:
            decided = human_disposition(state_dir, slug)
            if decided is not None:
                return slug, f"not forwarded: a human already dispositioned {slug} in pxx ({decided})"
            dest = state_dir / "inbox" / INBOX_HUMAN / f"{slug}.json"
            if dest.exists():
                return slug, f"not forwarded: {slug} is already pending in pxx's inbox"
            dest.parent.mkdir(parents=True, exist_ok=True)
            payload = proposal.to_dict()
            payload["reason"] = f"forwarded by dx learn (recommendation {rec.rec_key})"
            with dest.open("x", encoding="utf-8") as fh:     # never clobber
                fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return slug, f"forwarded to {dest}"
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def tier_floor(targets: tuple[str, ...]) -> str:
    """The least approval a change to ``targets`` can need. The merge gate
    decides the real tier from the real diff; this only warns early."""
    if not targets:
        return "unknown until the diff exists — `dx tier` decides at merge"
    kinds = {classify(t) for t in targets}
    if CONTROL_PLANE in kinds:
        return "control-plane: not signable; a human commits it (REDLINE if merged by dx)"
    if "exec-surface" in kinds:
        return "two-human: touches the exec surface"
    return "per the scope declaration — `dx tier` decides at merge"


def render_spec(rec: Recommendation, task_id: str, *, accepted_by: str) -> str:
    targets = ", ".join(f"`{t}`" for t in rec.targets) or "<fill in before dx run>"
    evidence = json.dumps(rec.evidence, indent=2, sort_keys=True, default=str)
    return f"""# {task_id} — {rec.subject}

<!-- Generated by `dx learn accept` from recommendation {rec.rec_key} ({rec.kind}).
     Accepted by {accepted_by}. dx learn proposed this; it did not author the
     change and may not review it. Fill the <...> fields before `dx run`. -->

- **Role:** `{rec.suggested_role}`   <!-- suggested; pick per USAGE.md § Pick a role -->
- **Scope:** `<path>`
- **Base commit:** `<sha or "current HEAD">`

## 1. Objective

{rec.draft_text}

## 2. Non-goals

- Do not loosen any gate, budget, review requirement, risk tier or freshness window to make
  the metric move. A metric moved that way is a failure of this task, not a success.
- Do not edit the ledger, approvals, keys, role cards or harness (control plane, RL-008).
- Do not change behaviour unrelated to the evidence below.

## 3. Allowed and prohibited paths

- **Allowed:** {targets}
- **Prohibited:** everything else. If the work needs another path, stop and revise this spec.
- **Approval tier floor:** {tier_floor(rec.targets)}

## 4. Constraints and invariants

- Fail closed on bad input. Keep existing public signatures unless the objective requires it.

## 5. Acceptance criteria and required tests

- The fix is covered by a test that fails before the change and passes after.
- Pre-registered success measure (fixed now, judged after merge by `dx learn efficacy`):
  `{rec.metric['name']}` for `{rec.metric['key']}` falls after this task's MERGED row,
  with P >= 0.90 over at least the configured minimum of later observations.
  Link this task first: `dx learn link {rec.rec_key} --task {task_id}`.

## 6. Reviewer

<named human, not the author of the change>

## Evidence (from dx learn, for the implementer)

```json
{evidence}
```
"""
