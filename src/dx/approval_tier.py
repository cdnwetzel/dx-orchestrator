"""Which approval a candidate needs — decided from the diff, not declared by
the person asking.

Charter Decision 0020 (signed 2026-09-30) sets three tiers:

``single-reviewer``
    the default in team mode. The author may sign their own task's candidate;
    the SIGNED and MERGED rows then carry
    ``sod_exception=author≠reviewer (author signed; scope single-reviewer)``.

``two-human``
    today's rule, signer ≠ author. Applies to any scope declared ``two-human``
    in the executor role's declaration, and — whatever the declaration — to
    any candidate whose chain diff touches the scope's **exec surface**: the
    files that decide what "tests passed" means.

``control-plane``
    the candidate touches a path RL-008 reserves for human commits. Not
    signable; the merge gate appends a REDLINE row and stops.

Everything here is read-only and pure given its inputs, so ``dx tier`` can
answer the same question ``dx merge`` enforces without touching the ledger.

Fail-closed rules, each one a way the check could otherwise wrongly pass:

* no declaration configured → two-human; a missing default → two-human;
* no ``--repo`` → two-human, because the chain diff cannot be read;
* a repo that is not a declared scope → two-human;
* a path that cannot be classified as repo-relative (absolute, ``..``) is
  treated as exec surface, never as harmless.
"""
from __future__ import annotations

import fnmatch
import json
import posixpath
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pxx.protected_paths import PROTECTED_PREFIXES

SINGLE_REVIEWER = "single-reviewer"
TWO_HUMAN = "two-human"
CONTROL_PLANE = "control-plane"
DECLARABLE_TIERS = (SINGLE_REVIEWER, TWO_HUMAN)

SOD_EXCEPTION = "author≠reviewer (author signed; scope single-reviewer)"

#: Decision 0020 §3. A bare name matches that basename at any depth (a
#: ``conftest.py`` three directories down still decides what tests mean); an
#: entry ending in ``/`` matches that directory name as any path segment; a
#: glob matches the basename. pxx's own protected prefixes are appended.
EXEC_SURFACE: tuple[str, ...] = (
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "Makefile",
    "*.mk",
    "justfile",
    ".pre-commit-config.yaml",
    ".github/workflows/",
    "tests/",
    "requirements*",
    "pxx.toml",
)

#: RL-008's admission list, verbatim: paths the control plane keeps for human
#: commits. Matched anchored at the repo root.
CONTROL_PLANE_PATHS: tuple[str, ...] = (
    "harness/",
    "roles/",
    "CODEOWNERS",
    "red-lines.md",
    "ledger*",
    "queue/",
    ".claude*",
)

GIT_TIMEOUT_S = 60


class DeclarationError(RuntimeError):
    """The scope declaration is missing, malformed, or names a scope that
    does not exist."""


def normalize(path: str) -> str | None:
    """Repo-relative POSIX path, or None when it cannot be classified.

    Mirrors pxx.protected_paths: backslashes to slashes, ONE leading ``./``
    stripped (never a character-set strip), anything absolute or escaping the
    root is unclassifiable.
    """
    p = path.replace("\\", "/").strip()
    if p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or p.startswith("../"):
        return None
    norm = posixpath.normpath(p)
    if norm.startswith("../") or norm == ".." or norm.startswith("/"):
        return None
    return norm


def _matches(rel: str, pattern: str, *, anchored: bool) -> bool:
    """A ``dir/`` pattern matches anything beneath that directory — at the root
    when anchored, at any depth otherwise. A file pattern is a glob over the
    whole path when anchored, over the basename otherwise."""
    if pattern.endswith("/"):
        name = pattern[:-1]
        if anchored:
            return rel.startswith(name + "/")
        parents = rel.split("/")[:-1]
        return name in parents
    if anchored:
        return fnmatch.fnmatchcase(rel, pattern)
    return fnmatch.fnmatchcase(posixpath.basename(rel), pattern)


