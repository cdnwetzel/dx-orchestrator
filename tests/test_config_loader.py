"""Hardware manifest loading and role → hardware routing."""
from pathlib import Path

import pytest

from dx.config_loader import (
    DEFAULT_ROLES_PATH,
    get_config_path,
    get_gui_config,
    get_psoperator_config,
    get_roles_path,
    get_route_for_role,
    load_config,
)


def test_dx_config_env_selects_the_manifest(manifest_path):
    assert get_config_path() == manifest_path
    assert "roles" in load_config()


def test_missing_manifest_raises_with_actionable_message(monkeypatch, tmp_path):
    monkeypatch.setenv("DX_CONFIG", str(tmp_path / "nope.yml"))
    with pytest.raises(FileNotFoundError) as exc:
        load_config(force=True)
    assert "setup_dependencies.sh" in str(exc.value)


def test_route_resolves_endpoint_model_and_provider():
    route = get_route_for_role("widget-engineer")
    assert route.endpoint == "http://vllm.invalid:8007"
    assert route.model == "test-27b"
    assert route.provider == "vllm"


def test_unknown_role_falls_back_to_default():
    route = get_route_for_role("role-not-in-manifest")
    assert route.endpoint == "http://default.invalid:11434"
    assert route.model == "default-model"
    assert route.provider == "ollama"


def test_partial_role_entry_inherits_default_model_and_provider():
    """A role that sets only `endpoint` must still get a model and provider."""
    route = get_route_for_role("partial-route")
    assert route.endpoint == "http://partial.invalid:9000"
    assert route.model == "default-model"
    assert route.provider == "ollama"


def test_endpoint_has_no_trailing_v1():
    """pxx appends its own /v1/models. A trailing /v1 in the manifest doubles it
    and every task fails with MODEL_UNAVAILABLE (commit 9a424f5).
    """
    for slug in ("widget-engineer", "rotating-reviewer", "partial-route", "default"):
        assert not get_route_for_role(slug).endpoint.rstrip("/").endswith("/v1")


def test_config_cache_invalidates_when_dx_config_changes(monkeypatch, tmp_path):
    """Regression: the manifest cache was a bare global, so a changed DX_CONFIG
    silently returned the previously loaded manifest.
    """
    assert get_route_for_role("widget-engineer").endpoint == "http://vllm.invalid:8007"

    other = tmp_path / "other.yml"
    other.write_text(
        'roles:\n  widget-engineer:\n    endpoint: "http://elsewhere.invalid:1234"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DX_CONFIG", str(other))
    assert get_route_for_role("widget-engineer").endpoint == "http://elsewhere.invalid:1234"


def test_gui_and_psoperator_sections():
    gui = get_gui_config()
    assert gui["vlm_model"] == "test-vl:3b"
    assert gui["vlm_endpoint"].startswith("http://vlm.invalid")
    assert get_psoperator_config()["observer_port"] == 9764


def test_missing_optional_sections_return_empty_dicts(monkeypatch, tmp_path):
    bare = tmp_path / "bare.yml"
    bare.write_text("roles: {}\n", encoding="utf-8")
    monkeypatch.setenv("DX_CONFIG", str(bare))
    assert get_gui_config() == {}
    assert get_psoperator_config() == {}


class TestRolesPathResolution:
    """DX_ROLES_PATH > manifest `roles_path:` > packaged default."""

    def test_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DX_ROLES_PATH", str(tmp_path))
        assert get_roles_path() == tmp_path

    def test_manifest_used_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("DX_ROLES_PATH", raising=False)
        assert get_roles_path() == Path("IGNORED_BY_TESTS")

    def test_default_when_neither_set(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DX_ROLES_PATH", raising=False)
        bare = tmp_path / "bare.yml"
        bare.write_text("roles: {}\n", encoding="utf-8")
        monkeypatch.setenv("DX_CONFIG", str(bare))
        assert get_roles_path() == DEFAULT_ROLES_PATH

    def test_default_when_manifest_absent(self, monkeypatch, tmp_path):
        """`dx roles` must work before setup_dependencies.sh has seeded a manifest."""
        monkeypatch.delenv("DX_ROLES_PATH", raising=False)
        monkeypatch.setenv("DX_CONFIG", str(tmp_path / "absent.yml"))
        assert get_roles_path() == DEFAULT_ROLES_PATH

    def test_tilde_is_expanded(self, monkeypatch):
        monkeypatch.setenv("DX_ROLES_PATH", "~/some/roles")
        assert "~" not in str(get_roles_path())
