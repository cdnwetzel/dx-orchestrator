import datetime as _dt
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    #: The manifest's value, when it differed from ``endpoint`` after
    #: normalisation. Non-None means dx corrected the operator's URL and the
    #: caller is expected to say so out loud.
    endpoint_raw: str | None = None
    #: Seconds pxx may wait on this role's HTTP round trip. Per role, because
    #: the reason for a long wait is per role: a model that is on disk but not
    #: resident pays a cold load on its first call, and a box without the memory
    #: to keep it resident pays that on every first call. Raising the ceiling is
    #: the right answer there -- keep-alive would mean holding memory the box
    #: does not have. None leaves pxx's own default (120s) alone.
    timeout_s: float | None = None


DEFAULT_CONFIG_PATH = Path("~/.config/dx/hardware_manifest.yml").expanduser()


def get_config_path() -> Path:
    """Resolved path to the hardware manifest (DX_CONFIG overrides the default)."""
    return Path(os.environ.get("DX_CONFIG", str(DEFAULT_CONFIG_PATH))).expanduser()


# Cache is keyed on the resolved path: changing DX_CONFIG mid-process must not
# silently return the previously loaded manifest. The snapshot (the digests of
# the bytes the mapping was parsed from) is cached WITH the mapping and
# replaced with it, never one without the other.
_config: dict[str, Any] | None = None
_config_source: Path | None = None
_config_snapshot: "ConfigSnapshot | None" = None


@dataclass(frozen=True)
class ConfigSnapshot:
    """What `load_config` consumed: the digests of the single read the
    mapping in use was parsed from (S3, 2026-10-13).

    `raw_sha256` is over the file bytes as read. `normalized_sha256` is over
    the canonical tagged form of the parsed mapping (`canonical_manifest_json`)
    and is None, with `normalization_error` saying why, when the manifest
    holds a value the canonical form does not support; loading never fails
    on that. Neither digest stores any manifest content: they fingerprint
    the file, they do not reproduce it.
    """

    path: Path
    raw_sha256: str
    normalized_sha256: str | None
    normalization_error: str | None
    size_bytes: int
    read_utc: str

    def as_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "raw_sha256": self.raw_sha256,
            "normalized_sha256": self.normalized_sha256,
            "normalization_error": self.normalization_error,
            "size_bytes": self.size_bytes,
            "read_utc": self.read_utc,
        }


class CanonicalizationError(ValueError):
    """The parsed manifest holds a value the canonical form does not cover."""


def _tag(node: object, path: str, seen: set[int]) -> object:
    """The collision-free tagged tree the normalized digest hashes.

    Every genuine mapping key is emitted as ``k:<key>``; every value JSON
    cannot carry becomes a one-key object whose key starts with ``t:``. A
    manifest whose own key is literally ``t:timestamp`` therefore serialises
    as ``k:t:timestamp`` and can never be read as a tag.
    """
    if isinstance(node, dict):
        if id(node) in seen:
            raise CanonicalizationError(f"cycle at {path or '<root>'}")
        seen.add(id(node))
        out: dict[str, object] = {}
        for key, value in node.items():
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"non-string key {key!r} at {path or '<root>'}"
                )
            out[f"k:{key}"] = _tag(value, f"{path}.{key}" if path else key, seen)
        seen.discard(id(node))
        return out
    if isinstance(node, list):
        if id(node) in seen:
            raise CanonicalizationError(f"cycle at {path or '<root>'}")
        seen.add(id(node))
        items = [_tag(v, f"{path}[{i}]", seen) for i, v in enumerate(node)]
        seen.discard(id(node))
        return items
    if node is None or isinstance(node, (bool, str)):
        return node
    if isinstance(node, int):
        return node
    if isinstance(node, float):
        if math.isnan(node):
            return {"t:float": "nan"}
        if math.isinf(node):
            return {"t:float": "inf" if node > 0 else "-inf"}
        return node
    if isinstance(node, _dt.datetime):
        return {"t:timestamp": node.isoformat()}
    if isinstance(node, _dt.date):
        return {"t:timestamp": node.isoformat()}
    if isinstance(node, (bytes, bytearray)):
        return {"t:binary": {"sha256": hashlib.sha256(bytes(node)).hexdigest(),
                             "len": len(node)}}
    raise CanonicalizationError(
        f"unsupported type {type(node).__name__} at {path or '<root>'}"
    )


