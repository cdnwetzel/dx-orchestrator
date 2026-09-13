#!/usr/bin/env python3
"""Generate a dx hardware manifest from the shared role->tier template and a
per-box fleet binding.

    manifest  =  template (tracked, address-free)  x  binding (per box, untracked)

Why this exists: a hand-maintained manifest drifts silently — it once carried 6
of 40 roles and a pre-0.9.1 ``screenshot_cmd`` and nothing could tell. A generated
manifest makes both impossible: every deck card must resolve to a tier, and the
output is stamped with the binding's digest so ``dx doctor`` can tell a
generated file from a hand-edited one.

Design contracts (all fail closed):
  * A malformed binding raises and writes nothing — never a half-manifest. An
    *unreachable* endpoint is fine to generate (that's runtime); an *unparseable*
    binding is not.
  * A tier a role needs but the binding does not define is a hard error.
  * A tier may declare itself ``unmapped`` (no model in this fleet) — the role
    then falls to ``default``, but the omission is *declared with a reason*, not
    silent. Legal roles on a fleet with no legal model take this path.
  * A tier may declare ``governed: false`` with a ``reason`` — an interim,
    unaudited route that names itself rather than hiding. (The RL-010
    marked-fallback pattern, applied to routing.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = REPO_ROOT / "config" / "manifest.template.yml"


class BindingError(Exception):
    """The fleet binding is structurally invalid — missing a router, a tier a
    role needs, or a required field. Raised before anything is written."""


def _load_yaml(path: Path, what: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BindingError(f"cannot read {what} at {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BindingError(f"{what} at {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise BindingError(f"{what} at {path} must be a mapping, got {type(data).__name__}")
    return data


def binding_digest(binding: dict[str, Any]) -> str:
    """A stable sha256 over the binding's content — order-independent — so a
    generated manifest can be checked against the binding it claims to come from.
    A binding that will not canonicalise (mixed-type keys, an unquoted YAML date)
    is a malformed binding, raised as such rather than an uncaught TypeError."""
    try:
        canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise BindingError(f"binding is not serialisable (mixed-type keys or an unquoted date?): {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _req_str(value: Any, what: str) -> str:
    """A value that MUST be a non-empty string. An int endpoint like ``123`` would
    otherwise stringify and reach PXX_BASE_URL as ``"123"``; refuse it here."""
    if not isinstance(value, str) or not value.strip():
        raise BindingError(f"{what} must be a non-empty string, got {value!r}")
    return value


def _opt_bool(value: Any, what: str, default: bool) -> bool:
    """A boolean control that, if present, must be a REAL bool — never a quoted
    string. ``governed: \"false\"`` is truthy and would silently mark a tier
    governed; ``unmapped: \"false\"`` would silently unmap it. Refuse the coercion."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BindingError(f"{what} must be a real boolean (unquoted true/false), got {value!r}")
    return value


