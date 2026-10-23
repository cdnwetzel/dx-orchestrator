"""In-memory registry of parsed role cards.

A card that cannot be parsed is recorded as a **failure**, not skipped. The role
cards are the constitution: a deck that is quietly two cards short is not a
valid deck, and an Anchored card that fails to parse disappears from the
registry entirely — which would make `dx run` report "role not found" instead of
refusing to execute it autonomously. That is a fail-open on the Anchored gate,
so parse failures propagate to every caller that asks.
"""
from __future__ import annotations

from pathlib import Path

from .role_models import RoleCard
from .role_parser import parse_role_file

# Caches are keyed on the source directory: loading a second path must not
# silently return the first path's cards.
_registry: dict[str, RoleCard] | None = None
_failures: dict[str, str] | None = None
_registry_source: Path | None = None


def load_registry(cards_path: Path, force: bool = False) -> dict[str, RoleCard]:
    """Load all role cards from a directory. Cached per path; force=True reloads.

    Cards that fail to parse are recorded in `get_parse_failures()` rather than
    being dropped on the floor.
    """
    global _registry, _failures, _registry_source

    cards_path = Path(cards_path)
    if _registry is not None and _registry_source == cards_path and not force:
        return _registry

    registry: dict[str, RoleCard] = {}
    failures: dict[str, str] = {}
    for md_file in sorted(cards_path.glob("*.md")):
        try:
            card = parse_role_file(md_file)
            registry[card.slug] = card
        except Exception as exc:
            failures[md_file.name] = f"{type(exc).__name__}: {exc}"

    _registry = registry
    _failures = failures
    _registry_source = cards_path
    return registry


def get_role(slug: str) -> RoleCard | None:
    if _registry is None:
        raise RuntimeError("Registry not loaded — call load_registry() first.")
    return _registry.get(slug)


def get_registry() -> dict[str, RoleCard] | None:
    return _registry


def get_parse_failures() -> dict[str, str]:
    """Filenames that could not be parsed, mapped to the reason.

    Empty when every card in the directory loaded cleanly.
    """
    return dict(_failures or {})


def failed_slug(slug: str) -> str | None:
    """The filename whose stem matches `slug`, if that file failed to parse.

    Lets callers distinguish "no such role" from "that role's card is corrupt" —
    the first is a typo, the second is a governance problem.
    """
    for filename in get_parse_failures():
        if Path(filename).stem == slug:
            return filename
    return None
