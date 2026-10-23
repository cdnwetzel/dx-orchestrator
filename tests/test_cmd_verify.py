"""`dx verify-gui` — configuration resolution and the RL-007 boundary.

The VLM answer is advisory evidence. These tests pin that dx never invents a
host to talk to, and that the verifier's verdict is reported, not trusted as a
merge gate (dx merge still requires a GPG signature).
"""
import json
import subprocess
from pathlib import Path

import pytest

from dx.cli import build_parser
from dx.cmd_verify import (
    GuiConfigError,
    Verdict,
    gui_target,
    parse_vlm_reply,
    verify_gui,
)


def test_target_comes_from_the_manifest():
    target = gui_target()
    assert target.vlm_endpoint == "http://vlm.invalid:11434/api/generate"
    assert target.vlm_model == "test-vl:3b"
    assert target.ssh_host == "tester@vlm.invalid"


def test_env_overrides_the_manifest(monkeypatch):
    monkeypatch.setenv("DX_VLM_MODEL", "override-vl:7b")
    monkeypatch.setenv("DX_VLM_ENDPOINT", "http://override.invalid/api/generate")
    monkeypatch.setenv("DX_GUI_SSH_HOST", "someone@override.invalid")
    target = gui_target()
    assert target.vlm_model == "override-vl:7b"
    assert target.vlm_endpoint == "http://override.invalid/api/generate"
    assert target.ssh_host == "someone@override.invalid"


