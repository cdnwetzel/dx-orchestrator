"""`dx tier` — the merge gate's tier question, answered read-only."""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import manifest_with_approval, write_declaration
from dx.cli import build_parser


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True, timeout=30).stdout.strip()


@pytest.fixture
def declared(monkeypatch, tmp_path, fake_ledger):
    ai_root = tmp_path / "ai"
    scope = ai_root / "pilot"
    scope.mkdir(parents=True)
    _git(scope, "init", "-q", "-b", "main")
    (scope / "a.py").write_text("a\n")
    _git(scope, "add", "-A")
    _git(scope, "commit", "-qm", "base")
    base = _git(scope, "rev-parse", "HEAD")
    (scope / "b.py").write_text("b\n")
    _git(scope, "add", "-A")
    _git(scope, "commit", "-qm", "work")
    candidate = _git(scope, "rev-parse", "HEAD")
    decl = write_declaration(ai_root / "roles.json", ["${AI_ROOT}/pilot"])
    monkeypatch.setenv("DX_CONFIG", str(manifest_with_approval(tmp_path, decl, ai_root)))
    monkeypatch.setenv("DX_LEDGER_REPO", str(fake_ledger))
    # T-TEST's recorded candidate is "e"*40 in the fixture; point it at ours
    from conftest import _canonical, _chain  # noqa: PLC0415
    rows, head = _chain([
        {"ts": "2026-09-01T00:00:00Z", "task_id": "T-TEST", "action": "ADMITTED",
         "author_human": "Alice Author", "author_seat": "S4"},
        {"ts": "2026-09-02T00:00:00Z", "task_id": "T-TEST", "action": "EXECUTED",
         "author_human": "Alice Author", "author_seat": "S4", "sha": candidate},
    ])
    (fake_ledger / "ledger.jsonl").write_text("".join(_canonical(r) + "\n" for r in rows))
    (fake_ledger / "queue" / "T-TEST.json").write_text(json.dumps(
        {"task_id": "T-TEST", "approve_role": "code_review", "sha": candidate, "base_sha": base}))
    return scope


def _tier(*argv):
    args = build_parser().parse_args(["tier", *argv])
    args.func(args)


def test_json_answer_names_the_tier_and_who_may_sign(declared, capsys):
    _tier("T-TEST", "--repo", str(declared), "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["tier"] == "single-reviewer"
    assert out["author"] == "Alice Author"
    assert out["signable"] is True
    assert "including the author Alice Author" in out["who_may_sign"]
    assert out["chain"] == ["T-TEST"]


def test_without_a_repo_the_answer_is_two_human(declared, capsys):
    _tier("T-TEST")
    out = capsys.readouterr().out
    assert "T-TEST: two-human" in out and "no --repo" in out
    assert "other than the author" in out


def test_it_writes_nothing(declared, capsys):
    ledger = declared.parent.parent / "devswarm-ledger"
    before = (ledger / "ledger.jsonl").read_bytes()
    _tier("T-TEST", "--repo", str(declared))
    assert (ledger / "ledger.jsonl").read_bytes() == before


def test_an_unknown_task_exits_one(declared, capsys):
    with pytest.raises(SystemExit) as exc:
        _tier("T-NOPE")
    assert exc.value.code == 1
