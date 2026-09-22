"""What pxx's own run record says about the tests — read, not restated.

`pxx loop` runs the project's ``test_command`` itself between rounds and
emits a ``gate_decision`` event (``gate: "tests"``) each time, with the pass
state, the failing set size, the failures introduced over the round-1
baseline, and — since 2.5.5+ps2 — whether that run was sandboxed. Those
events, and the run's ``outcome.json``, are the only test facts dx will put
in a bundle. Nothing the model *said* about tests is consulted: on
2026-09-22 an executor reported "10 passed" from a hand-picked subset of a
suite that failed 4 of 16, and the review surface had nothing else to show.

Everything here is best-effort and read-only: a run directory that is
missing, partial or malformed yields ``None`` facts, never an exception the
caller has to survive.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: outcome.json fields copied verbatim when present.
_OUTCOME_KEYS = (
    "code", "rounds", "baseline_failures", "terminal_failures",
    "introduced_failures", "test_seconds",
)


@dataclass(frozen=True)
class RunFacts:
    """``tests`` is None when the run recorded no test gate at all."""

    tests: dict | None
    #: text artifacts to add to the bundle (relative name -> content)
    artifacts: dict[str, str] = field(default_factory=dict)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tests_gates(events_path: Path) -> list[dict]:
    gates: list[dict] = []
    try:
        lines = events_path.read_text().splitlines()
    except OSError:
        return gates
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        data = ev.get("data") if isinstance(ev, dict) else None
        if ev.get("kind") == "gate_decision" and isinstance(data, dict) \
                and data.get("gate") == "tests":
            gates.append(data)
    return gates


def collect(run_dir: Path | None) -> RunFacts:
    """Facts from one pxx run directory (``<state_dir>/runs/<id>/``)."""
    if run_dir is None or not run_dir.is_dir():
        return RunFacts(tests=None)
    artifacts: dict[str, str] = {}
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
        return RunFacts(tests=None, artifacts=artifacts)
    last = gates[-1]
    tests: dict = {
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
    return RunFacts(tests=tests, artifacts=artifacts)