def canonical_manifest_json(mapping: dict[str, Any]) -> bytes:
    """Canonical JSON (sorted keys, no spaces, UTF-8, no NaN) of the tagged
    tree of ``mapping``. Raises CanonicalizationError for non-string keys,
    cycles and unsupported types; loading never calls this in a way that
    fails the load."""
    tagged = _tag(mapping, "", set())
    return json.dumps(
        tagged, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def untag_canonical(tagged: object) -> object:
    """Inverse of the tagged form for the round-trip types: mappings, lists,
    str, bool, int, None, finite float and timestamp. Binary and non-finite
    floats are content-addressed, not reversible, and come back as their
    tag objects unchanged."""
    if isinstance(tagged, dict):
        if len(tagged) == 1:
            (key, value), = tagged.items()
            if key == "t:timestamp" and isinstance(value, str):
                try:
                    return _dt.datetime.fromisoformat(value) if "T" in value else _dt.date.fromisoformat(value)
                except ValueError:
                    return tagged
            if key.startswith("t:"):
                return tagged
        out: dict[str, object] = {}
        for key, value in tagged.items():
            if not key.startswith("k:"):
                raise CanonicalizationError(f"not a tagged mapping key: {key!r}")
            out[key[2:]] = untag_canonical(value)
        return out
    if isinstance(tagged, list):
        return [untag_canonical(v) for v in tagged]
    return tagged


def _snapshot_of(path: Path, data: bytes, mapping: dict[str, Any]) -> ConfigSnapshot:
    try:
        normalized: str | None = hashlib.sha256(canonical_manifest_json(mapping)).hexdigest()
        error: str | None = None
    except CanonicalizationError as exc:
        normalized, error = None, str(exc)
    return ConfigSnapshot(
        path=path,
        raw_sha256=hashlib.sha256(data).hexdigest(),
        normalized_sha256=normalized,
        normalization_error=error,
        size_bytes=len(data),
        read_utc=_dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def load_config(force: bool = False) -> dict[str, Any]:
    global _config, _config_source, _config_snapshot

    path = get_config_path()
    if _config is not None and _config_source == path and not force:
        return _config

    if not path.exists():
        raise FileNotFoundError(
            f"Hardware manifest not found at {path}. "
            "Run scripts/setup_dependencies.sh or set DX_CONFIG."
        )

    # One read. The digests and the mapping come from the same bytes, so a
    # snapshot can never describe a different file than the one in use.
    data = path.read_bytes()
    try:
        loaded = yaml.safe_load(data) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc

    mapping = _as_mapping(loaded, "the manifest", path)
    snapshot = _snapshot_of(path, data, mapping)
    _config, _config_source, _config_snapshot = mapping, path, snapshot
    return _config


def get_config_snapshot() -> ConfigSnapshot:
    """The snapshot of the mapping currently in use; loads if nothing is
    cached. Never re-reads the file to answer."""
    load_config()
    assert _config_snapshot is not None  # set together with _config
    return _config_snapshot


def normalize_endpoint(endpoint: str) -> tuple[str, bool]:
    """Strip trailing ``/v1`` path segments. Returns ``(endpoint, changed)``.

    pxx appends its own suffix per provider — ``/api/tags`` for ollama,
    ``/v1/models`` for everything OpenAI-shaped — so a base URL that already
    ends in ``/v1`` is probed as ``/v1/v1/models``, 404s, and every task fails
    with MODEL_UNAVAILABLE. Documenting that footgun in four places did not stop
    people walking into it, because hosted OpenAI-compatible services publish
    their URL *with* the ``/v1`` (OpenRouter, LiteLLM, Together). Pasting the
    vendor's own string is the common case, not a careless mistake.

    There is no configuration in which a trailing ``/v1`` is correct here, so
    correcting it is safe. It is never silent: ``RoleRoute.endpoint_raw``
    carries the original so the caller can announce the change.

    Parsed rather than string-suffixed. ``http://v1`` ends with the characters
    ``/v1`` while its path is empty and its *host* is ``v1``; a naive
    ``endswith`` strip turns it into ``http:/``.
    """
    parts = urlsplit(endpoint)
    if not parts.scheme or not parts.netloc:
        # Not a URL we understand (no scheme, or a bare host). Leave it alone —
        # endpoint_warnings() already flags a missing scheme.
        return endpoint, False

    segments = [s for s in parts.path.split("/") if s]
    changed = False
    while segments and segments[-1].lower() == "v1":
        segments.pop()
        changed = True
    if not changed:
        return endpoint, False

    new_path = "/" + "/".join(segments) if segments else ""
    return (
        urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)),
        True,
    )


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
    configured = str(role_cfg.get("endpoint") or default_ep)
    endpoint, corrected = normalize_endpoint(configured)
    raw_timeout = role_cfg.get("timeout_s", default_cfg.get("timeout_s"))
    timeout_s: float | None = None
    if raw_timeout is not None:
        try:
            parsed = float(raw_timeout)
        except (TypeError, ValueError):
            parsed = -1.0
        # Reject non-positive/non-finite rather than passing them on: pxx warns
        # and falls back to its default anyway, and a silently wrong timeout on
        # the wrong knob is worse than the default (pxx _timeout_from_env).
        timeout_s = parsed if parsed > 0 and math.isfinite(parsed) else None

    return RoleRoute(
        endpoint=endpoint,
        model=role_cfg.get("model") or default_model,
        provider=role_cfg.get("provider") or default_provider,
        endpoint_raw=configured if corrected else None,
        timeout_s=timeout_s,
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
                f"roles.{slug}.endpoint ends with /v1 ({endpoint}). dx strips "
                f"it at run time — pxx appends its own suffix, so this would be "
                f"probed as /v1/v1/models and 404 — but fix the manifest so the "
                f"file says what actually runs."
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
    _as_mapping(cfg.get("ledger"), "ledger", path)
    get_review_config()
    get_branches_config()


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


def get_ledger_config() -> dict[str, Any]:
    return _as_mapping(load_config().get("ledger"), "ledger", get_config_path())


def get_ledger_repo_path() -> Path:
    """Path to a local clone of a devswarm-ledger-format repository.

    Resolution order: DX_LEDGER_REPO, then `ledger.repo` in the manifest, then
    ~/ai/devswarm-ledger-reference — matching get_psoperator_repo below.

    The default is the PUBLIC REFERENCE ledger, which exercises every RL-003
    gate against synthetic rows and **attests to no real work**. That makes the
    default a safe starting point and a dangerous resting point: a merge gated
    against it verifies a signature over data nobody did.

    The manifest step exists because env-only resolution fails in the way that
    matters least visibly. An operator exports DX_LEDGER_REPO in one shell, sees
    a green `dx doctor`, and every later shell — a systemd unit, a cron job, a
    bridge service, a new terminal — silently falls back to the reference
    ledger. Nothing errors. `dx merge` just gates real work against synthetic
    history. A file the operator can point at, and that doctor prints, removes a
    class of failure that a correct export cannot.
    """
    env = os.environ.get("DX_LEDGER_REPO")
    if env:
        return Path(env).expanduser()
    try:
        configured = get_ledger_config().get("repo")
    except FileNotFoundError:
        return DEFAULT_LEDGER_REPO
    if configured:
        return Path(str(configured)).expanduser()
    return DEFAULT_LEDGER_REPO


@dataclass(frozen=True)
class ApprovalConfig:
    """The manifest's `approval:` section — where the executor's scope
    declaration lives and which role in it the merge gate reads.

    The declaration is psguard's `roles.json` (charter Decision 0020 §2: the
    executor role's declaration, integrity-manifested). dx does not own that
    file and never writes it; it reads `approval_tier_default` and
    `approval_tier` from the named role, and resolves the role's `${AI_ROOT}`
    scope entries with `ai_root`. Absent section → no declaration → every
    merge is two-human (fail closed), and `dx tier` says why.
    """
    declaration: Path
    role: str
    ai_root: Path


def get_approval_config() -> ApprovalConfig | None:
    """The `approval:` section, or None when the manifest has none.

    Present but malformed is an error, not None: an operator who wrote the
    section meant it, and a typo must not silently become "two-human for
    everything" any more than it may become "single-reviewer for everything".
    """
    try:
        raw = load_config().get("approval")
    except FileNotFoundError:
        return None
    if raw is None:
        return None
    path = get_config_path()
    section = _as_mapping(raw, "approval", path)
    declaration = section.get("declaration")
    role = section.get("role")
    ai_root = section.get("ai_root")
    missing = [k for k, v in (("declaration", declaration), ("role", role), ("ai_root", ai_root)) if not v]
    if missing:
        raise ConfigError(
            f"{path}: `approval` needs {', '.join(missing)} — the declaration "
            f"file (psguard roles.json), the executor role in it, and the "
            f"AI_ROOT its `${{AI_ROOT}}` scope entries resolve against."
        )
    return ApprovalConfig(
        declaration=Path(str(declaration)).expanduser(),
        role=str(role),
        ai_root=Path(str(ai_root)).expanduser(),
    )


#: The review modes dx will send to pxx. ``blocking`` exists in pxx and is
#: deliberately absent here: D-4 (2026-10-02) makes the reviewer advisory until
#: a calibration run has measured what its findings cost in rounds. Lifting
#: that is a one-line change to this tuple, after the calibration, and this is
#: the only place the gate is: ``dx.evidence.render_review_line`` and the AskPS
#: bridge's ``_review_result`` already phrase their closing sentence from the
#: recorded mode, so widening this needs no change there.
REVIEW_MODES: tuple[str, ...] = ("advisory",)
REVIEW_MODE_DEFAULT = "advisory"


@dataclass(frozen=True)
class ReviewConfig:
    """The manifest's `review:` section — whether `dx run` asks pxx for its
    reviewer leg, and in which mode.

    pxx's own default (`loop_review`) never decides: dx passes an explicit
    flag on every run, so the manifest is the one place the choice lives and
    a `PXX_LOOP_REVIEW` in some unit cannot turn the reviewer on or off
    behind the manifest's back. Absent section → off, stated as such in the
    bundle ("no review section"), which reads differently from a reviewer
    that was asked for and did not run.
    """
    enabled: bool
    mode: str = REVIEW_MODE_DEFAULT


def get_review_config() -> ReviewConfig | None:
    """The `review:` section, or None when the manifest has none.

    Present but malformed is an error, not None: a typo must not silently
    become "review off". `validate_manifest` calls this, so `dx doctor`
    fails on a bad section before the first run does.
    """
    try:
        raw = load_config().get("review")
    except FileNotFoundError:
        return None
    if raw is None:
        return None
    path = get_config_path()
    section = _as_mapping(raw, "review", path)
    enabled = section.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"{path}: `review.enabled` must be a boolean (true/false), found "
            f"{enabled!r}. There is no default: say which."
        )
    mode = section.get("mode", REVIEW_MODE_DEFAULT)
    if mode == "blocking":
        raise ConfigError(
            f"{path}: `review.mode: blocking` is not offered by this dx. D-4 "
            f"(2026-10-02) keeps the reviewer advisory — it runs and is "
            f"recorded, never blocks — until a calibration run has measured "
            f"what its findings cost in rounds. Use `mode: advisory`."
        )
    if not isinstance(mode, str) or mode not in REVIEW_MODES:
        raise ConfigError(
            f"{path}: `review.mode` must be one of {', '.join(REVIEW_MODES)}, "
            f"found {mode!r}."
        )
    return ReviewConfig(enabled=enabled, mode=mode)