def classify(path: str) -> str | None:
    """``control-plane``, ``exec-surface`` or None for an ordinary path."""
    rel = normalize(path)
    if rel is None:
        return "exec-surface"  # unclassifiable is never harmless
    for pattern in CONTROL_PLANE_PATHS:
        if _matches(rel, pattern, anchored=True):
            return CONTROL_PLANE
    for pattern in EXEC_SURFACE:
        if _matches(rel, pattern, anchored=pattern.startswith(".github/")):
            return "exec-surface"
    for prefix in PROTECTED_PREFIXES:
        if prefix.endswith("/"):
            if rel.startswith(prefix):
                return "exec-surface"
        elif rel == prefix:
            return "exec-surface"
    return None


@dataclass(frozen=True)
class Declaration:
    """The executor role's declaration, resolved: absolute scope roots and the
    tier each declares (or the default)."""
    source: Path
    role: str
    scopes: tuple[str, ...]
    tiers: dict[str, str]
    default: str | None

    def tier_declared_for(self, scope: str) -> str | None:
        return self.tiers.get(scope, self.default)


def _resolve(entry: str, ai_root: Path) -> str:
    resolved = entry.replace("${AI_ROOT}", str(ai_root))
    if "${" in resolved:
        raise DeclarationError(f"unresolved template in scope entry {entry!r}")
    if not resolved.startswith("/"):
        raise DeclarationError(f"scope entry {entry!r} did not resolve to an absolute path")
    return str(Path(resolved).resolve())