def test_screenshot_cmd_has_a_documented_default(monkeypatch, tmp_path):
    cfg = tmp_path / "m.yml"
    cfg.write_text(
        "gui_verification:\n"
        '  vlm_endpoint: "http://x.invalid/api/generate"\n'
        '  vlm_model: "m"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DX_CONFIG", str(cfg))
    assert gui_target().screenshot_cmd == "import -window root png:-"


def _min_manifest(tmp_path, extra=""):
    cfg = tmp_path / "m.yml"
    cfg.write_text(
        "gui_verification:\n"
        '  vlm_endpoint: "http://x.invalid/api/generate"\n'
        '  vlm_model: "m"\n' + extra,
        encoding="utf-8",
    )
    return cfg


class TestVlmTimeout:
    """A slow box on a cold vision model exceeds the 30 s default and every check
    fails with a read timeout. The wait must be raisable without a code change."""

    def test_default_is_30(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DX_VLM_TIMEOUT", raising=False)
        monkeypatch.setenv("DX_CONFIG", str(_min_manifest(tmp_path)))
        assert gui_target().vlm_timeout_s == 30

    def test_manifest_overrides_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DX_VLM_TIMEOUT", raising=False)
        monkeypatch.setenv("DX_CONFIG", str(_min_manifest(tmp_path, "  timeout_s: 120\n")))
        assert gui_target().vlm_timeout_s == 120

    def test_env_overrides_manifest(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DX_VLM_TIMEOUT", "90")
        monkeypatch.setenv("DX_CONFIG", str(_min_manifest(tmp_path, "  timeout_s: 120\n")))
        assert gui_target().vlm_timeout_s == 90

    def test_non_numeric_is_a_config_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DX_VLM_TIMEOUT", "soon")
        monkeypatch.setenv("DX_CONFIG", str(_min_manifest(tmp_path)))
        with pytest.raises(GuiConfigError):
            gui_target()

    def test_non_positive_is_a_config_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DX_VLM_TIMEOUT", raising=False)
        monkeypatch.setenv("DX_CONFIG", str(_min_manifest(tmp_path, "  timeout_s: 0\n")))
        with pytest.raises(GuiConfigError):
            gui_target()

    def test_it_reaches_the_request(self, monkeypatch, tmp_path):
        """The resolved value is the timeout actually handed to requests.post."""
        monkeypatch.setenv("DX_VLM_TIMEOUT", "77")
        monkeypatch.setenv("DX_CONFIG", str(_min_manifest(tmp_path)))
        seen = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"response": "YES fine"}

        def fake_post(url, json, timeout):
            seen["timeout"] = timeout
            return _Resp()

        import dx.cmd_verify as cv

        monkeypatch.setattr("requests.post", fake_post)
        t = gui_target()
        cv._verify_with_vlm(b"\x89PNG", "x", t.vlm_endpoint, t.vlm_model, t.vlm_timeout_s)
        assert seen["timeout"] == 77


class TestNoHostDefaults:
    """Regression: a real lab IP and SSH username were baked in as fallbacks, so
    an unconfigured install would silently SSH to someone else's machine.
    """

    @pytest.fixture
    def empty_manifest(self, monkeypatch, tmp_path):
        cfg = tmp_path / "empty.yml"
        cfg.write_text("roles: {}\n", encoding="utf-8")
        monkeypatch.setenv("DX_CONFIG", str(cfg))
        return cfg

    def test_missing_endpoint_is_an_error_not_a_default(self, empty_manifest):
        with pytest.raises(GuiConfigError) as exc:
            gui_target()
        assert "vlm_endpoint" in str(exc.value)
        assert "no default host" in str(exc.value)

    def test_error_names_the_manifest_being_read(self, empty_manifest):
        with pytest.raises(GuiConfigError) as exc:
            gui_target()
        assert str(empty_manifest) in str(exc.value)

    def test_missing_ssh_host_blocks_ssh_capture(self, monkeypatch, tmp_path):
        cfg = tmp_path / "m.yml"
        cfg.write_text(
            "gui_verification:\n"
            '  vlm_endpoint: "http://x.invalid/api/generate"\n'
            '  vlm_model: "m"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("DX_CONFIG", str(cfg))
        gui_target()  # fine without SSH
        with pytest.raises(GuiConfigError) as exc:
            gui_target(require_ssh=True)
        assert "ssh_host" in str(exc.value)
        assert "--screenshot" in str(exc.value)

    def test_verify_gui_reports_config_error_rather_than_raising(self, empty_manifest):
        """cmd_merge calls this; it must degrade to a failed check, not a crash."""
        verdict, detail = verify_gui("anything")
        assert verdict is Verdict.NO_VERDICT
        assert "config error" in detail

    def test_no_ssh_is_attempted_without_configuration(self, empty_manifest, monkeypatch):
        monkeypatch.setattr(
            "dx.cmd_verify.subprocess.run",
            lambda *a, **k: pytest.fail("ssh was invoked with no configured host"),
        )
        assert verify_gui("anything")[0] is Verdict.NO_VERDICT


class TestCaptureMustBePng:
    """Regression: the shipped default `import -window root -` makes ImageMagick
    write PostScript to stdout, and nothing checked, so the VLM was handed a PS
    document labelled as a screenshot. Found the first time verify-gui ran
    against a live desktop (0.9.1).
    """

    @staticmethod
    def _fake_ssh(stdout: bytes):
        def run(cmd, **kwargs):
            assert cmd[0] == "ssh"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

        return run

    def test_postscript_capture_is_rejected_and_names_the_fix(self, monkeypatch):
        ps = b"%!PS-Adobe-3.0\n%%Creator: ImageMagick\n"
        monkeypatch.setattr("dx.cmd_verify.subprocess.run", self._fake_ssh(ps))
        verdict, detail = verify_gui("anything")
        assert verdict is Verdict.NO_VERDICT
        assert "capture error" in detail
        assert "not a PNG" in detail
        assert "png:-" in detail

    def test_png_capture_reaches_the_vlm(self, monkeypatch):
        png = b"\x89PNG\r\n\x1a\n fake"
        monkeypatch.setattr("dx.cmd_verify.subprocess.run", self._fake_ssh(png))
        seen = {}

        def fake_vlm(data, expected, endpoint, model, timeout_s=30):
            seen["data"] = data
            return (Verdict.MET, "YES")

        monkeypatch.setattr("dx.cmd_verify._verify_with_vlm", fake_vlm)
        assert verify_gui("anything") == (Verdict.MET, "YES")
        assert seen["data"] == png


def test_capture_error_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ssh: connect to host vlm.invalid port 22: No route to host")

    monkeypatch.setattr("dx.cmd_verify._capture_ssh", boom)
    verdict, detail = verify_gui("anything")
    # No frame was judged, so this is not a NO — it is nothing.
    assert verdict is Verdict.NO_VERDICT
    assert "capture error" in detail
    assert "No route to host" in detail


def test_vlm_yes_and_no_are_both_honoured(monkeypatch, tmp_path, capsys):
    shot = tmp_path / "screen.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    cases = (
        ("YES — the dialog is shown", Verdict.MET, True),
        ("NO — blank", Verdict.NOT_MET, False),
        ("The dialog appears to be shown, possibly.", Verdict.NO_VERDICT, False),
    )
    for answer, verdict, expected_pass in cases:
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30, _v=verdict, _a=answer: (_v, _a),
        )
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json", "--no-evidence"]
        )
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is expected_pass
        assert payload["verdict"] == verdict.value
        # NO_VERDICT is not a pass and not a NO: exit 1 either way, and the
        # payload says which it was.
        assert exc.value.code == (0 if expected_pass else 1)


