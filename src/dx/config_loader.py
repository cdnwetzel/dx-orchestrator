import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RoleRoute:
    endpoint: str
    model: str | None = None
    provider: str | None = None  # "ollama" | "vllm" | "openai" | "openai-compatible"


DEFAULT_CONFIG_PATH = Path("~/.config/dx/hardware_manifest.yml").expanduser()


def get_config_path() -> Path:
    """Resolved path to the hardware manifest (DX_CONFIG overrides the default)."""
    return Path(os.environ.get("DX_CONFIG", str(DEFAULT_CONFIG_PATH))).expanduser()


# Cache is keyed on the resolved path: changing DX_CONFIG mid-process must not
# silently return the previously loaded manifest.
_config: dict[str, Any] | None = None
_config_source: Path | None = None


def load_config(force: bool = False) -> dict[str, Any]:
    global _config, _config_source

    path = get_config_path()
    if _config is not None and _config_source == path and not force:
        return _config

    if not path.exists():
        raise FileNotFoundError(
            f"Hardware manifest not found at {path}. "
            "Run scripts/setup_dependencies.sh or set DX_CONFIG."
        )

    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}

    _config = loaded
    _config_source = path
    return _config


def get_model_endpoint_for_role(role_slug: str) -> str:
    return get_route_for_role(role_slug).endpoint


def get_route_for_role(role_slug: str) -> RoleRoute:
    """Return the endpoint/model/provider for a role, falling back to `default`."""
    cfg = load_config()
    roles = cfg.get("roles", {}) or {}
    default_cfg = roles.get("default") or {}
    default_ep = default_cfg.get("endpoint", "http://localhost:11434")
    default_model = default_cfg.get("model")
    default_provider = default_cfg.get("provider")

    role_cfg = roles.get(role_slug) or {}
    return RoleRoute(
        endpoint=role_cfg.get("endpoint") or default_ep,
        model=role_cfg.get("model") or default_model,
        provider=role_cfg.get("provider") or default_provider,
    )


def get_gui_config() -> dict[str, Any]:
    return load_config().get("gui_verification", {}) or {}


def get_psoperator_config() -> dict[str, Any]:
    return load_config().get("psoperator", {}) or {}


# ---------------------------------------------------------------------------
# External repo locations
# ---------------------------------------------------------------------------

DEFAULT_LEDGER_REPO = Path("~/ai/devswarm-ledger").expanduser()
DEFAULT_ROLES_PATH = Path("~/ai/claude-sdlc-roles/skills/sdlc-role/roles").expanduser()


def get_ledger_repo_path() -> Path:
    """Path to a local clone of cdnwetzel/devswarm-ledger.

    Override with DX_LEDGER_REPO. Falls back to ~/ai/devswarm-ledger,
    which is where scripts/setup_dependencies.sh clones it.
    """
    return Path(
        os.environ.get("DX_LEDGER_REPO", str(DEFAULT_LEDGER_REPO))
    ).expanduser()


DEFAULT_PSOPERATOR_REPO = Path("~/ai/psoperator").expanduser()


def get_psoperator_repo() -> Path:
    """Path to a local clone of cdnwetzel/psoperator.

    Resolution order: PSOPERATOR_REPO env var, then `psoperator.repo` in the
    manifest, then ~/ai/psoperator (where setup_dependencies.sh clones it).
    """
    env = os.environ.get("PSOPERATOR_REPO")
    if env:
        return Path(env).expanduser()
    try:
        configured = get_psoperator_config().get("repo")
    except FileNotFoundError:
        return DEFAULT_PSOPERATOR_REPO
    if configured:
        return Path(str(configured)).expanduser()
    return DEFAULT_PSOPERATOR_REPO


def get_roles_path() -> Path:
    """Directory holding the claude-sdlc-roles role cards.

    Resolution order:
      1. DX_ROLES_PATH environment variable
      2. `roles_path:` at the top level of the hardware manifest
      3. ~/ai/claude-sdlc-roles/skills/sdlc-role/roles (setup_dependencies.sh default)

    Every other external path dx depends on is overridable; this one is too, so
    the install is not pinned to one directory layout.
    """
    env = os.environ.get("DX_ROLES_PATH")
    if env:
        return Path(env).expanduser()

    try:
        configured = load_config().get("roles_path")
    except FileNotFoundError:
        # `dx roles` must work before the manifest is seeded.
        return DEFAULT_ROLES_PATH

    if configured:
        return Path(str(configured)).expanduser()
    return DEFAULT_ROLES_PATH
