import re
from pathlib import Path
from typing import Any

import yaml

from .role_models import FitLevel, RoleCard


class RoleParseError(Exception):
    pass


_SECTION_RE = re.compile(r"##\s*(.+?)\s*\n(.*?)(?=\n## |\Z)", re.DOTALL)

# One Markdown list item. Role cards wrap long prohibitions across several
# lines, so a bullet is "a line that starts one" plus any indented continuation.
_BULLET_START_RE = re.compile(r"^\s*[-*\u2022]\s+(.*)$")
_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*")

# Inline metadata line style used by claude-sdlc-roles:
#   **Slug:** `slug` · **Phase:** X · **Agent fit:** Y · **9-person seat:** Z
_FIT_RE = re.compile(r"\*\*Agent fit:\*\*\s*([A-Za-z]+)", re.IGNORECASE)
_SEAT_RE = re.compile(r"\*\*9-person seat:\*\*\s*([^\n·|]+)")


def _extract_bullets(text: str) -> list[str]:
    """Split a Markdown bullet list into one entry per item.

    Treating every line as its own bullet — which a naive `^[\\s\\-*]+` pattern
    does — split wrapped prohibitions into fragments. A real card's four-item
    "Must not" section yielded six entries, two of which ("argue past it.") were
    sentence tails rather than prohibitions, and the emphasis markers were half
    eaten. `prohibited_patterns` exists to be matched against agent behavior, so
    the items have to be whole.
    """
    items: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _BULLET_START_RE.match(line)
        if match:
            items.append(match.group(1).strip())
        elif items:
            # An indented continuation of the bullet above it.
            items[-1] = f"{items[-1]} {line.strip()}"
    return [_EMPHASIS_RE.sub(r"\1", item).strip() for item in items if item.strip()]


def _normalize_key(title: str) -> str:
    key = title.strip().lower().replace(" ", "_")
    return re.sub(r"[^a-z_]", "", key)


def _match_section(sections: dict[str, str], *prefixes: str) -> str:
    """Return the first section whose normalized key equals or starts with a prefix."""
    for p in prefixes:
        if p in sections:
            return sections[p]
    for key, value in sections.items():
        for p in prefixes:
            if key.startswith(p):
                return value
    return ""


def parse_role_file(filepath: Path) -> RoleCard:
    content = filepath.read_text(encoding="utf-8")
    slug = filepath.stem

    # Optional YAML frontmatter (not used by claude-sdlc-roles today, but supported)
    frontmatter: dict[str, Any] = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                loaded = yaml.safe_load(parts[1])
                if isinstance(loaded, dict):
                    frontmatter = loaded
            except yaml.YAMLError:
                pass
            body = parts[2].strip()

    sections: dict[str, str] = {}
    for title, text in _SECTION_RE.findall(body):
        sections[_normalize_key(title)] = text.strip()

    # Fit: prefer frontmatter, else parse the inline **Agent fit:** metadata line
    fit_str = frontmatter.get("fit")
    if not fit_str:
        m = _FIT_RE.search(body)
        if m:
            fit_str = m.group(1).strip()
    if not fit_str:
        fit_str = "Partial"
    try:
        fit_level = FitLevel(fit_str)
    except ValueError:
        fit_level = FitLevel.PARTIAL

    # Seat: prefer frontmatter, else parse the inline **9-person seat:** metadata
    seat = frontmatter.get("seat")
    if not seat:
        m = _SEAT_RE.search(body)
        if m:
            seat = m.group(1).strip()
    if not seat:
        seat = "Borrowed"

    must_not = _match_section(sections, "must_not", "mustnot")

    return RoleCard(
        slug=slug,
        fit=fit_level,
        seat=seat,
        anchored=(fit_level == FitLevel.ANCHORED),
        mandate=_match_section(sections, "mandate"),
        inputs_required=_match_section(sections, "inputs_required", "inputsrequired"),
        outputs=_match_section(sections, "outputs"),
        operating_checklist=_match_section(
            sections, "operating_checklist", "operatingchecklist"
        ),
        definition_of_done=_match_section(
            sections, "definition_of_done", "definitionofdone"
        ),
        must_not=must_not,
        failure_modes=_match_section(sections, "failure_modes", "failuremodes"),
        handoff=_match_section(sections, "handoff"),
        related=_match_section(sections, "related"),
        prohibited_patterns=_extract_bullets(must_not),
    )
