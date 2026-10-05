"""What pxx's own run record says about the tests and the reviewer — read,
not restated.

`pxx loop` runs the project's ``test_command`` itself between rounds and
emits a ``gate_decision`` event (``gate: "tests"``) each time, with the pass
state, the failing set size, the failures introduced over the round-1
baseline, and — since 2.5.5+ps2 — whether that run was sandboxed. Those
events, and the run's ``outcome.json``, are the only test facts dx will put
in a bundle. Nothing the model *said* about tests is consulted: on
2026-09-22 an executor reported "10 passed" from a hand-picked subset of a
suite that failed 4 of 16, and the review surface had nothing else to show.

The reviewer leg is read the same way. When dx asked for it (``--review``),
pxx's Guard 4 emits a ``gate_decision`` event (``gate: "review"``) per
review with the verdict, the finding count and the mode, and a
``review_stale`` event when the commit moved under the reviewer;
``outcome.json`` carries ``review_seconds``, ``unparseable_review_count``
and the ``REVIEW_*`` contributing codes. "Requested but did not run" is its
own explicit shape, never inferred from ``review_seconds == 0`` — zero is
also what a review that failed instantly reports.

Everything here is best-effort and read-only: a run directory that is
missing, partial or malformed yields ``None`` facts, never an exception the
caller has to survive.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: outcome.json fields copied verbatim when present.
_OUTCOME_KEYS = (
    "code", "rounds", "baseline_failures", "terminal_failures",
    "introduced_failures", "test_seconds",
)


#: outcome.json fields copied into the review fact when present.
_REVIEW_OUTCOME_KEYS = ("review_seconds", "unparseable_review_count")


@dataclass(frozen=True)
class RunFacts:
    """``tests`` is None when the run recorded no test gate at all.
    ``review`` is None only when the caller did not say whether a review was
    requested; otherwise it is one of the three explicit shapes
    (ran / requested-not-ran / not-requested) that :func:`review_fact` builds."""

    tests: dict[str, Any] | None
    #: text artifacts to add to the bundle (relative name -> content)
    artifacts: dict[str, str] = field(default_factory=dict)
    review: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewRequest:
    """What dx sent pxx about the reviewer — from the command it built, not
    from the manifest re-read after the run."""

    requested: bool
    mode: str | None = None
    #: why it was not requested ("manifest review: off" / "no review section")
    why_not: str | None = None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _gate_events(events_path: Path, *gates: str) -> list[tuple[str, dict[str, Any]]]:
    """``(raw line, data)`` for every ``gate_decision`` event whose gate is
    one of ``gates``, in record order. Malformed lines are skipped."""
    found: list[tuple[str, dict[str, Any]]] = []
    try:
        lines = events_path.read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        data = ev.get("data") if isinstance(ev, dict) else None
        if ev.get("kind") == "gate_decision" and isinstance(data, dict) \
                and data.get("gate") in gates:
            found.append((line, data))
    return found


def _tests_gates(events_path: Path) -> list[dict[str, Any]]:
    return [data for _line, data in _gate_events(events_path, "tests")]


def review_fact(run_dir: Path | None, request: ReviewRequest) -> tuple[dict[str, Any], str | None]:
    """The bundle's ``result.review`` and, when a review ran, the gate events
    verbatim (one JSON line each) for ``artifacts/pxx-review-gates.jsonl``.

    Three shapes, each explicit:

    * not requested: ``{"requested": false, "why": …}``
    * requested, ran: the last ``gate: "review"`` event's verdict, findings
      and ``allowed``; ``runs`` and ``stale_rereviews`` counted; the
      ``REVIEW_*`` legs of ``outcome.json``
    * requested, did not run: ``{"requested": true, "ran": false, "why": …}``
      with the loop's terminal code, because the review gate runs only after
      a round's tests pass and a loop that never got green never reached it
    """
    if not request.requested:
        return {"requested": False, "why": request.why_not or "not requested"}, None
    fact: dict[str, Any] = {"requested": True, "mode": request.mode}
    run_dir = loop_record_for(run_dir) or run_dir
    if run_dir is None or not run_dir.is_dir():
        fact.update(ran=False, why="no run record found for this run")
        return fact, None
    outcome = _read_json(run_dir / "outcome.json") or {}
    events = _gate_events(run_dir / "events.jsonl", "review", "review_stale")
    reviews = [data for _line, data in events if data.get("gate") == "review"]
    if not reviews:
        code = outcome.get("code")
        fact.update(
            ran=False,
            why=(f"no review gate in the run record: the loop ended "
                 f"{code if code else 'without a recorded outcome'} before its "
                 f"review gate (review runs only after a round's tests pass)"),
            run_id=run_dir.name,
        )
        return fact, None
    last = reviews[-1]
    fact.update(
        ran=True,
        runs=len(reviews),
        verdict=last.get("verdict"),
        findings=last.get("findings"),
        allowed=last.get("allowed"),
        stale_rereviews=sum(1 for _l, d in events if d.get("gate") == "review_stale"),
        run_id=run_dir.name,
    )
    for key in _REVIEW_OUTCOME_KEYS:
        if key in outcome:
            fact[key] = outcome[key]
    codes = outcome.get("contributing_codes")
    if isinstance(codes, list):
        fact["contributing"] = [c for c in codes if isinstance(c, str) and c.startswith("REVIEW_")]
    return fact, "\n".join(line for line, _d in events) + "\n"


def loop_record_for(run_dir: Path | None) -> Path | None:
    """The loop's own record among ``run_dir``'s siblings, when there is one.

    `pxx loop` (2.5.5+ps3) writes ``runs/<ts>-loop-<id>/`` beside each
    round's session directory; only that one carries the tests gate and the
    whole-loop diff. A caller that found a run by mtime may hold a round's
    directory instead -- prefer the loop record started no earlier than it.
    """
    if run_dir is None or not run_dir.is_dir():
        return None
    try:
        task = _read_json(run_dir / "task.json") or {}
        if task.get("mode") == "loop":
            return run_dir
        started = run_dir.stat().st_mtime
        loops = [
            d for d in run_dir.parent.iterdir()
            if d.is_dir() and "-loop-" in d.name
            and (_read_json(d / "task.json") or {}).get("mode") == "loop"
            and d.stat().st_mtime >= started - 1.0
        ]
    except OSError:
        return None
    return max(loops, key=lambda d: d.stat().st_mtime) if loops else None


def collect(run_dir: Path | None, review: ReviewRequest | None = None) -> RunFacts:
    """Facts from one pxx run directory (``<state_dir>/runs/<id>/``).

    ``review`` is what dx asked pxx for; when given, the review fact is built
    even for a missing run directory (then "requested, did not run")."""
    review_fact_: dict[str, Any] | None = None
    review_lines: str | None = None
    if review is not None:
        review_fact_, review_lines = review_fact(run_dir, review)
    run_dir = loop_record_for(run_dir) or run_dir
    if run_dir is None or not run_dir.is_dir():
        return RunFacts(tests=None, review=review_fact_)
    artifacts: dict[str, str] = {}
    if review_lines:
        artifacts["pxx-review-gates.jsonl"] = review_lines
    outcome = _read_json(run_dir / "outcome.json")
    if outcome is not None:
        artifacts["pxx-outcome.json"] = json.dumps(outcome, indent=2, sort_keys=True) + "\n"
    try:
        patch = (run_dir / "diff.patch").read_text()
    except OSError:
        patch = ""
    if patch.strip():
        # pxx writes this BEFORE its safety net resets the tree, so it is the
        # run's work even when the scope no longer shows it (see dx.salvage).
        artifacts["run-diff.patch"] = patch
    gates = _tests_gates(run_dir / "events.jsonl")
    if not gates:
        return RunFacts(tests=None, artifacts=artifacts, review=review_fact_)
    last = gates[-1]
    tests: dict[str, Any] = {
        "runs": len(gates),
        "passed": bool(last.get("passed")),
        "failing": last.get("failing"),
        "new_failures": list(last.get("new_failures") or []),
        # Absent on pxx < 2.5.5+ps2, which never confined its own test run;
        # None then, never assumed True.
        "sandboxed": last.get("sandboxed"),
        "run_id": run_dir.name,
    }
    if outcome is not None:
        tests.update({k: outcome.get(k) for k in _OUTCOME_KEYS if k in outcome})
        if "test_command" in outcome:
            tests["command"] = outcome["test_command"]
    return RunFacts(tests=tests, artifacts=artifacts, review=review_fact_)
