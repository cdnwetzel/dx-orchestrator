"""dx.approval_tier — the tier is decided from the diff, and every way the
decision could wrongly land on single-reviewer is a case here.

Decision 0020: single-reviewer is the default; two-human on declared scopes
and on any candidate touching the exec surface; control-plane contact is not
signable. Fail closed on everything unknown.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import write_declaration
from dx.approval_tier import (
    CONTROL_PLANE,
    SINGLE_REVIEWER,
    SOD_EXCEPTION,
    TWO_HUMAN,
    Declaration,
    DeclarationError,
    chain_changed_paths,
    chain_root_base,
    classify,
    decide,
    load_declaration,
    normalize,
)

# --- classification ---------------------------------------------------------

EXEC = [
    "conftest.py", "pkg/sub/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg",
    "tox.ini", "noxfile.py", "Makefile", "build/rules.mk", "justfile",
    ".pre-commit-config.yaml", ".github/workflows/ci.yml", "tests/test_x.py",
    "pkg/tests/test_y.py", "docs/tests/notes.md", "requirements.txt",
    "requirements-dev.txt", "pxx.toml", ".pxx/config.toml", "pxx/safety.py",
    "WORKFLOW.md",
]
PLAIN = [
    "src/app.py", "README.md", "docs/.github/workflows/x.yml", "testsuite/a.py",
    "mytests/a.py", "tests", "src/testsupport/helper.py", "x/harness/y",
    "Makefile.md", "requirementsx/y.py",
]
CONTROL = [
    "harness/run.sh", "roles/backend.md", "CODEOWNERS", "red-lines.md",
    "ledger.jsonl", "ledger-2026.jsonl", "queue/T-0001.json", ".claude/settings.json",
    ".claude.json",
]


@pytest.mark.parametrize("path", EXEC)
def test_exec_surface_paths_are_exec_surface(path):
    assert classify(path) == "exec-surface", path


@pytest.mark.parametrize("path", PLAIN)
def test_ordinary_paths_are_ordinary(path):
    assert classify(path) is None, path


@pytest.mark.parametrize("path", CONTROL)
def test_control_plane_paths_are_control_plane(path):
    assert classify(path) == CONTROL_PLANE, path


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "", "a/../../b", "./../c"])
def test_unclassifiable_paths_are_never_harmless(path):
    """Absolute, escaping or empty: not repo-relative, so treated as exec
    surface rather than passed as ordinary."""
    assert normalize(path) is None
    assert classify(path) == "exec-surface"


def test_normalize_strips_one_leading_dot_slash_only():
    assert normalize("./src/a.py") == "src/a.py"
    assert normalize("src/./a.py") == "src/a.py"
    assert normalize("src\\a.py") == "src/a.py"
    # the 1.x pxx bug: a character-set strip unprotected `.github/`
    assert normalize(".github/workflows/ci.yml") == ".github/workflows/ci.yml"


# --- declaration --------------------------------------------------------------

@pytest.fixture
def ai_root(tmp_path: Path) -> Path:
    root = tmp_path / "ai"
    (root / "pilot").mkdir(parents=True)
    (root / "prod").mkdir()
    return root


def test_declaration_resolves_ai_root_and_reads_tiers(tmp_path, ai_root):
    path = write_declaration(tmp_path / "roles.json",
                             ["${AI_ROOT}/pilot", "${AI_ROOT}/prod"],
                             tiers={"${AI_ROOT}/prod": "two-human"})
    d = load_declaration(path, "pxx", ai_root)
    assert d.scopes == (str((ai_root / "pilot").resolve()), str((ai_root / "prod").resolve()))
    assert d.default == SINGLE_REVIEWER
    assert d.tier_declared_for(d.scopes[0]) == SINGLE_REVIEWER
    assert d.tier_declared_for(d.scopes[1]) == TWO_HUMAN


def test_declaration_without_a_default_reads_none(tmp_path, ai_root):
    path = write_declaration(tmp_path / "roles.json", ["${AI_ROOT}/pilot"], default=None)
    assert load_declaration(path, "pxx", ai_root).default is None


@pytest.mark.parametrize("mutate,needle", [
    (lambda e: e.__setitem__("approval_tier_default", "self"), "approval_tier_default"),
    (lambda e: e.__setitem__("approval_tier", {"${AI_ROOT}/pilot": "maybe"}), "approval_tier"),
    (lambda e: e.__setitem__("approval_tier", {"${AI_ROOT}/elsewhere": "two-human"}), "not one of the role's scope"),
    (lambda e: e.__setitem__("approval_tier", ["two-human"]), "must be an object"),
    (lambda e: e.pop("scope"), "no `scope` list"),
    (lambda e: e.__setitem__("scope", ["relative/path"]), "absolute"),
])
def test_malformed_declarations_are_refused(tmp_path, ai_root, mutate, needle):
    path = write_declaration(tmp_path / "roles.json", ["${AI_ROOT}/pilot"])
    data = json.loads(path.read_text())
    mutate(data["pxx"])
    path.write_text(json.dumps(data))
    with pytest.raises(DeclarationError, match=needle):
        load_declaration(path, "pxx", ai_root)


def test_a_missing_role_is_refused(tmp_path, ai_root):
    path = write_declaration(tmp_path / "roles.json", ["${AI_ROOT}/pilot"])
    with pytest.raises(DeclarationError, match="no role 'other'"):
        load_declaration(path, "other", ai_root)


def test_an_unreadable_declaration_is_refused(tmp_path, ai_root):
    with pytest.raises(DeclarationError, match="cannot read"):
        load_declaration(tmp_path / "absent.json", "pxx", ai_root)


# --- decision -----------------------------------------------------------------

def _decl(ai_root: Path, **kw) -> Declaration:
    path = write_declaration(ai_root / "roles.json", ["${AI_ROOT}/pilot", "${AI_ROOT}/prod"], **kw)
    return load_declaration(path, "pxx", ai_root)


def test_no_declaration_is_two_human(ai_root):
    d = decide(ai_root / "pilot", ["src/a.py"], None)
    assert d.tier == TWO_HUMAN
    assert "no approval declaration" in d.reasons[0]
    assert d.sod_exception is None


def test_no_repo_is_two_human(ai_root):
    d = decide(None, ["src/a.py"], _decl(ai_root))
    assert d.tier == TWO_HUMAN and "no --repo" in d.reasons[0]


def test_an_undeclared_repo_is_two_human(ai_root):
    (ai_root / "other").mkdir()
    d = decide(ai_root / "other", ["src/a.py"], _decl(ai_root))
    assert d.tier == TWO_HUMAN and "not a scope declared" in d.reasons[0]


def test_an_unreadable_diff_is_two_human(ai_root):
    d = decide(ai_root / "pilot", None, _decl(ai_root))
    assert d.tier == TWO_HUMAN and "could not be read" in d.reasons[0]


def test_the_default_single_reviewer_applies_to_an_ordinary_diff(ai_root):
    d = decide(ai_root / "pilot", ["src/a.py", "docs/b.md"], _decl(ai_root))
    assert d.tier == SINGLE_REVIEWER
    assert d.sod_exception == SOD_EXCEPTION
    assert d.signable and d.changed == 2 and d.touched == ()


def test_a_declared_two_human_scope_stays_two_human(ai_root):
    d = decide(ai_root / "prod", ["src/a.py"], _decl(ai_root, tiers={"${AI_ROOT}/prod": "two-human"}))
    assert d.tier == TWO_HUMAN and "declared" in d.reasons[0]


def test_a_two_human_default_is_honoured(ai_root):
    d = decide(ai_root / "pilot", ["src/a.py"], _decl(ai_root, default="two-human"))
    assert d.tier == TWO_HUMAN and "the default" in d.reasons[0]


def test_no_default_and_no_declaration_for_the_scope_is_two_human(ai_root):
    d = decide(ai_root / "pilot", ["src/a.py"], _decl(ai_root, default=None))
    assert d.tier == TWO_HUMAN and "no tier declared" in d.reasons[0]


def test_an_exec_surface_touch_raises_a_single_reviewer_scope(ai_root):
    d = decide(ai_root / "pilot", ["src/a.py", "tests/test_a.py"], _decl(ai_root))
    assert d.tier == TWO_HUMAN
    assert d.touched == ("tests/test_a.py",)
    assert d.sod_exception is None


def test_control_plane_contact_is_not_signable_whatever_the_declaration(ai_root):
    d = decide(ai_root / "pilot", ["src/a.py", "CODEOWNERS"], _decl(ai_root, default="two-human"))
    assert d.tier == CONTROL_PLANE and not d.signable
    assert d.touched == ("CODEOWNERS",)


def test_control_plane_wins_over_exec_surface(ai_root):
    d = decide(ai_root / "pilot", ["tests/x.py", "queue/T.json"], _decl(ai_root))
    assert d.tier == CONTROL_PLANE


def test_an_empty_diff_on_a_single_reviewer_scope_is_single_reviewer(ai_root):
    """No paths, nothing touched: the declared tier stands (the gate's state
    check, not this module, decides whether an empty candidate is mergeable)."""
    d = decide(ai_root / "pilot", [], _decl(ai_root))
    assert d.tier == SINGLE_REVIEWER and d.changed == 0


def test_as_json_carries_what_the_row_and_bundle_need(ai_root):
    j = decide(ai_root / "pilot", ["src/a.py"], _decl(ai_root)).as_json()
    assert j["tier"] == SINGLE_REVIEWER and j["sod_exception"] == SOD_EXCEPTION
    assert j["signable"] is True and j["touched"] == [] and j["changed"] == 1


# --- chain walk and diff --------------------------------------------------------

def _queue(ledger: Path, task: str, **fields) -> None:
    (ledger / "queue").mkdir(exist_ok=True)
    (ledger / "queue" / f"{task}.json").write_text(json.dumps({"task_id": task, **fields}))


def test_chain_root_base_walks_supersedes_to_the_first_base(tmp_path):
    _queue(tmp_path, "T-3", base_sha="c" * 40, supersedes="T-2")
    _queue(tmp_path, "T-2", base_sha="b" * 40, supersedes="T-1")
    _queue(tmp_path, "T-1", base_sha="a" * 40, supersedes="")
    assert chain_root_base("T-3", tmp_path) == ("a" * 40, ["T-1", "T-2", "T-3"])


def test_chain_root_base_survives_a_missing_predecessor(tmp_path):
    _queue(tmp_path, "T-3", base_sha="c" * 40, supersedes="T-2")
    assert chain_root_base("T-3", tmp_path) == ("c" * 40, ["T-2", "T-3"])


def test_chain_root_base_stops_on_a_cycle(tmp_path):
    _queue(tmp_path, "T-1", base_sha="a" * 40, supersedes="T-2")
    _queue(tmp_path, "T-2", base_sha="b" * 40, supersedes="T-1")
    base, chain = chain_root_base("T-1", tmp_path)
    assert chain == ["T-2", "T-1"] and base in ("a" * 40, "b" * 40)


def test_chain_root_base_without_queue_files(tmp_path):
    assert chain_root_base("T-9", tmp_path) == (None, ["T-9"])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True, timeout=30).stdout.strip()


def test_chain_changed_paths_spans_root_base_to_candidate(tmp_path):
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "b").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first attempt")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_b.py").write_text("def test():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rework adds a test")
    candidate = _git(repo, "rev-parse", "HEAD")
    # The whole chain, not the last correction: `b` is in the span even though
    # the candidate commit itself only added the test.
    assert sorted(chain_changed_paths(repo, base, candidate)) == ["b", "tests/test_b.py"]
    assert chain_changed_paths(repo, None, candidate) == ["tests/test_b.py"]


def test_chain_changed_paths_is_none_when_git_cannot_answer(tmp_path):
    assert chain_changed_paths(tmp_path, "a" * 40, "b" * 40) is None
