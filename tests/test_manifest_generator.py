"""The manifest generator: template x binding -> hardware manifest.

The point of generating the manifest rather than hand-maintaining it is that two
drifts become impossible: a role card with no tier (the 6-of-40 drift), and a
hand-edit that silently diverges from the binding (caught by the stamped digest).
These tests pin that, plus the three fail-closed contracts: a malformed binding
writes nothing, an undefined tier is an error, and an ungoverned/unmapped tier is
*declared with a reason* rather than silently absent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "config" / "manifest.template.yml"
REAL_CARDS = Path("~/ai/sdlc-agent-roles/skills/sdlc-role/roles").expanduser()

# Load scripts/gen_manifest.py (not an installed module) by path.
_spec = importlib.util.spec_from_file_location("gen_manifest", REPO_ROOT / "scripts" / "gen_manifest.py")
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _template() -> dict:
    return yaml.safe_load(TEMPLATE.read_text())


def _binding(**tier_overrides) -> dict:
    tiers = {
        "HEAVY": {"model": "heavy-m"},
        "CODE": {"model": "code-m"},
        "FAST": {"model": "fast-m"},
        "VISION": {"model": "vision-m"},
        "LEGAL_DEEP": {"model": "legal-deep-m", "force_only": True},
        "LEGAL_GEN": {"model": "legal-gen-m", "force_only": True},
        "DEFAULT": {"endpoint": "http://local.invalid:11434", "provider": "ollama", "model": "fallback-m"},
    }
    tiers.update(tier_overrides)
    return {
        "version": 1,
        "router": {"url": "http://router.invalid:8888", "provider": "openai-compatible"},
        "tiers": tiers,
    }


# --- the anti-drift guard: every card resolves to a tier ---------------------


@pytest.mark.skipif(not REAL_CARDS.is_dir(), reason="sdlc-agent-roles not cloned; CI clones it")
def test_every_deck_card_resolves_to_a_tier():
    # The drift that once left 6 of 40 roles mapped: a card with no tier falls
    # through silently. The deck grew 38 -> 40 this week; a new card must fail
    # this test, not vanish onto default.
    template = _template()
    tiered = set(template["roles"])
    deck = {p.stem for p in REAL_CARDS.glob("*.md")}
    missing = deck - tiered
    assert not missing, f"deck cards with no tier in the template: {sorted(missing)}"


def test_template_default_tier_is_defined_and_bound():
    template = _template()
    assert template.get("default_tier"), "template must name a default_tier"
    # and a well-formed binding must define it
    manifest = gen.generate(template, _binding())
    assert manifest["roles"]["default"]["model"] == "fallback-m"


# --- resolution -------------------------------------------------------------


def test_generate_routes_each_tier_to_its_bound_model():
    manifest = gen.generate(_template(), _binding())
    r = manifest["roles"]
    assert r["backend-engineer"]["model"] == "code-m"
    assert r["solution-architect"]["model"] == "heavy-m"
    assert r["ux-designer"]["model"] == "vision-m"
    # a tier with no endpoint override inherits the router url
    assert r["backend-engineer"]["endpoint"] == "http://router.invalid:8888"


def test_force_only_legal_is_noted_not_hidden():
    manifest = gen.generate(_template(), _binding())
    assert "--force-only" in manifest["roles"]["legal-contracts"]["description"]


# --- governance posture: declared, not silent -------------------------------


def test_an_unmapped_tier_drops_its_roles_to_default_with_a_reason():
    b = _binding(LEGAL_DEEP={"unmapped": True, "reason": "no legal model here"})
    manifest = gen.generate(_template(), b)
    assert "legal-contracts" not in manifest["roles"]          # fell to default
    assert manifest["_generated"]["unmapped_roles"]["legal-contracts"] == "no legal model here"


def test_an_ungoverned_tier_is_surfaced_with_its_reason():
    b = _binding(HEAVY={"model": "h", "governed": False, "reason": "interim direct vLLM"})
    manifest = gen.generate(_template(), b)
    assert manifest["_generated"]["ungoverned_tiers"]["HEAVY"] == "interim direct vLLM"
    assert "ungoverned: interim direct vLLM" in manifest["roles"]["solution-architect"]["description"]


# --- the stamped digest -----------------------------------------------------


def test_binding_digest_is_order_independent():
    a = {"router": {"url": "x", "provider": "p"}, "tiers": {"A": {"model": "m"}}}
    b = {"tiers": {"A": {"model": "m"}}, "router": {"provider": "p", "url": "x"}}
    assert gen.binding_digest(a) == gen.binding_digest(b)


def test_generated_manifest_stamps_the_binding_digest():
    b = _binding()
    manifest = gen.generate(_template(), b)
    assert manifest["_generated"]["binding_sha256"] == gen.binding_digest(b)


# --- fail closed ------------------------------------------------------------


def test_a_tier_a_role_needs_but_the_binding_omits_is_an_error():
    b = _binding()
    del b["tiers"]["CODE"]
    with pytest.raises(gen.BindingError, match="CODE"):
        gen.generate(_template(), b)


def test_a_mapped_tier_with_no_model_is_an_error():
    b = _binding(HEAVY={"governed": True})  # no model
    with pytest.raises(gen.BindingError, match="no model"):
        gen.generate(_template(), b)


def test_malformed_binding_writes_nothing_and_exits_2(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("this: [is, not, a, binding\n")  # unparseable YAML
    out = tmp_path / "out.yml"
    rc = gen.main(["--binding", str(bad), "--template", str(TEMPLATE), "--out", str(out)])
    assert rc == 2
    assert not out.exists(), "a malformed binding must not leave a half-written manifest"


def test_round_trip_generates_a_manifest_dx_can_load(tmp_path, monkeypatch):
    out = tmp_path / "hardware_manifest.yml"
    rc = gen.main(["--binding", str(_write(tmp_path, _binding())), "--template", str(TEMPLATE), "--out", str(out)])
    assert rc == 0
    monkeypatch.setenv("DX_CONFIG", str(out))
    from dx import config_loader
    config_loader.load_config(force=True)
    from dx.config_loader import get_route_for_role, validate_manifest
    validate_manifest()  # every section has the expected shape
    assert get_route_for_role("backend-engineer").model == "code-m"
    assert get_route_for_role("legal-contracts").model == "legal-deep-m"


def _write(tmp_path: Path, binding: dict) -> Path:
    p = tmp_path / "binding.yml"
    p.write_text(yaml.safe_dump(binding))
    return p
