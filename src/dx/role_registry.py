from pathlib import Path
from typing import Dict, Optional

from .role_models import RoleCard
from .role_parser import parse_role_file

_registry: Optional[Dict[str, RoleCard]] = None


def load_registry(cards_path: Path, force: bool = False) -> Dict[str, RoleCard]:
    """Load all role cards from a directory. Cached; pass force=True to reload."""
    global _registry
    if _registry is not None and not force:
        return _registry

    registry: Dict[str, RoleCard] = {}
    for md_file in sorted(cards_path.glob("*.md")):
        try:
            card = parse_role_file(md_file)
            registry[card.slug] = card
        except Exception as exc:
            print(f"WARN: skipping {md_file.name}: {exc}")

    _registry = registry
    return registry


def get_role(slug: str) -> Optional[RoleCard]:
    if _registry is None:
        raise RuntimeError("Registry not loaded — call load_registry() first.")
    return _registry.get(slug)


def get_registry() -> Optional[Dict[str, RoleCard]]:
    return _registry
