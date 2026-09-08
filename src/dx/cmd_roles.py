import json
import sys
from pathlib import Path

from .config_loader import get_roles_path
from .role_models import FitLevel
from .role_registry import get_registry, get_role, load_registry
from .role_validate import count_by_fit, validate_registry


def register_roles_subcommand(subparsers) -> None:
    roles = subparsers.add_parser("roles", help="Inspect and validate role cards")
    roles_sub = roles.add_subparsers(dest="roles_command", required=True)

    # dx roles list
    lst = roles_sub.add_parser("list", help="List role cards")
    lst.add_argument("--fit", choices=[f.value for f in FitLevel])
    lst.add_argument("--seat", type=str)
    lst.add_argument("--anchored", action="store_true")
    lst.add_argument("--slug", type=str)
    lst.add_argument("--json", action="store_true")
    lst.add_argument("--path", type=str)
    lst.set_defaults(func=cmd_roles_list)

    # dx roles validate
    val = roles_sub.add_parser("validate", help="Structural checks on role cards")
    val.add_argument("--path", type=str)
    val.add_argument("--verbose", "-v", action="store_true")
    val.set_defaults(func=cmd_roles_validate)


def _cards_path(args) -> Path:
    return Path(args.path).expanduser() if args.path else get_roles_path()


def cmd_roles_list(args) -> None:
    cards_path = _cards_path(args)
    if not cards_path.exists():
        print(f"ERROR: cards not found at {cards_path}", file=sys.stderr)
        sys.exit(1)

    registry = load_registry(cards_path)

    if args.slug:
        card = get_role(args.slug)
        if not card:
            print(f"ERROR: role '{args.slug}' not found", file=sys.stderr)
            sys.exit(1)
        payload = {
            "slug": card.slug,
            "fit": card.fit.value,
            "seat": card.seat,
            "anchored": card.anchored,
            "mandate": card.mandate,
            "must_not": card.must_not,
            "handoff": card.handoff,
            "prohibited_patterns": card.prohibited_patterns,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"SLUG:      {card.slug}")
            print(f"FIT:       {card.fit.value}")
            print(f"SEAT:      {card.seat}")
            print(f"ANCHORED:  {card.anchored}")
            print(f"\nMANDATE:\n{card.mandate[:500]}")
            print(f"\nMUST NOT:\n{card.must_not[:500]}")
        return

    cards = list(registry.values())
    if args.anchored:
        cards = [c for c in cards if c.fit == FitLevel.ANCHORED]
    elif args.fit:
        cards = [c for c in cards if c.fit.value == args.fit]
    if args.seat:
        cards = [c for c in cards if c.seat == args.seat]
    cards.sort(key=lambda c: c.slug)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "slug": c.slug,
                        "fit": c.fit.value,
                        "seat": c.seat,
                        "anchored": c.anchored,
                    }
                    for c in cards
                ],
                indent=2,
            )
        )
        return

    if not cards:
        print("(no roles match the filter)")
        return

    slug_w = max(len(c.slug) for c in cards) + 2
    fit_w = max(len(c.fit.value) for c in cards) + 2
    seat_w = max(len(c.seat) for c in cards) + 2
    print(f"{'Slug':<{slug_w}}{'Fit':<{fit_w}}{'Seat':<{seat_w}}Anchored")
    print("-" * (slug_w + fit_w + seat_w + 10))
    for c in cards:
        anchored = "YES" if c.anchored else ""
        print(f"{c.slug:<{slug_w}}{c.fit.value:<{fit_w}}{c.seat:<{seat_w}}{anchored}")


def cmd_roles_validate(args) -> None:
    cards_path = _cards_path(args)
    if not cards_path.exists():
        print(f"ERROR: cards not found at {cards_path}", file=sys.stderr)
        sys.exit(1)

    print(f"🔍 Validating role cards at {cards_path}")
    print("-" * 60)

    all_ok, failures = validate_registry(cards_path)
    registry = get_registry() or {}

    if args.verbose and all_ok:
        for slug, card in sorted(registry.items()):
            print(f"  ✓ {slug} (Fit: {card.fit.value}, Seat: {card.seat})")

    if all_ok:
        print(f"✅ PASS: {len(registry)} role cards validated.")
        counts = count_by_fit(registry)
        print("\n📊 Fit distribution:")
        for fit, count in counts.items():
            print(f"   {fit}: {count}")
        sys.exit(0)

    print("❌ FAIL: structural errors found:\n")
    for slug, errs in failures:
        print(f"  📄 {slug}:")
        for e in errs:
            print(f"     - {e}")
    print(f"\nTotal cards with errors: {len(failures)}")
    sys.exit(1)
