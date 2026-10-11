"""Where an accepted or forwarded recommendation goes next.

Two exits, both into machinery that already has human gates:

* pxx's improvement inbox — a model-routing recommendation is something pxx's
  own improvement plane can carry (target ``model``, operation
  ``switch_model``). This module holds only the pxx-side primitives: build the
  proposal, look at the inbox, create an entry without clobbering, withdraw an
  entry that is still ours. The coordination — when to forward, the durable
  intent/outcome records, recovery and divergence — is ``learn_forward``.
* ``render_spec`` — every accepted recommendation becomes a dx task spec
  (``templates/spec.md`` shape). The change itself is then made by ``dx run``,
  reviewed, and merged with a signature like any other task.

pxx's inbox has two writers dx cannot fully serialize with. ``pxx improve
cycle`` takes ``cycle.lock``, and every primitive here takes it too.
``pxx improve triage`` takes no lock: it reads a pending entry, writes the
dispositioned copy, then unlinks the pending file. dx therefore never
overwrites (files are created with ``"x"``), only removes an entry whose
proposal fingerprint is still ours, and re-inspects after each change;
``learn_forward`` records what it sees and surfaces any disagreement.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .approval_tier import CONTROL_PLANE, classify
from .learn_model import Recommendation

FORWARDABLE_KINDS = frozenset({"variant_preference"})

# The fields of pxx's Proposal.to_dict(). Triage rewrites "reason" and adds
# "disposition"; these never change, so they identify an entry as ours.
_PROPOSAL_KEYS = ("target", "operation", "evidence", "hypothesis", "expected_movement", "risk",
                  "confidence", "basis", "root_cause", "reason_prompt_change_is_insufficient")


class ForwardError(Exception):
    pass


class CycleLockBusy(ForwardError):
    pass


def proposal_fingerprint(entry: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({k: entry.get(k) for k in _PROPOSAL_KEYS},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_proposal(rec: Recommendation) -> tuple[str, dict[str, Any], str]:
    """(slug, payload, fingerprint) for ``rec`` in pxx's exact Proposal shape."""
    if rec.kind not in FORWARDABLE_KINDS:
        raise ForwardError(f"{rec.kind} has no pxx improvement target; it stays in dx learn")
    from pxx.improve.cycle import proposal_slug
    from pxx.improve.mining import Proposal, RootCause

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
    payload = proposal.to_dict()
    payload["reason"] = f"forwarded by dx learn (recommendation {rec.rec_key})"
    return (proposal_slug(f"{proposal.target}:{proposal.operation}"), payload,
            proposal_fingerprint(payload))


@contextmanager
def cycle_lock(state_dir: Path) -> Iterator[None]:
    """pxx's own cycle.lock, non-blocking (a running cycle means: try next pass)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "cycle.lock").open("w") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CycleLockBusy("a pxx improvement cycle holds cycle.lock") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _inbox(state_dir: Path, box: str, slug: str) -> Path:
    return state_dir / "inbox" / box / f"{slug}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}          # present but unreadable: treated as someone else's
    return data if isinstance(data, dict) else {}


def inspect(state_dir: Path, slug: str, fingerprint: str) -> str:
    """What pxx's inbox holds for ``slug``, relative to our fingerprint.

    pending-ours | pending-other | qualified | rejected | qualified-other |
    rejected-other | absent.

    Our own pending entry is checked first: a decided file under the same slug
    must never hide it (it would then never be withdrawn). Only HUMAN decisions
    count — an entry in qualified/ or rejected/ carrying a ``disposition``
    block, which is pxx's own rule (``cycle.human_disposition``). The cycle's
    auto-rejections carry none and pxx itself re-routes them, so they neither
    block a forward nor count as anyone's decision.
    """
    pending = _read(_inbox(state_dir, "human-review-required", slug))
    if pending is not None and proposal_fingerprint(pending) == fingerprint:
        return "pending-ours"
    for box in ("qualified", "rejected"):
        entry = _read(_inbox(state_dir, box, slug))
        if entry is not None and isinstance(entry.get("disposition"), dict):
            return box if proposal_fingerprint(entry) == fingerprint else f"{box}-other"
    return "absent" if pending is None else "pending-other"


def write_pending(state_dir: Path, slug: str, payload: dict[str, Any], fingerprint: str) -> str:
    """Create our pending entry under cycle.lock. Returns the resulting inspect()
    view; only "pending-ours" means it was written and is still there."""
    with cycle_lock(state_dir):
        before = inspect(state_dir, slug, fingerprint)
        if before != "absent":
            return before
        dest = _inbox(state_dir, "human-review-required", slug)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest.open("x", encoding="utf-8") as fh:     # never clobber
                fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except FileExistsError:
            pass   # someone created it between inspect and open: report what is there
        # triage takes no lock: a human may have acted in this very window, so the
        # answer is what the inbox holds now, not what we meant to do
        return inspect(state_dir, slug, fingerprint)


def withdraw_if_ours(state_dir: Path, slug: str, fingerprint: str) -> str:
    """Remove our own still-pending entry. Returns the view afterwards
    ("absent" when withdrawn). Never touches a dispositioned or foreign entry."""
    with cycle_lock(state_dir):
        if inspect(state_dir, slug, fingerprint) == "pending-ours":
            _inbox(state_dir, "human-review-required", slug).unlink(missing_ok=True)
        return inspect(state_dir, slug, fingerprint)


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
