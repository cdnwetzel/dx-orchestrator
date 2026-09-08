"""Parser regression tests.

Five of the six bugs found during the first install were parser bugs
(checkpoint.md). This file pins each of those behaviors.
"""
import pytest

from dx.role_models import FitLevel
from dx.role_parser import parse_role_file


def test_parses_inline_metadata_line(roles_dir):
    """claude-sdlc-roles uses an inline `**Agent fit:** X · **9-person seat:** Y`
    line, not YAML frontmatter. Regression: parser originally only read frontmatter.
    """
    card = parse_role_file(roles_dir / "widget-engineer.md")
    assert card.slug == "widget-engineer"
    assert card.fit is FitLevel.HIGH
    assert card.seat == "S4"
    assert card.anchored is False


def test_seat_stops_at_middot_separator(roles_dir):
    """The seat value must not swallow the rest of the metadata line."""
    card = parse_role_file(roles_dir / "widget-engineer.md")
    assert "·" not in card.seat
    assert "**" not in card.seat


def test_must_not_section_matched_by_prefix(roles_dir):
    """`## Must not (separation of duties)` normalizes to a longer key than
    `must_not`. Regression: exact-key lookup missed it and returned "".
    """
    card = parse_role_file(roles_dir / "widget-engineer.md")
    assert card.must_not, "must_not section must not be empty"
    assert "Review or approve your own widget" in card.must_not


def test_all_standard_sections_populated(roles_dir):
    card = parse_role_file(roles_dir / "widget-engineer.md")
    for field in (
        "mandate",
        "inputs_required",
        "outputs",
        "operating_checklist",
        "definition_of_done",
        "must_not",
        "failure_modes",
        "handoff",
        "related",
    ):
        assert getattr(card, field).strip(), f"{field} should be populated"


def test_anchored_flag_derives_from_fit(roles_dir):
    card = parse_role_file(roles_dir / "oracle-sme.md")
    assert card.fit is FitLevel.ANCHORED
    assert card.anchored is True
    assert card.seat == "Borrowed"


def test_anchored_card_has_handoff(roles_dir):
    """dx run prints the handoff text when it hard-blocks an Anchored role."""
    card = parse_role_file(roles_dir / "oracle-sme.md")
    assert "Hands to" in card.handoff


def test_compound_seat_is_preserved(roles_dir):
    """Regression: the seat value was truncated / rejected for real compound seats."""
    card = parse_role_file(roles_dir / "rotating-reviewer.md")
    assert card.seat == "Rotation (S3/S4/S7)"
    assert card.fit is FitLevel.PARTIAL


def test_prohibited_patterns_extracted_from_must_not(roles_dir):
    card = parse_role_file(roles_dir / "widget-engineer.md")
    assert card.prohibited_patterns
    joined = " ".join(card.prohibited_patterns)
    assert "Review or approve your own widget" in joined


def test_unknown_fit_falls_back_to_partial(tmp_path):
    card_file = tmp_path / "weird-role.md"
    card_file.write_text(
        "# Weird\n\n**Agent fit:** Bananas · **9-person seat:** S1\n\n"
        "## Mandate\n\nSomething long enough to pass validation.\n\n"
        "## Must not (separation of duties)\n\n- Nope\n",
        encoding="utf-8",
    )
    card = parse_role_file(card_file)
    assert card.fit is FitLevel.PARTIAL


def test_missing_metadata_uses_documented_defaults(tmp_path):
    card_file = tmp_path / "bare-role.md"
    card_file.write_text("# Bare\n\n## Mandate\n\nNothing else here.\n", encoding="utf-8")
    card = parse_role_file(card_file)
    assert card.fit is FitLevel.PARTIAL
    assert card.seat == "Borrowed"
    assert card.must_not == ""


def test_yaml_frontmatter_still_supported(tmp_path):
    """Frontmatter is not used by claude-sdlc-roles today but is a supported input."""
    card_file = tmp_path / "fm-role.md"
    card_file.write_text(
        "---\nfit: Anchored\nseat: S2\n---\n\n"
        "# FM\n\n## Mandate\n\nA sufficiently long mandate section.\n\n"
        "## Must not (separation of duties)\n\n- Do not do the thing\n\n"
        "## Handoff\n\n**Hands to:** someone\n",
        encoding="utf-8",
    )
    card = parse_role_file(card_file)
    assert card.fit is FitLevel.ANCHORED
    assert card.seat == "S2"
    assert card.anchored is True


def test_slug_comes_from_filename(tmp_path):
    card_file = tmp_path / "some-slug.md"
    card_file.write_text("# Title mismatch\n\n## Mandate\n\nBody.\n", encoding="utf-8")
    assert parse_role_file(card_file).slug == "some-slug"


def test_to_prompt_context_includes_mandate_and_must_not(roles_dir):
    """This string is what actually reaches the model — it must carry both."""
    card = parse_role_file(roles_dir / "widget-engineer.md")
    ctx = card.to_prompt_context()
    assert "widget-engineer" in ctx
    assert "High" in ctx
    assert card.mandate.splitlines()[0] in ctx
    assert "Review or approve your own widget" in ctx


@pytest.mark.parametrize("name", ["widget-engineer", "oracle-sme", "rotating-reviewer"])
def test_every_fixture_parses_without_error(roles_dir, name):
    assert parse_role_file(roles_dir / f"{name}.md").slug == name