BRANCH_BASE_DEFAULT = "main"
BRANCH_PREFIX_DEFAULT = "task/"


@dataclass(frozen=True)
class BranchesConfig:
    """The manifest's `branches:` section — whether `dx run` works on a task
    branch and `dx merge` makes a real merge (plan D-3, 2026-10-08; AskPS
    `ps-coding-wp3-design.md`).

    Inline mode (the section absent, or `enabled: false`) is what every run
    did through 0.22.0: pxx commits on whatever branch the scope has checked
    out, so the work is on `main` before anyone signs. Branch mode puts each
    run on `<prefix><task_id>` created at the task's recorded base, tags the
    candidate at EXECUTED, and returns the checkout to `base`; sign-off then
    merges for real. There is no default for `enabled`: a typo must not become
    "inline".
    """
    enabled: bool
    base: str = BRANCH_BASE_DEFAULT
    prefix: str = BRANCH_PREFIX_DEFAULT

    def branch_for(self, task_id: str) -> str:
        return f"{self.prefix}{task_id}"


def _ref_component(value: object, field: str, path: Path) -> str:
    """A branch name or prefix the manifest may name: a non-empty string with
    none of the characters git refuses in a ref. The full check is git's
    (`check-ref-format`), run when the branch is created; this catches the
    manifest typo before the first run does."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"{path}: `branches.{field}` must be a non-empty string, found {value!r}."
        )
    bad = set(" \t\n~^:?*[\\") & set(value)
    if bad or ".." in value or "@{" in value or value.startswith("-") or value.startswith("/"):
        raise ConfigError(
            f"{path}: `branches.{field}` is not a git ref name: {value!r}."
        )
    return value


def get_branches_config() -> BranchesConfig | None:
    """The `branches:` section, or None when the manifest has none (inline
    mode). Present but malformed is an error, not None, on the pattern of
    `get_review_config`; `validate_manifest` calls this, so `dx doctor` fails
    on a bad section before the first run does."""
    try:
        raw = load_config().get("branches")
    except FileNotFoundError:
        return None
    if raw is None:
        return None
    path = get_config_path()
    section = _as_mapping(raw, "branches", path)
    enabled = section.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"{path}: `branches.enabled` must be a boolean (true/false), found "
            f"{enabled!r}. There is no default: say which."
        )
    base = _ref_component(section.get("base", BRANCH_BASE_DEFAULT), "base", path)
    prefix = _ref_component(section.get("prefix", BRANCH_PREFIX_DEFAULT), "prefix", path)
    if base.startswith(prefix):
        raise ConfigError(
            f"{path}: `branches.base` ({base!r}) starts with `branches.prefix` "
            f"({prefix!r}); the base would read as a stray task branch."
        )
    unknown = sorted(set(section) - {"enabled", "base", "prefix"})
    if unknown:
        raise ConfigError(
            f"{path}: `branches` has unknown key(s) {', '.join(unknown)}; "
            f"it takes enabled, base, prefix."
        )
    return BranchesConfig(enabled=enabled, base=base, prefix=prefix)


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
