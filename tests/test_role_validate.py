"""Structural validation of role cards — the `dx roles validate` gate."""
from dx.role_models import FitLevel, RoleCard
from dx.role_parser import parse_role_file
from dx.role_registry import get_registry
from dx.role_validate import count_by_fit, validate_card, validate_registry


def _card(**overrides) -> RoleCard:
    base = dict(
        slug="widget-engineer",
        fit=FitLevel.HIGH,
        seat="S4",
        anchored=False,
        mandate="A mandate long enough to satisfy the minimum length check.",
        must_not="- Do not approve your own change",
        handoff="**Hands to:** somebody",
    )
    base.update(overrides)
    return RoleCard(**base)


def test_well_formed_card_passes():
    ok, errors = validate_card(_card())
    assert ok, errors


def test_anchored_flag_must_agree_with_fit():
    ok, errors = validate_card(_card(fit=FitLevel.ANCHORED, anchored=False))
    assert not ok
    assert any("anchored flag is False" in e for e in errors)

    ok, errors = validate_card(_card(fit=FitLevel.HIGH, anchored=True))
    assert not ok
    assert any("anchored flag is True" in e for e in errors)


def test_anchored_role_requires_handoff():
    """dx run prints the handoff text when it blocks; an empty one is useless."""
    ok, errors = validate_card(_card(fit=FitLevel.ANCHORED, anchored=True, handoff=""))
    assert not ok
    assert any("Handoff" in e for e in errors)


def test_short_mandate_rejected():
    ok, errors = validate_card(_card(mandate="tiny"))
    assert not ok
    assert any("mandate" in e for e in errors)


def test_missing_must_not_rejected():
    """must_not is the section dx mechanically enforces; it cannot be empty."""
    ok, errors = validate_card(_card(must_not=""))
    assert not ok
    assert any("must_not" in e for e in errors)


def test_bad_slug_rejected():
    ok, errors = validate_card(_card(slug="Widget_Engineer"))
    assert not ok
    assert any("lowercase" in e for e in errors)


def test_real_world_compound_seats_accepted():
    """Regression: the seat regex was S<n>-only and rejected these real values."""
    for seat in (
        "S4",
        "Borrowed",
        "S8 + borrowed",
        "Rotation (S3/S4/S7)",
        "All engineers (rotating)",
        "S9 design + S8 validate",
        "S6 / S7 / S9 split",
    ):
        ok, errors = validate_card(_card(seat=seat))
        assert ok, f"seat {seat!r} should be accepted: {errors}"


def test_empty_seat_rejected():
    ok, errors = validate_card(_card(seat="   "))
    assert not ok
    assert any("seat" in e for e in errors)


def test_registry_validation_reports_the_malformed_fixture(roles_dir):
    all_ok, failures = validate_registry(roles_dir)
    assert not all_ok
    failed_slugs = {slug for slug, _ in failures}
    assert failed_slugs == {"malformed-role"}


def test_malformed_fixture_fails_for_the_expected_reasons(roles_dir):
    card = parse_role_file(roles_dir / "malformed-role.md")
    ok, errors = validate_card(card)
    assert not ok
    joined = " ".join(errors)
    assert "mandate" in joined
    assert "must_not" in joined
    assert "Handoff" in joined


def test_empty_directory_is_a_failure_not_a_pass(tmp_path):
    """An empty roles dir must never report 'all cards valid'."""
    all_ok, failures = validate_registry(tmp_path)
    assert not all_ok
    assert failures[0][0] == "REGISTRY"


def test_count_by_fit(roles_dir):
    validate_registry(roles_dir)
    counts = count_by_fit(get_registry() or {})
    assert counts["High"] == 1
    assert counts["Partial"] == 1
    assert counts["Anchored"] == 2  # oracle-sme + malformed-role
