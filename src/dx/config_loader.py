import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


@dataclass
class RoleRoute:
    endpoint: str
    model: Optional[str] = None

DEFAULT_CONFIG_PATH = Path("~/.config/dx/hardware_manifest.yml").expanduser()


def _config_path() -> Path:
    return Path(os.environ.get("DX_CONFIG", str(DEFAULT_CONFIG_PATH))).expanduser()


_config: Optional[Dict[str, Any]] = None


def load_config(force: bool = False) -> Dict[str, Any]:
    global _config
    if _config is not None and not force:
        return _config

    path = _config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Hardware manifest not found at {path}. "
            "Run scripts/setup_dependencies.sh or set DX_CONFIG."
        )

    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}

    _config = loaded
    return _config


def get_model_endpoint_for_role(role_slug: str) -> str:
    return get_route_for_role(role_slug).endpoint


def get_route_for_role(role_slug: str) -> RoleRoute:
    """Return (endpoint, model) for a role, falling back to `default`."""
    cfg = load_config()
    roles = cfg.get("roles", {}) or {}
    default_cfg = roles.get("default") or {}
    default_ep = default_cfg.get("endpoint", "http://localhost:11434/v1")
    default_model = default_cfg.get("model")

    role_cfg = roles.get(role_slug) or {}
    return RoleRoute(
        endpoint=role_cfg.get("endpoint", default_ep),
        model=role_cfg.get("model", default_model),
    )


def get_gui_config() -> Dict[str, Any]:
    return load_config().get("gui_verification", {}) or {}


def get_psoperator_config() -> Dict[str, Any]:
    return load_config().get("psoperator", {}) or {}
