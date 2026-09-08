import re
from pathlib import Path

from .role_models import FitLevel, RoleCard
from .role_registry import get_parse_failures, get_registry, load_registry

# Seat is free-form text in claude-sdlc-roles (e.g. "S4", "S8 + borrowed",
# "S9 design + S8 validate", "All engineers (rotating)", "Borrowed").
# We only reject empty / whitespace-only values.
_SEAT_RE = re.compile(r"^\S.*\S$|^\S$")


def validate_card(card: RoleCard) -> tuple[bool, list[str]]:
    """Run structural checks on a single card. Returns (ok, errors)."""
    errors: list[str] = []

    if card.fit == FitLevel.ANCHORED and not card.anchored:
        errors.append("fit=Anchored but anchored flag is False")
    if card.fit != FitLevel.ANCHORED and card.anchored:
        errors.append(f"fit={card.fit.value} but anchored flag is True")

    if not _SEAT_RE.match(card.seat):
        errors.append(f"invalid seat '{card.seat}' (expected S1..S9, Borrowed, or compound)")

    if len(card.mandate.strip()) < 10:
        errors.append("mandate section empty or <10 chars")

    if len(card.must_not.strip()) < 5:
        errors.append("must_not section missing or too short")

    if card.fit == FitLevel.ANCHORED and len(card.handoff.strip()) < 5:
        errors.append("Anchored role must have a non-empty Handoff section")

    if not re.match(r"^[a-z][a-z0-9\-]*[a-z0-9]$", card.slug):
        errors.append(f"slug '{card.slug}' must be lowercase, hyphenated")

    return (len(errors) == 0, errors)


def validate_registry(cards_path: Path) -> tuple[bool, list[tuple[str, list[str]]]]:
    """Validate every card. Returns (all_ok, [(slug, errors), ...])."""
    load_registry(cards_path, force=True)
    registry = get_registry() or {}

    all_ok = True
    failures: list[tuple[str, list[str]]] = []

    if len(registry) == 0:
        return (False, [("REGISTRY", [f"no role cards found under {cards_path}"])])

    # A card that could not be parsed is not a card that passed. Reporting PASS
    # on a short deck would mean `dx roles validate` certifies a constitution
    # with pages missing — and an unparseable Anchored card silently removes its
    # hard-block from dx run.
    parse_failures = get_parse_failures()
    if parse_failures:
        all_ok = False
        for filename, reason in sorted(parse_failures.items()):
            failures.append((filename, [f"could not be parsed — {reason}"]))

    for slug, card in sorted(registry.items()):
        ok, errs = validate_card(card)
        if not ok:
            all_ok = False
            failures.append((slug, errs))

    return (all_ok, failures)


def count_by_fit(registry: dict[str, RoleCard]) -> dict[str, int]:
    counts = {fit.value: 0 for fit in FitLevel}
    for card in registry.values():
        counts[card.fit.value] += 1
    return counts
