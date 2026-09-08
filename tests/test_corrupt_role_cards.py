"""A role card that cannot be parsed must fail closed everywhere.

The role cards are the constitution. Before this, an unparseable card printed a
WARN to stdout and was dropped from the registry — and `dx roles validate` still
reported `PASS`, certifying a deck with pages missing.

The sharp end is Anchored roles. Those seven exist to refuse autonomous
execution. A corrupt Anchored card vanished from the registry, so `dx run` said
"role not found" and the hard-block never fired: a fail-open on the one gate
whose whole job is to stop.
"""
from __future__ import annotations

import pytest

from dx.cli import build_parser
from dx.role_registry import failed_slug, get_parse_failures, load_registry
from dx.role_validate import validate_registry

GOOD_CARD = (
    "# Fine\n\n**Agent fit:** High · **9-person seat:** S1\n\n"
    "## Mandate\n\nA mandate long enough to satisfy validation.\n\n"
    "## Must not (separation of duties)\n\n- Do not self-approve.\n"
)
ANCHORED_CARD = (
    "# Anchored\n\n**Agent fit:** Anchored · **9-person seat:** Borrowed\n\n"
    "## Mandate\n\nRequires a named accountable human to decide.\n\n"
    "## Must not (separation of duties)\n\n- Be simulated to closure.\n\n"
    "## Handoff\n\n**Hands to:** `someone`\n"
)


@pytest.fixture
def deck(monkeypatch, tmp_path):
    """A roles directory with one good card and one unreadable card."""

    def _build(*, corrupt: str = "broken-role", anchored: bool = False):
        (tmp_path / "fine-role.md").write_text(GOOD_CARD, encoding="utf-8")
        if anchored:
            (tmp_path / "anchored-role.md").write_text(ANCHORED_CARD, encoding="utf-8")
        # Invalid UTF-8: unreadable before any parsing logic runs.
        (tmp_path / f"{corrupt}.md").write_bytes(b"# Broken\n\xff\xfe not utf8 \xff\n")
        monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path))
        return tmp_path

    return _build


class TestRegistry:
    def test_parse_failures_are_recorded_not_discarded(self, deck):
        path = deck()
        load_registry(path, force=True)
        failures = get_parse_failures()
        assert "broken-role.md" in failures
        assert "UnicodeDecodeError" in failures["broken-role.md"]

    def test_good_cards_still_load(self, deck):
        path = deck()
        registry = load_registry(path, force=True)
        assert "fine-role" in registry

    def test_failed_slug_maps_a_role_to_its_broken_file(self, deck):
        path = deck()
        load_registry(path, force=True)
        assert failed_slug("broken-role") == "broken-role.md"
        assert failed_slug("fine-role") is None
        assert failed_slug("never-existed") is None

    def test_a_clean_deck_reports_no_failures(self, tmp_path, monkeypatch):
        (tmp_path / "fine-role.md").write_text(GOOD_CARD, encoding="utf-8")
        monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path))
        load_registry(tmp_path, force=True)
        assert get_parse_failures() == {}


class TestValidate:
    def test_validate_fails_when_a_card_cannot_be_parsed(self, deck):
        """Regression: this reported `PASS: 1 role cards validated` and exit 0."""
        path = deck()
        all_ok, failures = validate_registry(path)
        assert not all_ok
        assert any(name == "broken-role.md" for name, _ in failures)

    def test_the_failure_explains_why(self, deck):
        path = deck()
        _, failures = validate_registry(path)
        reasons = " ".join(r for _, errs in failures for r in errs)
        assert "could not be parsed" in reasons

    def test_cli_exits_one(self, deck, capsys):
        deck()
        args = build_parser().parse_args(["roles", "validate"])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        out = capsys.readouterr().out
        assert exc.value.code == 1, out
        assert "broken-role.md" in out

    def test_a_clean_deck_still_passes(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "fine-role.md").write_text(GOOD_CARD, encoding="utf-8")
        monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path))
        args = build_parser().parse_args(["roles", "validate"])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        out = capsys.readouterr().out
        assert exc.value.code == 0, out
        assert "PASS" in out


