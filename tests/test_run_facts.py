"""dx.run_facts: the bundle's test facts come from pxx's run record only."""
import json

from dx.run_facts import collect


def _run_dir(tmp_path, events=None, outcome=None, patch=None):
    d = tmp_path / "20260922T170039Z-abc"
    d.mkdir()
    if events is not None:
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if outcome is not None:
        (d / "outcome.json").write_text(json.dumps(outcome))
    if patch is not None:
        (d / "diff.patch").write_text(patch)
    return d


def _gate(**data):
    return {"kind": "gate_decision", "data": {"gate": "tests", **data}}


def test_missing_run_dir_yields_no_facts(tmp_path):
    assert collect(None).tests is None
    assert collect(tmp_path / "nope").tests is None


def test_a_run_with_no_test_gate_records_none(tmp_path):
    """`pxx edit` never runs tests: its record has no tests gate. That is
    None -- never 'passed', never inferred from the exit code."""
    d = _run_dir(tmp_path, events=[{"kind": "session_end", "data": {"code": "COMPLETED"}}],
                 outcome={"code": "COMPLETED", "rounds": 30})
    facts = collect(d)
    assert facts.tests is None
    assert "pxx-outcome.json" in facts.artifacts


def test_last_tests_gate_wins_and_outcome_legs_are_copied(tmp_path):
    d = _run_dir(
        tmp_path,
        events=[
            _gate(round=1, allowed=True, passed=False, failing=4, new_failures=[], sandboxed=True),
            _gate(round=2, allowed=True, passed=False, failing=2, new_failures=[], sandboxed=True),
            _gate(round=3, allowed=True, passed=True, failing=0, new_failures=[], sandboxed=True),
        ],
        outcome={"code": "COMPLETED", "rounds": 3, "baseline_failures": 4,
                 "terminal_failures": 0, "introduced_failures": 0, "test_seconds": 0.4,
                 "summary": "the model's words are not copied"},
        patch="diff --git a/x b/x\n",
    )
    t = collect(d).tests
    assert t["runs"] == 3 and t["passed"] is True and t["failing"] == 0
    assert t["sandboxed"] is True and t["run_id"] == d.name
    assert t["baseline_failures"] == 4 and t["terminal_failures"] == 0
    assert "summary" not in t
    assert collect(d).artifacts["run-diff.patch"].startswith("diff --git")


def test_pre_ps2_gate_without_sandboxed_is_unknown_not_true(tmp_path):
    d = _run_dir(tmp_path, events=[_gate(round=1, allowed=True, passed=True, failing=0)])
    assert collect(d).tests["sandboxed"] is None


def test_malformed_record_is_survived(tmp_path):
    d = _run_dir(tmp_path, events=None)
    (d / "events.jsonl").write_text("{not json\n" + json.dumps(_gate(passed=False, failing=1)) + "\n")
    (d / "outcome.json").write_text("[]")
    t = collect(d).tests
    assert t["runs"] == 1 and t["failing"] == 1 and "code" not in t


def test_a_round_directory_defers_to_the_loop_record_beside_it(tmp_path):
    """dx finds runs by mtime and may land on a round's session directory;
    the loop's own record (task.json mode=loop) is the one with the gates."""
    session = _run_dir(tmp_path, events=[{"kind": "session_end", "data": {"code": "COMPLETED"}}],
                       outcome={"code": "COMPLETED"})
    loop = tmp_path / "20260922T170040Z-loop-abcd1234"
    loop.mkdir()
    (loop / "task.json").write_text(json.dumps({"mode": "loop", "test_command": "pytest -q"}))
    (loop / "events.jsonl").write_text(json.dumps(_gate(passed=False, failing=3, sandboxed=True)) + "\n")
    (loop / "outcome.json").write_text(json.dumps({"mode": "loop", "code": "TEST_REGRESSION",
                                                   "terminal_failures": 3, "test_command": "pytest -q"}))
    (loop / "diff.patch").write_text("diff --git a/t b/t\n")
    facts = collect(session)
    assert facts.tests is not None and facts.tests["failing"] == 3
    assert facts.tests["code"] == "TEST_REGRESSION" and facts.tests["command"] == "pytest -q"
    assert facts.tests["run_id"] == loop.name
    assert "run-diff.patch" in facts.artifacts


# --- what pxx did with the tree at the end (pxx >= 2.9.1 / 2.9.3) ----------

def _net(**data):
    return {"kind": "gate_decision", "data": {"gate": "safety_net", **data}}


def test_settle_reads_a_refused_restore_and_a_skipped_commit(tmp_path):
    from dx.run_facts import settle_fact

    d = _run_dir(tmp_path, events=[
        _net(allowed=False, restored=False, reason="head_moved", tag="pxx-pre/T"),
        {"kind": "observation", "data": {"source": "auto_commit_skipped", "reason": "secrets"}},
    ])
    fact = settle_fact(d)
    assert fact["safety_net"] == {"restored": False, "reason": "head_moved", "tag": "pxx-pre/T"}
    assert fact["commit_skipped"] == "secrets"
    assert json.loads(collect(d).artifacts["pxx-settle.json"]) == fact


def test_settle_is_none_without_either_record(tmp_path):
    from dx.run_facts import settle_fact

    d = _run_dir(tmp_path, events=[_gate(passed=True)])
    assert settle_fact(d) is None and collect(d).settle is None
    assert settle_fact(None) is None


def test_only_an_explicit_true_reads_as_restored(tmp_path):
    from dx.run_facts import settle_fact

    d = _run_dir(tmp_path, events=[_net(allowed=True, restored="yes", reason="x")])
    assert settle_fact(d)["safety_net"]["restored"] is False


def test_the_net_creation_event_is_not_a_restore(tmp_path):
    # Codex review: pxx emits safety_net (allowed, no "restored") when it ties
    # the net; on a COMPLETED run that is the only one, and it is not a failure.
    from dx.run_facts import settle_fact

    d = _run_dir(tmp_path, events=[_net(allowed=True, tag="pxx-pre/T", stash="")])
    assert settle_fact(d) is None
