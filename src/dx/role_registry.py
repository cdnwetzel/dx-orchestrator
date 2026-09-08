from pathlib import Path

from .role_models import RoleCard
from .role_parser import parse_role_file

# Cache is keyed on the source directory: loading a second path must not
# silently return the first path's cards.
_registry: dict[str, RoleCard] | None = None
_registry_source: Path | None = None


def load_registry(cards_path: Path, force: bool = False) -> dict[str, RoleCard]:
    """Load all role cards from a directory. Cached per path; force=True reloads."""
    global _registry, _registry_source

    cards_path = Path(cards_path)
    if _registry is not None and _registry_source == cards_path and not force:
        return _registry

    registry: dict[str, RoleCard] = {}
    for md_file in sorted(cards_path.glob("*.md")):
        try:
            card = parse_role_file(md_file)
            registry[card.slug] = card
        except Exception as exc:
            print(f"WARN: skipping {md_file.name}: {exc}")

    _registry = registry
    _registry_source = cards_path
    return registry


def get_role(slug: str) -> RoleCard | None:
    if _registry is None:
        raise RuntimeError("Registry not loaded — call load_registry() first.")
    return _registry.get(slug)


def get_registry() -> dict[str, RoleCard] | None:
    return _registry
