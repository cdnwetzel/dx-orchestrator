"""The reviewer leg (WP-4): the manifest decides, dx always says so to pxx,
and the bundle records what the run record says — ran, requested-but-did-not-
run, or not requested — never what anyone said.

Design: AskPS `docs/general/ps-coding-wp4-design.md` (Kimi design review
2026-10-05, PASS-with-amendments). D-4: advisory only; `blocking` is refused.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import MANIFEST
from dx.cli import build_parser
from dx.config_loader import ConfigError, ReviewConfig, get_review_config, validate_manifest
from dx.evidence import RoleTaskBundle, render_review_line, write_bundle
from dx.run_facts import ReviewRequest, collect, review_fact

# --- the manifest section -----------------------------------------------------


def _manifest_with(tmp_path: Path, monkeypatch, review_block: str | None) -> Path:
    out = tmp_path / "manifest.yml"
    out.write_text(MANIFEST.read_text(encoding="utf-8")
                   + (f"\nreview:\n{review_block}\n" if review_block is not None else ""),
                   encoding="utf-8")
    monkeypatch.setenv("DX_CONFIG", str(out))
    return out


class TestReviewConfig:
    def test_absent_section_is_none(self, tmp_path, monkeypatch):
        _manifest_with(tmp_path, monkeypatch, None)
        assert get_review_config() is None

    def test_enabled_advisory_resolves(self, tmp_path, monkeypatch):
        _manifest_with(tmp_path, monkeypatch, "  enabled: true\n  mode: advisory")
        assert get_review_config() == ReviewConfig(enabled=True, mode="advisory")

    def test_mode_defaults_to_advisory(self, tmp_path, monkeypatch):
        _manifest_with(tmp_path, monkeypatch, "  enabled: false")
        assert get_review_config() == ReviewConfig(enabled=False, mode="advisory")

    def test_enabled_must_be_a_real_boolean(self, tmp_path, monkeypatch):
        """A typo must not become 'review off' silently."""
        _manifest_with(tmp_path, monkeypatch, "  enabled: 'yes'")
        with pytest.raises(ConfigError, match="review.enabled"):
            get_review_config()

    def test_an_unknown_mode_is_refused(self, tmp_path, monkeypatch):
        _manifest_with(tmp_path, monkeypatch, "  enabled: true\n  mode: strict")
        with pytest.raises(ConfigError, match="review.mode"):
            get_review_config()

    def test_blocking_is_refused_naming_d4(self, tmp_path, monkeypatch):
        """pxx supports it; dx does not offer it until the calibration run."""
        _manifest_with(tmp_path, monkeypatch, "  enabled: true\n  mode: blocking")
        with pytest.raises(ConfigError, match="D-4"):
            get_review_config()

    def test_a_list_where_a_mapping_belongs_is_refused(self, tmp_path, monkeypatch):
        _manifest_with(tmp_path, monkeypatch, "  - enabled")
        with pytest.raises(ConfigError, match="review"):
            get_review_config()

    @pytest.mark.parametrize("block", ["  enabled: 'yes'", "  enabled: true\n  mode: blocking"])
    def test_validate_manifest_catches_a_bad_section(self, tmp_path, monkeypatch, block):
        _manifest_with(tmp_path, monkeypatch, block)
        with pytest.raises(ConfigError):
            validate_manifest()


# --- the command: both flags or --no-review, never a mix ---------------------


def _run(*argv):
    args = build_parser().parse_args(["run", *argv])
    args.func(args)


def _run_to_completion(*argv):
    """A run that may or may not sys.exit (a zero pxx exit with no git scope
    returns normally); what is under test is the bundle it leaves behind."""
    try:
        _run(*argv)
    except SystemExit:
        pass


@pytest.fixture(autouse=True)
def _fake_pxx(monkeypatch):
    monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/bin/pxx")
    # The reviewer claim imports pxx; these tests are about dx's own decisions.
    monkeypatch.setattr("dx.cmd_run._reviewer_route", lambda scope, env: None)


def _captured_cmd(monkeypatch, tmp_path, review_block: str | None) -> list[str]:
    _manifest_with(tmp_path, monkeypatch, review_block)
    seen: dict[str, list[str]] = {}

    class _R:
        returncode = 0

    monkeypatch.setattr("dx.cmd_run.subprocess.run",
                        lambda cmd, env=None, **k: (seen.update(cmd=cmd), _R())[1])
    _run("T-RV", "--required_role", "widget-engineer", "-m", "x", "--no-evidence",
         "--scope", str(tmp_path))
    return seen["cmd"]


class TestReviewFlags:
    def test_review_on_sends_both_flags_adjacent(self, monkeypatch, tmp_path):
        cmd = _captured_cmd(monkeypatch, tmp_path, "  enabled: true\n  mode: advisory")
        i = cmd.index("--review")
        assert cmd[i + 1 : i + 3] == ["--review-mode", "advisory"]
        assert "--no-review" not in cmd
        assert "--sandbox" in cmd and "--commit" in cmd

    def test_review_off_sends_exactly_no_review(self, monkeypatch, tmp_path):
        cmd = _captured_cmd(monkeypatch, tmp_path, "  enabled: false")
        assert cmd.count("--no-review") == 1
        assert "--review" not in cmd and "--review-mode" not in cmd

    def test_absent_section_sends_exactly_no_review(self, monkeypatch, tmp_path):
        cmd = _captured_cmd(monkeypatch, tmp_path, None)
        assert cmd.count("--no-review") == 1
        assert "--review-mode" not in cmd

    def test_a_flag_is_sent_on_every_run(self, monkeypatch, tmp_path):
        """pxx's loop_review default never decides."""
        for block in (None, "  enabled: false", "  enabled: true"):
            cmd = _captured_cmd(monkeypatch, tmp_path, block)
            assert ("--review" in cmd) != ("--no-review" in cmd)

    def test_a_bad_section_stops_the_run_before_pxx(self, monkeypatch, tmp_path, capsys):
        _manifest_with(tmp_path, monkeypatch, "  enabled: true\n  mode: blocking")
        called = []
        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: called.append(a))
        with pytest.raises(SystemExit) as exc:
            _run("T-RV", "--required_role", "widget-engineer", "-m", "x", "--no-evidence")
        assert exc.value.code == 1 and not called
        assert "D-4" in capsys.readouterr().err

    def test_dry_run_prints_the_review_flags(self, monkeypatch, tmp_path, capsys):
        _manifest_with(tmp_path, monkeypatch, "  enabled: true\n  mode: advisory")
        _run("T-RV", "--required_role", "widget-engineer", "-m", "x", "--dry-run")
        out = capsys.readouterr().out
        assert "--review --review-mode advisory" in out

    def test_dry_run_says_why_the_reviewer_is_off(self, monkeypatch, tmp_path, capsys):
        _manifest_with(tmp_path, monkeypatch, None)
        _run("T-RV", "--required_role", "widget-engineer", "-m", "x", "--dry-run")
        out = capsys.readouterr().out
        assert "--no-review" in out and "no review section" in out

    def test_the_section_is_read_once_per_run(self, monkeypatch, tmp_path):
        _manifest_with(tmp_path, monkeypatch, "  enabled: true")
        reads: list[int] = []
        real = get_review_config

        def counting():
            reads.append(1)
            return real()

        monkeypatch.setattr("dx.cmd_run.get_review_config", counting)

        class _R:
            returncode = 0

        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: _R())
        monkeypatch.setattr("dx.cmd_run._git", lambda *a, **k: None)
        _run_to_completion("T-RV", "--required_role", "widget-engineer", "-m", "x", "--no-commit",
                           "--evidence-dir", str(tmp_path / "ev"))
        assert len(reads) == 1


