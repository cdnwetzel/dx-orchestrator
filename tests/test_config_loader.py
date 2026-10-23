"""Hardware manifest loading and role → hardware routing."""
from pathlib import Path

import pytest

from dx import config_loader
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


class TestEndpointNormalisation:
    """A trailing `/v1` in the manifest is always wrong — pxx appends its own
    suffix — so dx corrects it rather than 404ing. The correction is announced,
    never silent.

    This is the project's most-documented footgun (a tutorial section, a
    troubleshooting row, a manifest comment, a doctor warning) and documenting
    it four times did not stop it, because hosted OpenAI-compatible services
    publish their base URL *with* the `/v1`.
    """

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("http://h:8000/v1", "http://h:8000"),
            ("http://h:8000/v1/", "http://h:8000"),
            ("https://openrouter.ai/api/v1", "https://openrouter.ai/api"),
            ("http://h/v1/v1", "http://h"),          # doubled, both go
            ("https://x.example/api/v1?k=v", "https://x.example/api?k=v"),
            ("http://h:8000/V1", "http://h:8000"),   # case-insensitive
        ],
    )
    def test_trailing_v1_is_stripped(self, given, expected):
        normalised, changed = config_loader.normalize_endpoint(given)
        assert (normalised, changed) == (expected, True)

    @pytest.mark.parametrize(
        "given",
        [
            "http://h:8000",
            "http://h:11434",
            "http://h/v1/models",     # /v1 is not the last segment
            "https://h/api",
            "not-a-url/v1",           # no scheme: endpoint_warnings' job, not ours
        ],
    )
    def test_everything_else_is_untouched(self, given):
        assert config_loader.normalize_endpoint(given) == (given, False)

    def test_a_host_named_v1_is_not_mangled(self):
        """The trap a string-suffix implementation falls into.

        "http://v1" ends with the characters "/v1" while its path is empty and
        its *host* is "v1". Stripping by suffix yields "http:/" — a silently
        broken endpoint, from a function whose whole job is to unbreak them.
        """
        assert config_loader.normalize_endpoint("http://v1") == ("http://v1", False)
        assert config_loader.normalize_endpoint("http://v1:8000") == (
            "http://v1:8000",
            False,
        )
        assert config_loader.normalize_endpoint("http://v1/v1") == ("http://v1", True)

    def test_the_route_records_what_it_corrected(self, tmp_path, monkeypatch):
        manifest = tmp_path / "m.yml"
        manifest.write_text(
            "roles:\n"
            "  backend-engineer:\n"
            '    endpoint: "http://box.invalid:8000/v1"\n'
            '    provider: "openai-compatible"\n'
            "  default:\n"
            '    endpoint: "http://box.invalid:11434"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("DX_CONFIG", str(manifest))
        config_loader.load_config(force=True)
        route = config_loader.get_route_for_role("backend-engineer")
        assert route.endpoint == "http://box.invalid:8000"
        assert route.endpoint_raw == "http://box.invalid:8000/v1"

    def test_an_untouched_endpoint_reports_no_correction(self, tmp_path, monkeypatch):
        """endpoint_raw must stay None when nothing changed, or every run
        announces a correction it did not make."""
        manifest = tmp_path / "m.yml"
        manifest.write_text(
            "roles:\n"
            "  backend-engineer:\n"
            '    endpoint: "http://box.invalid:8000"\n'
            "  default:\n"
            '    endpoint: "http://box.invalid:11434"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("DX_CONFIG", str(manifest))
        config_loader.load_config(force=True)
        assert config_loader.get_route_for_role("backend-engineer").endpoint_raw is None
