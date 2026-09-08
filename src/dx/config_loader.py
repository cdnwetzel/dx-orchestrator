import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(RuntimeError):
    """The hardware manifest is malformed.

    The manifest is hand-edited by design, so a wrong shape is an ordinary
    operator mistake rather than an exotic case. dx reports it and stops; it
    never guesses at what was meant.
    """


def _as_mapping(value: object, what: str, path: Path) -> dict[str, Any]:
    """Return `value` as a mapping, or raise ConfigError naming the offender."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"{path}: expected `{what}` to be a mapping, found "
            f"{type(value).__name__}. Check the indentation — a leading '-' "
            f"makes a list where dx expects `key: value` pairs."
        )
    return value


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
        try:
            loaded = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML — {exc}") from exc

    _config = _as_mapping(loaded, "the manifest", path)
    _config_source = path
    return _config


def get_model_endpoint_for_role(role_slug: str) -> str:
    return get_route_for_role(role_slug).endpoint


def get_route_for_role(role_slug: str) -> RoleRoute:
    """Return the endpoint/model/provider for a role, falling back to `default`."""
    cfg = load_config()
    path = get_config_path()
    roles = _as_mapping(cfg.get("roles"), "roles", path)
    default_cfg = _as_mapping(roles.get("default"), "roles.default", path)
    default_ep = default_cfg.get("endpoint", "http://localhost:11434")
    default_model = default_cfg.get("model")
    default_provider = default_cfg.get("provider")

    role_cfg = _as_mapping(roles.get(role_slug), f"roles.{role_slug}", path)
    return RoleRoute(
        endpoint=role_cfg.get("endpoint") or default_ep,
        model=role_cfg.get("model") or default_model,
        provider=role_cfg.get("provider") or default_provider,
    )


def endpoint_warnings(cfg: dict[str, Any] | None = None) -> list[str]:
    """Non-fatal problems with configured endpoints.

    The trailing-`/v1` mistake is the most-documented failure mode in this
    project — a tutorial section, a troubleshooting row, a comment in the seeded
    manifest — and nothing checked for it. pxx appends its own suffix per
    provider, so `http://host:8000/v1` is probed as `/v1/v1/models`, returns
    404, and every task fails with MODEL_UNAVAILABLE. Documenting a footgun is
    not the same as removing it.
    """
    cfg = load_config() if cfg is None else cfg
    path = get_config_path()
    warnings: list[str] = []

    roles = _as_mapping(cfg.get("roles"), "roles", path)
    for slug in sorted(roles):
        entry = _as_mapping(roles.get(slug), f"roles.{slug}", path)
        endpoint = str(entry.get("endpoint") or "")
        if endpoint.rstrip("/").endswith("/v1"):
            warnings.append(
                f"roles.{slug}.endpoint ends with /v1 ({endpoint}). pxx appends "
                f"its own suffix, so this is probed as /v1/v1/models and 404s. "
                f"Strip the /v1."
            )
        if endpoint and not endpoint.startswith(("http://", "https://")):
            warnings.append(
                f"roles.{slug}.endpoint has no scheme ({endpoint}). "
                f"Prefix it with http:// or https://."
            )

    gui = _as_mapping(cfg.get("gui_verification"), "gui_verification", path)
    vlm = str(gui.get("vlm_endpoint") or "")
    if vlm and not vlm.startswith(("http://", "https://")):
        warnings.append(
            f"gui_verification.vlm_endpoint has no scheme ({vlm})."
        )
    return warnings


def validate_manifest() -> None:
    """Load the manifest and check every section dx reads.

    `dx doctor` uses this so a green self-test means the config is actually
    usable. Checking only the section a given command happens to touch let a
    malformed role entry pass doctor and fail on the first `dx run` that
    selected it.
    """
    path = get_config_path()
    cfg = load_config(force=True)
    roles = _as_mapping(cfg.get("roles"), "roles", path)
    for slug in roles:
        _as_mapping(roles.get(slug), f"roles.{slug}", path)
    _as_mapping(cfg.get("gui_verification"), "gui_verification", path)
    _as_mapping(cfg.get("psoperator"), "psoperator", path)


def get_gui_config() -> dict[str, Any]:
    return _as_mapping(
        load_config().get("gui_verification"), "gui_verification", get_config_path()
    )


def get_psoperator_config() -> dict[str, Any]:
    return _as_mapping(
        load_config().get("psoperator"), "psoperator", get_config_path()
    )


# ---------------------------------------------------------------------------
# External repo locations
# ---------------------------------------------------------------------------

DEFAULT_LEDGER_REPO = Path("~/ai/devswarm-ledger-reference").expanduser()
DEFAULT_ROLES_PATH = Path("~/ai/sdlc-agent-roles/skills/sdlc-role/roles").expanduser()


def get_ledger_repo_path() -> Path:
    """Path to a local clone of a devswarm-ledger-format repository.

    Defaults to ~/ai/devswarm-ledger-reference — the public reference ledger,
    which setup_dependencies.sh clones and which exercises every RL-003 gate
    with synthetic rows. Point DX_LEDGER_REPO at your own operational ledger to
    gate real merges; the reference ledger attests to no real work.
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
    """Directory holding the sdlc-agent-roles role cards.

    Resolution order:
      1. DX_ROLES_PATH environment variable
      2. `roles_path:` at the top level of the hardware manifest
      3. ~/ai/sdlc-agent-roles/skills/sdlc-role/roles (setup_dependencies.sh default)

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