# --- the record: from the run record only ------------------------------------


def _run_dir(tmp_path: Path, events=None, outcome=None) -> Path:
    d = tmp_path / "20261005T150000Z-loop-rv01"
    d.mkdir()
    (d / "task.json").write_text(json.dumps({"mode": "loop"}))
    if events is not None:
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if outcome is not None:
        (d / "outcome.json").write_text(json.dumps(outcome))
    return d


def _gate(gate: str, **data):
    return {"kind": "gate_decision", "data": {"gate": gate, **data}}


ADVISORY = ReviewRequest(requested=True, mode="advisory")


class TestReviewFact:
    def test_not_requested_says_why(self, tmp_path):
        fact, lines = review_fact(None, ReviewRequest(requested=False, why_not="manifest review: off"))
        assert fact == {"requested": False, "why": "manifest review: off"} and lines is None

    def test_approve(self, tmp_path):
        d = _run_dir(tmp_path,
                     events=[_gate("tests", round=1, passed=True, failing=0),
                             _gate("review", round=1, allowed=True, verdict="APPROVE",
                                   mode="advisory", findings=0)],
                     outcome={"code": "COMPLETED", "review_seconds": 41.2,
                              "unparseable_review_count": 0, "contributing_codes": []})
        fact, lines = review_fact(d, ADVISORY)
        assert fact["requested"] and fact["mode"] == "advisory" and fact["ran"] is True
        assert fact["runs"] == 1 and fact["verdict"] == "APPROVE" and fact["findings"] == 0
        assert fact["allowed"] is True and fact["review_seconds"] == 41.2
        assert fact["contributing"] == [] and fact["run_id"] == d.name
        assert lines is not None and lines.count("\n") == 1 and '"review"' in lines

    def test_revise_then_approve_counts_runs_and_the_rejection(self, tmp_path):
        d = _run_dir(tmp_path,
                     events=[_gate("review", round=1, allowed=False, verdict="REVISE",
                                   mode="advisory", findings=2),
                             _gate("review", round=2, allowed=True, verdict="APPROVE",
                                   mode="advisory", findings=0)],
                     outcome={"code": "COMPLETED", "review_seconds": 80.0,
                              "contributing_codes": ["REVIEW_REJECTED"]})
        fact, _ = review_fact(d, ADVISORY)
        assert fact["runs"] == 2 and fact["verdict"] == "APPROVE"
        assert fact["contributing"] == ["REVIEW_REJECTED"]

    def test_unavailable_reviewer_in_advisory_is_recorded_not_a_pass(self, tmp_path):
        d = _run_dir(tmp_path,
                     events=[_gate("review", round=1, allowed=True, verdict="NO_REVIEW",
                                   mode="advisory", findings=0)],
                     outcome={"code": "COMPLETED", "review_seconds": 0.4,
                              "contributing_codes": ["REVIEW_UNAVAILABLE", "OTHER"]})
        fact, _ = review_fact(d, ADVISORY)
        assert fact["ran"] is True and fact["verdict"] == "NO_REVIEW" and fact["allowed"] is True
        assert fact["contributing"] == ["REVIEW_UNAVAILABLE"] and fact["review_seconds"] > 0

    def test_unparseable_count_and_stale_rereviews_are_copied(self, tmp_path):
        d = _run_dir(tmp_path,
                     events=[_gate("review_stale", round=1, allowed=False,
                                   reviewed_head="a", current_head="b"),
                             _gate("review", round=1, allowed=True, verdict="APPROVE",
                                   mode="advisory", findings=0)],
                     outcome={"code": "COMPLETED", "unparseable_review_count": 1})
        fact, lines = review_fact(d, ADVISORY)
        assert fact["stale_rereviews"] == 1 and fact["unparseable_review_count"] == 1
        assert lines is not None and lines.count("\n") == 2

    def test_requested_but_the_loop_never_reached_the_gate(self, tmp_path):
        """Explicit, with the terminal code; never inferred from review_seconds == 0."""
        d = _run_dir(tmp_path,
                     events=[_gate("tests", round=1, passed=False, failing=3)],
                     outcome={"code": "ROUND_CAP", "review_seconds": 0.0})
        fact, lines = review_fact(d, ADVISORY)
        assert fact["requested"] is True and fact["ran"] is False
        assert "ROUND_CAP" in fact["why"] and "before its review gate" in fact["why"]
        assert "review_seconds" not in fact and lines is None

    def test_requested_with_no_run_record_at_all(self, tmp_path):
        fact, _ = review_fact(tmp_path / "nope", ADVISORY)
        assert fact["ran"] is False and "no run record" in fact["why"]

    def test_a_run_record_without_outcome_json_still_reads_the_gate(self, tmp_path):
        d = _run_dir(tmp_path, events=[_gate("review", round=1, allowed=True,
                                              verdict="APPROVE", mode="advisory", findings=0)])
        fact, _ = review_fact(d, ADVISORY)
        assert fact["ran"] is True and fact["verdict"] == "APPROVE" and "review_seconds" not in fact

    def test_collect_carries_the_fact_and_the_gate_artifact(self, tmp_path):
        d = _run_dir(tmp_path,
                     events=[_gate("review", round=1, allowed=True, verdict="APPROVE",
                                   mode="advisory", findings=0)],
                     outcome={"code": "COMPLETED"})
        facts = collect(d, ADVISORY)
        assert facts.review is not None and facts.review["ran"] is True
        assert "pxx-review-gates.jsonl" in facts.artifacts

    def test_collect_without_a_request_says_nothing_about_review(self, tmp_path):
        d = _run_dir(tmp_path, events=[], outcome={"code": "COMPLETED"})
        assert collect(d).review is None


