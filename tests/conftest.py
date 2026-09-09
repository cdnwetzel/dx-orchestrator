"""Shared fixtures.

Every test that touches dx configuration points DX_CONFIG / DX_ROLES_PATH at
the synthetic fixtures in this directory, so the suite never depends on the
sibling clones or on a developer's ~/.config.
"""
import hashlib
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ROLES_DIR = FIXTURES / "roles"
MANIFEST = FIXTURES / "manifest.yml"


@pytest.fixture(autouse=True)
def _isolate_dx_env(monkeypatch, tmp_path_factory):
    """Point dx at fixtures and drop any inherited dx env for every test."""
    for var in ("DX_CONFIG", "DX_ROLES_PATH", "DX_LEDGER_REPO", "DX_VLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DX_CONFIG", str(MANIFEST))
    monkeypatch.setenv("DX_ROLES_PATH", str(ROLES_DIR))
    # Never let a test write a real evidence bundle into ~/.local/state/dx —
    # dx run and dx verify-gui both emit by default. Redirect to a throwaway dir
    # unless the test sets its own.
    monkeypatch.setenv("DX_EVIDENCE_DIR", str(tmp_path_factory.mktemp("dx-evidence")))
    yield


@pytest.fixture(autouse=True)
def _reset_caches():
    """dx caches the manifest and registry in module globals; clear both."""
    from dx import config_loader, role_registry

    def clear():
        config_loader._config = None
        config_loader._config_source = None
        role_registry._registry = None
        role_registry._registry_source = None

    clear()
    yield
    clear()


@pytest.fixture
def roles_dir() -> Path:
    return ROLES_DIR


@pytest.fixture
def manifest_path() -> Path:
    return MANIFEST


# ---------------------------------------------------------------------------
# A stand-in for a devswarm-ledger clone.
#
# Real chain verification, GPG keys and signed approvals live in a ledger repo —
# cdnwetzel/devswarm-ledger-reference publicly, or a private operational one.
# This fixture reproduces only the shape dx reads — tools/verify_chain.py,
# ledger.jsonl, queue/, approvals/ — so the merge-gate logic is testable in CI
# without any clone or a keyring.
# ---------------------------------------------------------------------------

def _canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _chain(rows):
    """Turn bare rows into a real hash chain and return (rows, head).

    The fixture used to print a head unrelated to its own rows. That was
    harmless while `dx merge` only read; once it appended, `append_row`
    cross-checked the verifier's claim against the actual last row and refused.
    A fake that cannot survive being written to is not a fake of a ledger.
    """
    prev = "0" * 64
    out = []
    for row in rows:
        full = dict(row)
        full["prev_hash"] = prev
        out.append(full)
        prev = hashlib.sha256(_canonical(full).encode()).hexdigest()
    return out, prev


_ROWS, LEDGER_HEAD = _chain(
    [
        {"ts": "2026-09-01T00:00:00Z", "task_id": "T-TEST", "action": "ADMITTED",
         "author_human": "Alice Author", "author_seat": "S4"},
        {"ts": "2026-09-02T00:00:00Z", "task_id": "T-TEST", "action": "EXECUTED",
         "author_human": "Alice Author", "author_seat": "S4"},
        {"ts": "2026-09-03T00:00:00Z", "task_id": "T-OTHER", "action": "ADMITTED",
         "author_human": "Bob Other", "author_seat": "S6"},
    ]
)
STALE_HEAD = "c" * 64


@pytest.fixture
def fake_ledger(tmp_path) -> Path:
    repo = tmp_path / "devswarm-ledger"
    (repo / "tools").mkdir(parents=True)
    (repo / "queue").mkdir()
    (repo / "approvals").mkdir()
    (repo / "docs" / "keys").mkdir(parents=True)

    # Stands in for the reference verifier: prints the head line dx greps for.
    (repo / "tools" / "verify_chain.py").write_text(
        "import sys\n"
        "print('Chain OK: 3 rows')\n"
        f"print('Ledger head hash: {LEDGER_HEAD}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    (repo / "ledger.jsonl").write_text(
        "".join(_canonical(r) + "\n" for r in _ROWS), encoding="utf-8"
    )

    (repo / "queue" / "T-TEST.json").write_text(
        json.dumps({"task_id": "T-TEST", "approve_role": "code_review", "state": "REVIEWED"}),
        encoding="utf-8",
    )
    (repo / "queue" / "T-NOROLE.json").write_text(
        json.dumps({"task_id": "T-NOROLE", "state": "REVIEWED"}), encoding="utf-8"
    )

    # Approval payload signed against the *current* head. Note: no trailing
    # newline — real .msg files are a bare 81-byte concatenation.
    (repo / "approvals" / "T-TEST.code_review.msg").write_text(
        f"T-TEST{LEDGER_HEAD}code_review", encoding="utf-8"
    )
    (repo / "approvals" / "T-TEST.code_review.asc").write_text(
        "-----BEGIN PGP SIGNATURE-----\n(stub)\n-----END PGP SIGNATURE-----\n",
        encoding="utf-8",
    )
    import subprocess

    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-qm", "seed")):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, timeout=30)
    return repo


@pytest.fixture
def ledger_head() -> str:
    """The head hash the fake ledger's verifier reports."""
    return LEDGER_HEAD


@pytest.fixture
def stale_head() -> str:
    """A head hash that is *not* current — used to exercise RL-003."""
    return STALE_HEAD