def test_missing_screenshot_file_exits_one(capsys, tmp_path):
    args = build_parser().parse_args(
        ["verify-gui", "--screenshot", str(tmp_path / "nope.png"), "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_psoperator_snapshot_dir_is_overridable(monkeypatch, tmp_path, capsys):
    """Never read another user's snapshot directory in tests or in CI."""
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    monkeypatch.setenv("PSOPERATOR_SNAPSHOT_DIR", str(snaps))
    args = build_parser().parse_args(["verify-gui", "--use-psoperator", "--json"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert "no PSOperator snapshots found" in json.loads(capsys.readouterr().out)["error"]


class TestVerifyGuiEmitsEvidence:
    """dx verify-gui writes a dx.gui_verification.v1 bundle by default — the
    screenshot it judged, stored so a reader sees what was actually checked.
    ROADMAP §1.3."""

    def test_a_bundle_is_written_and_reported(self, monkeypatch, tmp_path, capsys):
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n realish")
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (Verdict.MET, "YES matches"),
        )
        ev = tmp_path / "ev"
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json", "--task", "T-GUI",
             "--evidence-dir", str(ev)]
        )
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert exc.value.code == 0
        assert "evidence" in payload
        bundle = Path(payload["evidence"])
        assert bundle.is_dir() and bundle.parent.parent == ev and bundle.parent.name == "T-GUI"
        m = json.loads((bundle / "manifest.json").read_text())
        assert m["schema"] == "dx.gui_verification.v1"
        # the exact bytes we captured are what got stored
        assert (bundle / "artifacts" / "screenshot.png").read_bytes() == shot.read_bytes()

    def test_no_evidence_skips_the_bundle(self, monkeypatch, tmp_path, capsys):
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n realish")
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (Verdict.MET, "YES matches"),
        )
        ev = tmp_path / "ev"
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json", "--no-evidence",
             "--evidence-dir", str(ev)]
        )
        with pytest.raises(SystemExit):
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert "evidence" not in payload
        assert not ev.exists()

    def test_a_failed_check_still_leaves_a_bundle(self, monkeypatch, tmp_path, capsys):
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n realish")
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (Verdict.NOT_MET, "NO it is blank"),
        )
        ev = tmp_path / "ev"
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json", "--evidence-dir", str(ev)]
        )
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert exc.value.code == 1
        bundle = Path(payload["evidence"])
        m = json.loads((bundle / "manifest.json").read_text())
        assert m["result"]["passed"] is False


class TestObserverAttestation:
    """`--observer` requires a verified observer attestation bound to the frame.
    It fails closed: requested-but-unobtainable is an error, never a clean pass
    that silently dropped the provenance."""

    @staticmethod
    def _args(tmp_path, ev, *extra):
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n realish")
        return build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json", "--observer",
             "--evidence-dir", str(ev), *extra]
        )

    def test_verified_provenance_is_recorded_in_the_bundle(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (Verdict.MET, "YES matches"),
        )
        prov = {"key_id": "obs-1", "frame_hash": "b" * 64,
                "signature_verified": True, "frame_hash_matches": True}
        monkeypatch.setattr("dx.cmd_verify._observer_provenance", lambda png: prov)
        ev = tmp_path / "ev"
        args = self._args(tmp_path, ev)
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert exc.value.code == 0
        m = json.loads((Path(payload["evidence"]) / "manifest.json").read_text())
        assert m["gui_verification"]["observer"] == prov
        assert m["checks"]["observer_attested"]["ok"] is True

    def test_failure_to_attest_fails_closed_with_no_bundle(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (Verdict.MET, "YES matches"),
        )
        def boom(png):
            from dx.observer import ObserverError
            raise ObserverError("frame hash mismatch: not the frame attested")
        monkeypatch.setattr("dx.cmd_verify._observer_provenance", boom)
        ev = tmp_path / "ev"
        args = self._args(tmp_path, ev)
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert exc.value.code == 1
        assert "observer attestation not obtained" in payload["error"]
        assert not ev.exists()

    def test_missing_key_path_is_the_failure(self, monkeypatch, tmp_path, capsys):
        # real _observer_provenance, no key env → RuntimeError → fail closed
        monkeypatch.delenv("PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH", raising=False)
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (Verdict.MET, "YES matches"),
        )
        ev = tmp_path / "ev"
        args = self._args(tmp_path, ev)
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert exc.value.code == 1
        assert "PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH" in payload["error"]