# --- the bundle: field, check and the one line -------------------------------


def _bundle(**kw) -> RoleTaskBundle:
    base = dict(task_id="T-RV", title="T-RV — widget-engineer", passed=True, role="widget-engineer",
                artifacts={"command.txt": "pxx loop --review\n"})
    base.update(kw)
    return RoleTaskBundle(**base)


class TestBundleReview:
    def test_the_manifest_carries_result_review(self, tmp_path):
        out = write_bundle(_bundle(review={"requested": False, "why": "no review section"}), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["result"]["review"] == {"requested": False, "why": "no review section"}

    def test_an_older_caller_leaves_review_null(self, tmp_path):
        out = write_bundle(_bundle(), tmp_path)
        m = json.loads((out / "manifest.json").read_text())
        assert m["result"]["review"] is None
        assert "## Review" not in (out / "README.md").read_text()

    def test_the_readme_states_did_not_run(self, tmp_path):
        fact = {"requested": True, "mode": "advisory", "ran": False,
                "why": "no review gate in the run record: the loop ended ROUND_CAP before its review gate"}
        out = write_bundle(_bundle(review=fact), tmp_path)
        readme = (out / "README.md").read_text()
        assert "DID NOT RUN" in readme and "ROUND_CAP" in readme

    def test_the_line_for_a_run_that_ran(self):
        line = render_review_line({"requested": True, "mode": "advisory", "ran": True, "runs": 2,
                                   "verdict": "APPROVE", "findings": 0, "review_seconds": 41.2,
                                   "contributing": ["REVIEW_REJECTED"], "stale_rereviews": 1,
                                   "run_id": "r1"})
        assert "ran 2x" in line and "APPROVE" in line and "41.2s" in line
        assert "REVIEW_REJECTED" in line and "re-review" in line and "never a gate" in line

    def test_the_line_for_not_requested(self):
        assert "not requested" in render_review_line({"requested": False, "why": "manifest review: off"})


class TestRunEmitsTheReviewRecord:
    def _run_with_evidence(self, monkeypatch, tmp_path, review_block, run_dir):
        _manifest_with(tmp_path, monkeypatch, review_block)
        monkeypatch.setattr("dx.cmd_run.find_run_dir", lambda started: run_dir)
        monkeypatch.setattr("dx.cmd_run._git", lambda *a, **k: None)

        class _R:
            returncode = 0

        monkeypatch.setattr("dx.cmd_run.subprocess.run", lambda *a, **k: _R())
        ev = tmp_path / "ev"
        _run_to_completion("T-RV", "--required_role", "widget-engineer", "-m", "x", "--no-commit",
                           "--evidence-dir", str(ev))
        bundle = next(ev.glob("T-RV/*/manifest.json"))
        return json.loads(bundle.read_text()), bundle.parent

    def test_requested_and_ran_is_recorded_with_the_gate_artifact(self, monkeypatch, tmp_path):
        d = _run_dir(tmp_path, events=[_gate("review", round=1, allowed=True, verdict="APPROVE",
                                              mode="advisory", findings=0)],
                     outcome={"code": "COMPLETED", "review_seconds": 3.0})
        m, out = self._run_with_evidence(monkeypatch, tmp_path, "  enabled: true", d)
        assert m["result"]["review"]["ran"] is True
        assert m["checks"]["review_recorded"]["ok"] is True
        assert (out / "artifacts" / "pxx-review-gates.jsonl").exists()
        routing = json.loads((out / "artifacts" / "routing.json").read_text())
        assert "reviewer_configured" in routing  # None here: the seam is stubbed

    def test_requested_not_ran_fails_the_check_but_not_passed(self, monkeypatch, tmp_path):
        d = _run_dir(tmp_path, events=[_gate("tests", round=1, passed=False, failing=1)],
                     outcome={"code": "ROUND_CAP"})
        m, _ = self._run_with_evidence(monkeypatch, tmp_path, "  enabled: true", d)
        assert m["result"]["review"]["ran"] is False
        assert m["checks"]["review_recorded"]["ok"] is False
        assert "ROUND_CAP" in m["checks"]["review_recorded"]["detail"]
        assert m["result"]["passed"] is True  # pxx's exit, not the reviewer's

    def test_not_requested_is_ok_and_says_so(self, monkeypatch, tmp_path):
        m, _ = self._run_with_evidence(monkeypatch, tmp_path, None, None)
        assert m["result"]["review"] == {"requested": False, "why": "no review section"}
        assert m["checks"]["review_recorded"]["ok"] is True


# --- doctor: the posture, derived --------------------------------------------


class _Ok:
    returncode = 0
    stdout = b""
    stderr = b""


@pytest.fixture
def doctor_green(monkeypatch, tmp_path):
    """Every core check passes; the reviewer seam answers what the test says."""
    monkeypatch.setattr("dx.cmd_doctor._find_on_path", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr("dx.cmd_doctor.subprocess.run", lambda *a, **k: _Ok())
    monkeypatch.setattr("dx.cmd_doctor._check_import", lambda module, label: True)
    psop = tmp_path / "psoperator" / "examples"
    psop.mkdir(parents=True)
    (psop / "run_agent.py").touch()
    monkeypatch.setenv("PSOPERATOR_REPO", str(tmp_path / "psoperator"))
    ledger = tmp_path / "ledger" / "tools"
    ledger.mkdir(parents=True)
    (ledger / "verify_chain.py").touch()
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "ledger"))
    monkeypatch.setenv("DX_EVIDENCE_DIR", str(tmp_path / "evidence"))
    return tmp_path


