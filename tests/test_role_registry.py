"""Registry caching behavior."""
from dx.role_registry import get_registry, get_role, load_registry


def test_loads_all_cards(roles_dir):
    registry = load_registry(roles_dir)
    assert set(registry) == {
        "widget-engineer",
        "oracle-sme",
        "rotating-reviewer",
        "malformed-role",
    }


def test_get_role_returns_none_for_unknown_slug(roles_dir):
    load_registry(roles_dir)
    assert get_role("no-such-role") is None


def test_cache_is_keyed_on_path(roles_dir, tmp_path):
    """Regression: the cache was a bare global, so loading a second directory
    silently returned the first directory's cards.
    """
    load_registry(roles_dir)
    assert "widget-engineer" in (get_registry() or {})

    (tmp_path / "other-role.md").write_text(
        "# Other\n\n**Agent fit:** High · **9-person seat:** S1\n\n"
        "## Mandate\n\nA different directory entirely.\n",
        encoding="utf-8",
    )
    second = load_registry(tmp_path)
    assert set(second) == {"other-role"}
    assert "widget-engineer" not in second


def test_same_path_is_cached(roles_dir):
    first = load_registry(roles_dir)
    second = load_registry(roles_dir)
    assert first is second


def test_force_reloads(roles_dir):
    first = load_registry(roles_dir)
    second = load_registry(roles_dir, force=True)
    assert first is not second
    assert set(first) == set(second)