class TestParseVlmReply:
    """The prompt asks for YES or NO as the first word. dx reads that word and
    nothing else — a reply that breaks the contract is `no_verdict`, never a
    guess. Cases come from `~/ai/review/typesafe/corpus/seed.jsonl`, where the
    old `startswith("YES")` scored 0.67 and collapsed every no-verdict reply
    into NO.
    """

    @pytest.mark.parametrize(
        "reply, verdict",
        [
            ("YES - the calculator shows 42.", Verdict.MET),
            ("Yes, the calculator window is open.", Verdict.MET),
            ("yes the dialog is there", Verdict.MET),
            ("**YES** — calculator visible, result 42.", Verdict.MET),
            ("NO - the calculator shows 24, not 42.", Verdict.NOT_MET),
            ("No, there is no calculator window.", Verdict.NOT_MET),
            ("No. The dialog is open, but focus is elsewhere.", Verdict.NOT_MET),
        ],
    )
    def test_first_word_decides(self, reply, verdict):
        assert parse_vlm_reply(reply) == (verdict, "")

    def test_yesterday_is_not_yes(self):
        """The `startswith("YES")` bug: `YESTERDAY` passed."""
        verdict, reason = parse_vlm_reply("Yesterday's result is still displayed; the value is 0.")
        assert verdict is Verdict.NO_VERDICT
        assert "did not open with YES or NO" in reason

    @pytest.mark.parametrize(
        "reply",
        [
            "The screen does show the calculator with 42 in the display.",
            "Answer: YES. The calculator displays 42.",
            "Correct, the calculator is open and shows 42.",
            "It seems possible that the number is 42, though it could be 47.",
            "Not exactly — a dialog is open but I cannot tell which control is focused.",
            "I'm sorry, but I can't help with that request.",
        ],
    )
    def test_prose_is_not_interpreted(self, reply):
        """Whether the prose *sounds* like a yes is not dx's call to make."""
        verdict, reason = parse_vlm_reply(reply)
        assert verdict is Verdict.NO_VERDICT
        assert reason == "reply did not open with YES or NO"

    def test_empty_and_transport_error_are_no_verdict(self):
        assert parse_vlm_reply("") == (Verdict.NO_VERDICT, "empty reply")
        assert parse_vlm_reply("   \n") == (Verdict.NO_VERDICT, "empty reply")
        assert parse_vlm_reply("VLM error: read timed out") == (Verdict.NO_VERDICT, "transport error")

    def test_contradiction_is_no_verdict_not_a_pass(self):
        """`YES` followed by `No dialog is visible` was a PASSED receipt."""
        verdict, reason = parse_vlm_reply("YES\n\nNo dialog is visible; the editor fills the screen.")
        assert verdict is Verdict.NO_VERDICT
        assert "contradictory" in reason
        verdict, reason = parse_vlm_reply("NO. Yes there is a window but not the right one.")
        assert verdict is Verdict.NO_VERDICT
        assert "contradictory" in reason

    def test_never_returns_a_fourth_value(self):
        for reply in ("YES", "NO", "", "maybe", "YES NO", "**no**"):
            assert parse_vlm_reply(reply)[0] in Verdict


class TestNoVerdictInTheReceipt:
    """A silent, erroring or prose-only model is not a failed screen. The bundle
    has to keep "the model said no" and "the model did not answer" apart, and
    `passed` may be true for exactly one of the three verdicts."""

    def _run(self, monkeypatch, tmp_path, capsys, verdict, reply):
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n realish")
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (verdict, reply),
        )
        ev = tmp_path / "ev"
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json", "--evidence-dir", str(ev)]
        )
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        manifest = json.loads((Path(payload["evidence"]) / "manifest.json").read_text())
        return exc.value.code, payload, manifest

    def test_no_verdict_is_recorded_as_such_and_does_not_pass(self, monkeypatch, tmp_path, capsys):
        code, payload, m = self._run(
            monkeypatch, tmp_path, capsys, Verdict.NO_VERDICT, "The image appears to be blank."
        )
        assert code == 1
        assert payload["verdict"] == "no_verdict"
        assert m["result"]["passed"] is False
        assert m["gui_verification"]["verdict"] == "no_verdict"
        assert m["checks"]["verdict_reached"]["ok"] is False
        assert m["checks"]["expectation_met"]["ok"] is False
        assert m["checks"]["vlm_answered"]["ok"] is True  # the model *did* reply

    def test_a_real_no_reaches_a_verdict(self, monkeypatch, tmp_path, capsys):
        code, payload, m = self._run(monkeypatch, tmp_path, capsys, Verdict.NOT_MET, "NO — blank")
        assert code == 1
        assert m["gui_verification"]["verdict"] == "not_met"
        assert m["checks"]["verdict_reached"]["ok"] is True
        assert m["checks"]["expectation_met"]["ok"] is False

    def test_passed_is_true_only_for_met(self, monkeypatch, tmp_path, capsys):
        for verdict in Verdict:
            _, _, m = self._run(monkeypatch, tmp_path, capsys, verdict, "whatever")
            assert m["result"]["passed"] is (verdict is Verdict.MET)

    def test_human_output_marks_no_verdict_distinctly(self, monkeypatch, tmp_path, capsys):
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n realish")
        monkeypatch.setattr(
            "dx.cmd_verify._verify_with_vlm",
            lambda png, exp, ep, model, timeout_s=30: (Verdict.NO_VERDICT, ""),
        )
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--no-evidence"]
        )
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "no verdict" in out and "empty reply" in out
        assert "❌" not in out


