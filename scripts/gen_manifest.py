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
    generated manifest can be checked against the binding it claims to come from."""
    canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_tier(tier: str, binding: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one tier to a concrete route, or None if the binding declares the
    tier ``unmapped``. Raises :class:`BindingError` if the tier is undefined or
    missing a model."""
    tiers = binding.get("tiers")
    if not isinstance(tiers, dict):
        raise BindingError("binding has no 'tiers' mapping")
    cfg = tiers.get(tier)
    if not isinstance(cfg, dict):
        raise BindingError(
            f"binding does not define tier {tier!r}; every tier the template uses "
            "must be bound (or explicitly marked unmapped)"
        )
    if cfg.get("unmapped"):
        return None  # declared, not silent — the caller records the reason
    router = binding.get("router")
    if not isinstance(router, dict):
        raise BindingError("binding has no 'router' mapping (needed unless every tier overrides endpoint)")
    endpoint = cfg.get("endpoint") or router.get("url")
    provider = cfg.get("provider") or router.get("provider")
    model = cfg.get("model")
    if not endpoint:
        raise BindingError(f"tier {tier!r} has no endpoint and router.url is unset")
    if not provider:
        raise BindingError(f"tier {tier!r} has no provider and router.provider is unset")
    if not model:
        raise BindingError(f"tier {tier!r} is mapped but has no model")
    return {
        "endpoint": str(endpoint),
        "provider": str(provider),
        "model": str(model),
        "governed": bool(cfg.get("governed", True)),
        "reason": cfg.get("reason"),
        "force_only": bool(cfg.get("force_only", False)),
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
                ungoverned[tier] = r.get("reason") or "ungoverned route (no reason given)"
        return tier_cache[tier]

    for role, tier in roles_tiers.items():
        route = route_for(str(tier))
        if route is None:
            reason = binding["tiers"][tier].get("reason") or "tier unmapped in this fleet"
            unmapped[role] = reason
            continue
        note = str(tier)
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
        },
        "roles": resolved,
    }
    for section in ("gui_verification", "psoperator"):
        if section in binding:
            manifest[section] = binding[section]
    return manifest


_HEADER = """\
# GENERATED by scripts/gen_manifest.py — do NOT hand-edit.
# manifest = config/manifest.template.yml (role->tier)  x  fleet binding (this box).
# binding-sha256: {digest}
# To change routing: edit the binding and regenerate. dx doctor warns if this
# file no longer matches the binding it was generated from.
{governance}"""


def _governance_header(manifest: dict[str, Any]) -> str:
    g = manifest["_generated"]
    lines = []
    for tier, reason in g["ungoverned_tiers"].items():
        lines.append(f"#   UNGOVERNED tier {tier}: {reason}")
    for role, reason in g["unmapped_roles"].items():
        lines.append(f"#   unmapped -> default: {role} ({reason})")
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