def load_declaration(path: Path, role: str, ai_root: Path) -> Declaration:
    """Read ``approval_tier_default`` and ``approval_tier`` from one role of a
    psguard-style ``roles.json``. Every declared scope key must be one of the
    role's ``scope`` entries: a declaration for a scope that does not exist is
    a typo that would otherwise do nothing, silently."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeclarationError(f"cannot read declaration {path}: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get(role), dict):
        raise DeclarationError(f"{path}: no role {role!r}")
    entry = loaded[role]
    raw_scopes = entry.get("scope")
    if not isinstance(raw_scopes, list) or not all(isinstance(s, str) for s in raw_scopes):
        raise DeclarationError(f"{path}: role {role!r} has no `scope` list")
    scopes = tuple(_resolve(s, ai_root) for s in raw_scopes)
    default = entry.get("approval_tier_default")
    if default is not None and default not in DECLARABLE_TIERS:
        raise DeclarationError(
            f"{path}: {role}.approval_tier_default must be one of "
            f"{DECLARABLE_TIERS}, found {default!r}"
        )
    raw_tiers = entry.get("approval_tier") or {}
    if not isinstance(raw_tiers, dict):
        raise DeclarationError(f"{path}: {role}.approval_tier must be an object")
    tiers: dict[str, str] = {}
    for key, value in raw_tiers.items():
        if value not in DECLARABLE_TIERS:
            raise DeclarationError(
                f"{path}: {role}.approval_tier[{key!r}] must be one of "
                f"{DECLARABLE_TIERS}, found {value!r}"
            )
        resolved = _resolve(str(key), ai_root)
        if resolved not in scopes:
            raise DeclarationError(
                f"{path}: {role}.approval_tier names {key!r}, which is not one "
                f"of the role's scope entries — declare a scope that exists"
            )
        tiers[resolved] = str(value)
    return Declaration(source=path, role=role, scopes=scopes, tiers=tiers, default=default)


@dataclass(frozen=True)
class TierDecision:
    tier: str
    reasons: tuple[str, ...]
    scope: str | None
    #: The chain-diff paths that decided a raised tier (exec surface or
    #: control plane), repo-relative. Empty for single-reviewer.
    touched: tuple[str, ...]
    #: How many paths the chain diff held, when it could be read.
    changed: int | None

    @property
    def sod_exception(self) -> str | None:
        return SOD_EXCEPTION if self.tier == SINGLE_REVIEWER else None

    @property
    def signable(self) -> bool:
        return self.tier != CONTROL_PLANE

    def as_json(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "reasons": list(self.reasons),
            "scope": self.scope,
            "touched": list(self.touched),
            "changed": self.changed,
            "sod_exception": self.sod_exception,
            "signable": self.signable,
        }


def decide(
    repo: Path | None,
    changed_paths: list[str] | None,
    declaration: Declaration | None,
) -> TierDecision:
    """The tier for a candidate, from its repo, its chain diff and the
    declaration. Pure: no ledger, no git."""
    if declaration is None:
        return TierDecision(TWO_HUMAN, ("no approval declaration is configured "
                                        "(manifest `approval:`); every merge is two-human",),
                            None, (), None)
    if repo is None:
        return TierDecision(TWO_HUMAN, ("no --repo: the chain diff cannot be inspected, "
                                        "so the exec surface cannot be ruled out",),
                            None, (), None)
    root = str(Path(repo).resolve())
    if root not in declaration.scopes:
        return TierDecision(TWO_HUMAN, (f"{root} is not a scope declared for role "
                                        f"{declaration.role!r} in {declaration.source}",),
                            None, (), None)
    if changed_paths is None:
        return TierDecision(TWO_HUMAN, ("the chain diff could not be read from the "
                                        "repository",), root, (), None)
    control = tuple(p for p in changed_paths if classify(p) == CONTROL_PLANE)
    if control:
        return TierDecision(CONTROL_PLANE,
                            (f"the chain diff touches control-plane path(s) "
                             f"(RL-008): {', '.join(control)}",),
                            root, control, len(changed_paths))
    exec_surface = tuple(p for p in changed_paths if classify(p) == "exec-surface")
    if exec_surface:
        return TierDecision(TWO_HUMAN,
                            (f"the chain diff touches the exec surface: "
                             f"{', '.join(exec_surface)}",),
                            root, exec_surface, len(changed_paths))
    declared = declaration.tier_declared_for(root)
    if declared is None:
        return TierDecision(TWO_HUMAN, (f"no tier declared for {root} and no "
                                        f"approval_tier_default in {declaration.source}",),
                            root, (), len(changed_paths))
    if declared == TWO_HUMAN:
        how = "declared" if root in declaration.tiers else "the default"
        return TierDecision(TWO_HUMAN, (f"scope is two-human ({how})",),
                            root, (), len(changed_paths))
    how = "declared" if root in declaration.tiers else "the default"
    return TierDecision(SINGLE_REVIEWER,
                        (f"scope is single-reviewer ({how}); the chain diff "
                         f"({len(changed_paths)} path(s)) touches no exec-surface "
                         f"or control-plane path",),
                        root, (), len(changed_paths))


def chain_root_base(task_id: str, ledger_repo: Path) -> tuple[str | None, list[str]]:
    """The base commit of the FIRST task in this task's supersede chain, and
    the chain in order. A rework's own base is the previous candidate, so its
    diff shows only the last correction; what a signer approves is the whole
    deliverable since the family began. Read from queue files; a break in the
    chain returns what was found."""
    chain = [task_id]
    cur: str = task_id
    base: str | None = None
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        qf = ledger_repo / "queue" / f"{cur}.json"
        try:
            q = json.loads(qf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            break
        if not isinstance(q, dict):
            break
        b = q.get("base_sha")
        if isinstance(b, str) and b:
            base = b
        prior = q.get("supersedes")
        if isinstance(prior, str) and prior and prior not in seen:
            chain.append(prior)
            cur = prior
        else:
            break
    chain.reverse()
    return base, chain


def chain_changed_paths(repo: Path, root_base: str | None, candidate: str) -> list[str] | None:
    """Every path the chain diff touches, or None when git could not say."""
    if root_base:
        args = ["diff", "--no-renames", "--name-only", f"{root_base}..{candidate}"]
    else:
        args = ["show", "--no-renames", "--name-only", "--format=", candidate]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


__all__ = [
    "CONTROL_PLANE",
    "CONTROL_PLANE_PATHS",
    "DECLARABLE_TIERS",
    "EXEC_SURFACE",
    "SINGLE_REVIEWER",
    "SOD_EXCEPTION",
    "TWO_HUMAN",
    "Declaration",
    "DeclarationError",
    "TierDecision",
    "chain_changed_paths",
    "chain_root_base",
    "classify",
    "decide",
    "load_declaration",
    "normalize",
]
