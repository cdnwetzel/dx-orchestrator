"""Evidence-store inventory — so a default store is never an unwatched pile.

`summarize_store` counts bundles by family; the point of the `referenceable`
field is the prune rule landed with SP9: `dx.merge_gate.v1` is the one family a
ledger row can bind (A1/0.18.0), so it must never be pruned while a row names its
digest, whereas role-task / gui-verification bundles are freely prunable.
"""

from __future__ import annotations

import json
from pathlib import Path

from dx.evidence import (
    GUI_SCHEMA,
    MERGE_SCHEMA,
    SCHEMA,
    default_evidence_root,
    summarize_store,
)


def _bundle(root: Path, task: str, ts: str, schema: str) -> None:
    d = root / task / ts
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"schema": schema}), encoding="utf-8")


def test_a_missing_store_summarizes_as_empty(tmp_path):
    s = summarize_store(tmp_path / "nope")
    assert s.total == 0 and s.by_family == {} and s.referenceable == 0


def test_counts_by_family_and_flags_referenceable(tmp_path):
    _bundle(tmp_path, "T-1", "ts1", SCHEMA)
    _bundle(tmp_path, "T-1", "ts2", SCHEMA)
    _bundle(tmp_path, "T-2", "ts1", GUI_SCHEMA)
    _bundle(tmp_path, "T-0001", "ts1", MERGE_SCHEMA)
    s = summarize_store(tmp_path)
    assert s.total == 4
    assert s.by_family == {SCHEMA: 2, GUI_SCHEMA: 1, MERGE_SCHEMA: 1}
    assert s.referenceable == 1  # only the merge_gate bundle


def test_an_unreadable_manifest_is_counted_not_raised(tmp_path):
    d = tmp_path / "T-x" / "ts"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    s = summarize_store(tmp_path)
    assert s.total == 1 and s.by_family == {"unreadable": 1}


def test_default_root_honors_dx_evidence_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DX_EVIDENCE_DIR", str(tmp_path / "store"))
    assert default_evidence_root() == tmp_path / "store"
    monkeypatch.delenv("DX_EVIDENCE_DIR", raising=False)
    assert default_evidence_root() == Path("~/.local/state/dx/evidence").expanduser()
