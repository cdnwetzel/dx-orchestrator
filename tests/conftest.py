"""Shared fixtures.

Every test that touches dx configuration points DX_CONFIG / DX_ROLES_PATH at
the synthetic fixtures in this directory, so the suite never depends on the
private sibling repos or on a developer's ~/.config.
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ROLES_DIR = FIXTURES / "roles"
MANIFEST = FIXTURES / "manifest.yml"


@pytest.fixture(autouse=True)
def _isolate_dx_env(monkeypatch):
    """Point dx at fixtures and drop any inherited dx env for every test."""
    for var in ("DX_CONFIG", "DX_ROLES_PATH", "DX_LEDGER_REPO", "DX_VLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DX_CONFIG", str(MANIFEST))
    monkeypatch.setenv("DX_ROLES_PATH", str(ROLES_DIR))
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
# Real chain verification, GPG keys and signed approvals live in the private
# cdnwetzel/devswarm-ledger repo. This fixture reproduces only the shape dx
# reads — tools/verify_chain.py, ledger.jsonl, queue/, approvals/ — so the
# merge-gate logic is testable in CI without that repo or a keyring.
# ---------------------------------------------------------------------------

LEDGER_HEAD = "b" * 64
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

    rows = [
        {"ts": "2026-09-01T00:00:00Z", "task_id": "T-TEST", "action": "ADMITTED",
         "author_human": "Alice Author", "author_seat": "S4"},
        {"ts": "2026-09-02T00:00:00Z", "task_id": "T-TEST", "action": "EXECUTED",
         "author_human": "Alice Author", "author_seat": "S4"},
        {"ts": "2026-09-03T00:00:00Z", "task_id": "T-OTHER", "action": "ADMITTED",
         "author_human": "Bob Other", "author_seat": "S6"},
    ]
    (repo / "ledger.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
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
    return repo


@pytest.fixture
def ledger_head() -> str:
    """The head hash the fake ledger's verifier reports."""
    return LEDGER_HEAD


@pytest.fixture
def stale_head() -> str:
    """A head hash that is *not* current — used to exercise RL-003."""
    return STALE_HEAD