class TestRun:
    def _run(self, *argv):
        args = build_parser().parse_args(["run", *argv])
        args.func(args)

    def test_corrupt_card_is_reported_as_corrupt_not_missing(self, deck, capsys, monkeypatch):
        """'not found' reads like a typo and sends the operator to the wrong
        place while their governance file is what is broken."""
        deck()
        monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/pxx")
        with pytest.raises(SystemExit) as exc:
            self._run("T-1", "--required_role", "broken-role", "-m", "x", "--dry-run")
        err = capsys.readouterr().err
        assert exc.value.code == 1
        assert "could not be parsed" in err
        assert "broken-role.md" in err
        assert "dx roles validate" in err
        assert "not found" not in err

    def test_a_genuinely_unknown_role_still_says_not_found(self, deck, capsys, monkeypatch):
        deck()
        monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/pxx")
        with pytest.raises(SystemExit) as exc:
            self._run("T-1", "--required_role", "no-such-role", "-m", "x", "--dry-run")
        err = capsys.readouterr().err
        assert exc.value.code == 1
        assert "not found" in err

    def test_a_corrupt_anchored_card_does_not_become_runnable(self, deck, capsys, monkeypatch):
        """The fail-open this whole change exists to close: an Anchored card
        that fails to parse must never silently drop its hard-block."""
        deck(corrupt="anchored-sme")
        monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/pxx")
        with pytest.raises(SystemExit) as exc:
            self._run("T-1", "--required_role", "anchored-sme", "-m", "decide", "--dry-run")
        assert exc.value.code != 0
        assert "DRY RUN" not in capsys.readouterr().out

    def test_intact_anchored_cards_still_block(self, deck, capsys, monkeypatch):
        deck(anchored=True)
        monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/pxx")
        with pytest.raises(SystemExit) as exc:
            self._run("T-1", "--required_role", "anchored-role", "-m", "x", "--dry-run")
        assert exc.value.code == 2
        assert "Anchored" in capsys.readouterr().err


INVALID_CARD = "# Bare\n\n**Agent fit:** High · **9-person seat:** S1\n\n## Mandate\n\nShort.\n"


class TestInvalidCardsCannotGovern:
    """A card that fails `dx roles validate` must not govern a run.

    Without this, dx injected an empty MANDATE and an empty MUST NOT into the
    prompt and reported success — the governance text meant to constrain the
    agent silently blank, while `dx roles validate` had been calling the card
    invalid all along. Two commands disagreeing about whether a card is usable
    is the same fail-open in a different place.
    """

    @pytest.fixture
    def invalid_deck(self, monkeypatch, tmp_path):
        (tmp_path / "bare-role.md").write_text(INVALID_CARD, encoding="utf-8")
        (tmp_path / "fine-role.md").write_text(GOOD_CARD, encoding="utf-8")
        monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path))
        monkeypatch.setattr("dx.cmd_run._resolve_pxx", lambda: "/fake/pxx")
        return tmp_path

    def _run(self, *argv):
        args = build_parser().parse_args(["run", *argv])
        args.func(args)

    def test_run_refuses_an_invalid_card(self, invalid_deck, capsys):
        with pytest.raises(SystemExit) as exc:
            self._run("T-1", "--required_role", "bare-role", "-m", "x", "--dry-run")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "fails validation" in err
        assert "dx roles validate" in err

    def test_the_refusal_lists_the_specific_problems(self, invalid_deck, capsys):
        with pytest.raises(SystemExit):
            self._run("T-1", "--required_role", "bare-role", "-m", "x", "--dry-run")
        err = capsys.readouterr().err
        assert "mandate" in err
        assert "must_not" in err

    def test_it_never_reaches_the_prompt(self, invalid_deck, capsys):
        """The failure must happen before any prompt is built."""
        with pytest.raises(SystemExit):
            self._run("T-1", "--required_role", "bare-role", "-m", "x", "--dry-run")
        assert "DRY RUN" not in capsys.readouterr().out

    def test_force_bypasses_but_announces(self, invalid_deck, capsys):
        self._run("T-1", "--required_role", "bare-role", "-m", "x", "--dry-run", "--force")
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "--force in effect" in captured.err
        assert "invalid role card" in captured.err

    def test_a_valid_card_is_unaffected(self, invalid_deck, capsys):
        self._run("T-1", "--required_role", "fine-role", "-m", "x", "--dry-run")
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "fails validation" not in captured.err

    def test_the_two_commands_agree(self, invalid_deck):
        """Whatever dx roles validate rejects, dx run must also reject."""
        from dx.role_parser import parse_role_file
        from dx.role_validate import validate_card

        for name, expect_ok in (("fine-role", True), ("bare-role", False)):
            card = parse_role_file(invalid_deck / f"{name}.md")
            assert validate_card(card)[0] is expect_ok


class TestEmptyDeck:
    def test_doctor_fails_with_no_role_cards(self, monkeypatch, tmp_path, capsys):
        """Regression: doctor reported a healthy install with zero cards, while
        dx roles validate correctly failed on the same directory."""
        monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path))
        monkeypatch.setattr("dx.cmd_doctor._find_on_path", lambda name: f"/fake/{name}")
        monkeypatch.setattr("dx.cmd_doctor._check_import", lambda m, label: True)
        monkeypatch.setattr(
            "dx.cmd_doctor.subprocess.run", lambda *a, **k: type("R", (), {"returncode": 0})()
        )
        args = build_parser().parse_args(["doctor", "--no-network"])
        with pytest.raises(SystemExit) as exc:
            args.func(args)
        out = capsys.readouterr().out
        assert exc.value.code == 1, out
        assert "No role cards" in out

    def test_validate_also_fails(self, tmp_path):
        all_ok, failures = validate_registry(tmp_path)
        assert not all_ok
        assert failures[0][0] == "REGISTRY"