def _doctor(capsys) -> tuple[int, str]:
    args = build_parser().parse_args(["doctor", "--no-network"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    return exc.value.code, capsys.readouterr().out


class TestDoctorPosture:
    def test_off_when_the_section_is_absent_names_f001(self, doctor_green, monkeypatch, capsys):
        _manifest_with(doctor_green, monkeypatch, None)
        monkeypatch.setattr("dx.cmd_run._reviewer_route", lambda scope, env: None)
        code, out = _doctor(capsys)
        assert code == 0
        assert "reviewer leg: off (manifest has no review section)" in out and "F-001" in out

    def test_on_advisory(self, doctor_green, monkeypatch, capsys):
        _manifest_with(doctor_green, monkeypatch, "  enabled: true\n  mode: advisory")
        monkeypatch.setattr("dx.cmd_run._reviewer_route",
                            lambda scope, env: {"model": "rev-7b", "provider": "ollama",
                                                "base_url": "http://rev.invalid:11434"})
        code, out = _doctor(capsys)
        assert code == 0 and "reviewer leg: on, advisory" in out
        assert "✅ reviewer rev-7b @ http://rev.invalid:11434" in out
        assert "differs from writer test-27b" in out

    def test_reviewer_equal_to_writer_is_red_but_not_core(self, doctor_green, monkeypatch, capsys):
        _manifest_with(doctor_green, monkeypatch, "  enabled: true")
        monkeypatch.setattr("dx.cmd_run._reviewer_route",
                            lambda scope, env: {"model": env.get("PXX_MODEL"), "provider": "vllm",
                                                "base_url": env.get("PXX_BASE_URL")})
        code, out = _doctor(capsys)
        assert code == 0  # posture is a report, not a core check
        assert "❌ reviewer is the writer's model (test-27b)" in out and "decorrelation broken" in out

    def test_undetermined_is_a_warning_never_green(self, doctor_green, monkeypatch, capsys):
        _manifest_with(doctor_green, monkeypatch, "  enabled: true")
        monkeypatch.setattr("dx.cmd_run._reviewer_route", lambda scope, env: None)
        code, out = _doctor(capsys)
        assert "⚠️  reviewer not determined" in out
        assert "✅ reviewer" not in out

    def test_the_comparison_is_printed_with_the_reviewer_off(self, doctor_green, monkeypatch, capsys):
        _manifest_with(doctor_green, monkeypatch, "  enabled: false")
        monkeypatch.setattr("dx.cmd_run._reviewer_route",
                            lambda scope, env: {"model": "rev-7b", "provider": "ollama",
                                                "base_url": None})
        _, out = _doctor(capsys)
        assert "reviewer leg: off (manifest review: enabled false)" in out
        assert "✅ reviewer rev-7b" in out

    def test_the_reviewer_is_resolved_under_each_writers_environment(self, doctor_green, monkeypatch, capsys):
        _manifest_with(doctor_green, monkeypatch, "  enabled: true")
        seen: list[tuple[str | None, str | None]] = []

        def seam(scope, env):
            seen.append((env.get("PXX_MODEL"), env.get("PXX_BASE_URL")))
            return {"model": "rev", "provider": None, "base_url": None}

        monkeypatch.setattr("dx.cmd_run._reviewer_route", seam)
        _doctor(capsys)
        assert ("test-27b", "http://vllm.invalid:8007") in seen
        assert ("test-14b", "http://ollama.invalid:11434") in seen

    def test_a_bad_section_is_a_core_failure_before_the_posture(self, doctor_green, monkeypatch, capsys):
        _manifest_with(doctor_green, monkeypatch, "  enabled: true\n  mode: blocking")
        code, out = _doctor(capsys)
        assert code == 1 and "D-4" in out


# --- the reviewer claim: what the seam returns and what it never does --------


class TestReviewerRoute:
    def test_not_importable_is_none(self, monkeypatch, tmp_path):
        import builtins

        from dx.cmd_run import _reviewer_route

        real_import = builtins.__import__

        def no_pxx(name, *a, **k):
            if name.startswith("pxx"):
                raise ImportError(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_pxx)
        assert _reviewer_route(tmp_path, {}) is None

    def test_the_environment_is_restored_after_the_claim(self, monkeypatch, tmp_path):
        import os

        from dx.cmd_run import _reviewer_route

        monkeypatch.setenv("DX_REVIEW_LEG_TEST_SENTINEL", "kept")
        _reviewer_route(tmp_path, {"PXX_MODEL": "x"})
        assert os.environ.get("DX_REVIEW_LEG_TEST_SENTINEL") == "kept"
        assert "PXX_MODEL" not in os.environ or os.environ["PXX_MODEL"] != "x"


# --- the generator carries the section from the template ---------------------


class TestGeneratorCarriesReview:
    def test_review_passes_through_from_the_template(self):
        import importlib.util

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("gen_manifest", root / "scripts" / "gen_manifest.py")
        assert spec and spec.loader
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        template = {"default_tier": "DEFAULT", "roles": {"a": "DEFAULT"},
                    "review": {"enabled": True, "mode": "advisory"}}
        binding = {"version": 1, "router": {"url": "http://r.invalid:1", "provider": "openai-compatible"},
                   "tiers": {"DEFAULT": {"model": "m"}}}
        assert gen.generate(template, binding)["review"] == {"enabled": True, "mode": "advisory"}
        del template["review"]
        assert "review" not in gen.generate(template, binding)
        template["review"] = ["enabled"]
        with pytest.raises(gen.BindingError, match="review"):
            gen.generate(template, binding)