def _resolve_tier(tier: str, binding: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one tier to a concrete route, or None if the binding declares the
    tier ``unmapped``. Every field is validated before it can reach the manifest;
    a malformed tier raises :class:`BindingError` rather than emitting bad routing."""
    tiers = binding.get("tiers")
    if not isinstance(tiers, dict):
        raise BindingError("binding has no 'tiers' mapping")
    cfg = tiers.get(tier)
    if not isinstance(cfg, dict):
        raise BindingError(
            f"binding does not define tier {tier!r}; every tier the template uses "
            "must be bound (or explicitly marked unmapped)"
        )
    if _opt_bool(cfg.get("unmapped"), f"tier {tier!r} 'unmapped'", False):
        # Declared, not silent: an unmapped tier MUST say why (it becomes the
        # disclosure an examiner reads). A blank reason is not a disclosure.
        _req_str(cfg.get("reason"), f"tier {tier!r} is unmapped and must give a 'reason'")
        return None
    # A tier inherits the router only for fields it does not set itself, so a
    # routerless multi-node fleet (every tier explicit) is valid, and a
    # single-router fleet stays terse. Local values are validated before
    # inheritance, inherited values after.
    endpoint = _req_str(cfg["endpoint"], f"tier {tier!r} endpoint") if cfg.get("endpoint") is not None else None
    provider = _req_str(cfg["provider"], f"tier {tier!r} provider") if cfg.get("provider") is not None else None
    if endpoint is None or provider is None:
        router = binding.get("router")
        if isinstance(router, dict):
            if endpoint is None and router.get("url") is not None:
                endpoint = _req_str(router["url"], "router.url")
            if provider is None and router.get("provider") is not None:
                provider = _req_str(router["provider"], "router.provider")
    if endpoint is None:
        raise BindingError(f"tier {tier!r} has no endpoint and no router.url to inherit")
    if provider is None:
        raise BindingError(f"tier {tier!r} has no provider and no router.provider to inherit")
    model = _req_str(cfg.get("model"), f"tier {tier!r} model")
    governed = _opt_bool(cfg.get("governed"), f"tier {tier!r} 'governed'", True)
    reason = cfg.get("reason")
    if not governed:
        # An ungoverned route names itself; "no reason given" is not a name.
        reason = _req_str(reason, f"tier {tier!r} is governed:false and must give a 'reason'")
    return {
        "endpoint": endpoint,
        "provider": provider,
        "model": model,
        "governed": governed,
        "reason": reason,
        "force_only": _opt_bool(cfg.get("force_only"), f"tier {tier!r} 'force_only'", False),
    }


def generate(template: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    """Build the manifest dict from template x binding. Pure; raises on any
    malformed binding before returning anything."""
    roles_tiers = template.get("roles")
    if not isinstance(roles_tiers, dict):
        raise BindingError("template has no 'roles' mapping")
    default_tier = template.get("default_tier")
    if not default_tier:
        raise BindingError("template has no 'default_tier'")

    # A box may deliberately re-tier a role for its hardware — declared here, never
    # silent. The override tier must exist in the binding, and the role must be a
    # real template role (a typo cannot re-route a card into oblivion).
    overrides = binding.get("role_overrides")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):  # a falsey [] / "" must fail, not become {}
        raise BindingError("role_overrides must be a mapping of role -> tier")
    unknown = set(overrides) - set(roles_tiers)
    if unknown:
        raise BindingError(f"role_overrides names roles not in the template: {sorted(unknown)}")
    for role, tier in overrides.items():
        _req_str(tier, f"role_overrides[{role!r}] tier")

    resolved: dict[str, dict[str, Any]] = {}
    unmapped: dict[str, str] = {}       # role -> reason (declared fall-through to default)
    ungoverned: dict[str, str] = {}     # tier -> reason (interim unaudited)

    # Resolve each tier once, so a tier's posture is reported per tier not per role.
    tier_cache: dict[str, dict[str, Any] | None] = {}

    def route_for(tier: str) -> dict[str, Any] | None:
        if tier not in tier_cache:
            r = _resolve_tier(tier, binding)
            tier_cache[tier] = r
            if r is not None and not r["governed"]:
                ungoverned[tier] = r["reason"]  # guaranteed non-empty by _resolve_tier
        return tier_cache[tier]

    for role, template_tier in roles_tiers.items():
        tier = str(overrides.get(role, template_tier))
        route = route_for(tier)
        if route is None:
            unmapped[role] = _req_str(  # guaranteed present, re-read for the record
                binding["tiers"][tier].get("reason"), f"tier {tier!r} unmapped reason"
            )
            continue
        note = tier if tier == str(template_tier) else f"{tier} (override of {template_tier})"
        if route["force_only"]:
            note += " [--force-only: Anchored gate is the control, not the model]"
        if not route["governed"]:
            note += f" [ungoverned: {route['reason']}]"
        resolved[role] = {
            "endpoint": route["endpoint"],
            "provider": route["provider"],
            "model": route["model"],
            "description": note,
        }

    default_route = route_for(str(default_tier))
    if default_route is None:
        raise BindingError(f"default_tier {default_tier!r} must not be unmapped")
    resolved["default"] = {
        "endpoint": default_route["endpoint"],
        "provider": default_route["provider"],
        "model": default_route["model"],
        "description": f"default ({default_tier})",
    }

    manifest: dict[str, Any] = {
        "_generated": {
            "by": "scripts/gen_manifest.py",
            "binding_sha256": binding_digest(binding),
            "ungoverned_tiers": ungoverned,          # tier -> reason (dx doctor surfaces these)
            "unmapped_roles": unmapped,              # role -> reason (fell to default, declared)
            "role_overrides": {r: str(t) for r, t in overrides.items()},  # deliberate re-tiering
        },
        "roles": resolved,
    }
    for section in ("gui_verification", "psoperator"):
        if section in binding:
            value = binding[section]
            if not isinstance(value, dict):
                # config_loader/_as_mapping would reject a scalar or list later;
                # fail here so a malformed binding never becomes a written manifest.
                raise BindingError(f"binding {section!r} must be a mapping, got {type(value).__name__}")
            manifest[section] = value
    return manifest


_HEADER = """\
# GENERATED by scripts/gen_manifest.py — do NOT hand-edit.
# manifest = config/manifest.template.yml (role->tier)  x  fleet binding (this box).
# binding-sha256: {digest}
# To change routing: edit the binding and regenerate. The digest above identifies
# the binding this file came from; a future dx doctor check will use it to flag a
# hand-edit (drift detection not yet enforced).
{governance}"""


def _governance_header(manifest: dict[str, Any]) -> str:
    g = manifest["_generated"]
    lines = []
    for tier, reason in g["ungoverned_tiers"].items():
        lines.append(f"#   UNGOVERNED tier {tier}: {reason}")
    for role, reason in g["unmapped_roles"].items():
        lines.append(f"#   unmapped -> default: {role} ({reason})")
    for role, tier in g.get("role_overrides", {}).items():
        lines.append(f"#   re-tiered: {role} -> {tier} (override of template)")
    return ("\n".join(lines) + "\n") if lines else ""


def render(manifest: dict[str, Any]) -> str:
    body = yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False, allow_unicode=True)
    header = _HEADER.format(
        digest=manifest["_generated"]["binding_sha256"],
        governance=_governance_header(manifest),
    )
    return header + body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a dx hardware manifest from template x binding.")
    ap.add_argument("--binding", required=True, type=Path, help="per-box fleet binding YAML")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="role->tier template")
    ap.add_argument("--out", type=Path, help="write here (default: stdout)")
    args = ap.parse_args(argv)

    try:
        template = _load_yaml(args.template, "template")
        binding = _load_yaml(args.binding, "binding")
        manifest = generate(template, binding)
    except BindingError as exc:
        print(f"gen_manifest: {exc}", file=sys.stderr)
        return 2  # fail loud, write nothing

    text = render(manifest)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        g = manifest["_generated"]
        print(f"wrote {args.out}  ({len(manifest['roles'])} roles, binding {g['binding_sha256'][:12]}…)")
        if g["ungoverned_tiers"]:
            print(f"  ⚠️  ungoverned tiers: {', '.join(g['ungoverned_tiers'])}")
        if g["unmapped_roles"]:
            print(f"  ℹ️  unmapped→default: {', '.join(g['unmapped_roles'])}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