def test_merge_records_no_verdict_distinctly_from_a_no(monkeypatch, tmp_path, capsys):
    """`dx merge --verify-gui` fails the advisory check either way (RL-007 —
    the signature is the gate), but the record must say which it was."""
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "absent"))
    monkeypatch.setattr(
        "dx.cmd_merge.verify_gui",
        lambda expected: (Verdict.NO_VERDICT, "VLM error: read timed out"),
    )
    args = build_parser().parse_args(["merge", "T-TEST", "--verify-gui", "--no-evidence"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no verdict" in err
    assert "GUI verification failed" not in err


class TestAbortedGenerationIsAnError:
    """A 200 from Ollama is not an answer. The first live run through the
    three-valued path got `done: false` and 31 question marks from a throttled
    node; parsing that as a reply gave the right verdict for the wrong reason
    and a receipt claiming the model answered. Classify before parsing."""

    GARBAGE = "?" * 31

    @staticmethod
    def _post(body):
        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return body

        return lambda url, json, timeout: _R()

    def _verify(self, monkeypatch, body):
        import dx.cmd_verify as cv

        monkeypatch.setattr("requests.post", self._post(body))
        return cv._verify_with_vlm(b"\x89PNG", "x", "http://vlm.invalid/api/generate", "m")

    def test_done_false_is_a_vlm_error_not_a_reply(self, monkeypatch):
        verdict, out = self._verify(monkeypatch, {"model": "m", "done": False, "response": self.GARBAGE})
        assert verdict is Verdict.NO_VERDICT
        assert out.startswith("VLM error: generation aborted")
        assert "done=false" in out and "???" in out  # the placeholder is kept as evidence
        assert parse_vlm_reply(out) == (Verdict.NO_VERDICT, "transport error")

    def test_an_error_body_with_200_is_a_vlm_error(self, monkeypatch):
        verdict, out = self._verify(monkeypatch, {"error": "model 'm' not found"})
        assert verdict is Verdict.NO_VERDICT and out == "VLM error: model 'm' not found"

    def test_done_true_is_parsed_as_before(self, monkeypatch):
        assert self._verify(monkeypatch, {"done": True, "response": "YES — matches"})[0] is Verdict.MET
        assert self._verify(monkeypatch, {"done": True, "response": "NO — blank"})[0] is Verdict.NOT_MET

    def test_a_body_without_done_is_tolerated(self, monkeypatch):
        """A proxy that drops the key must not turn every answer into an error."""
        assert self._verify(monkeypatch, {"response": "YES — matches"})[0] is Verdict.MET

    def test_the_receipt_says_the_model_did_not_answer(self, monkeypatch, tmp_path, capsys):
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n realish")
        monkeypatch.setattr("requests.post", self._post({"done": False, "response": self.GARBAGE}))
        ev = tmp_path / "ev"
        args = build_parser().parse_args(
            ["verify-gui", "--screenshot", str(shot), "--json", "--evidence-dir", str(ev)]
        )
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        payload = json.loads(capsys.readouterr().out)
        assert exc.value.code == 1 and payload["verdict"] == "no_verdict"
        m = json.loads((Path(payload["evidence"]) / "manifest.json").read_text())
        assert m["checks"]["vlm_answered"]["ok"] is False  # was True before this change
        assert m["checks"]["verdict_reached"]["detail"] == "transport error"
        assert m["gui_verification"]["vlm_answer"].startswith("VLM error: generation aborted")
