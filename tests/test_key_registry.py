"""dx.key_registry — the mechanism a row records comes from here, or the row
is not written.

Every case is a way the registry could wrongly admit a key: missing file,
wrong schema, a listed file that is absent, an unlisted file that is present,
a retired key, a duplicate, a residency outside the two RL-010 mechanisms.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import HARDWARE_MECHANISM, SOFTWARE_MECHANISM, registry_entry, write_registry
from dx.approval_key import TEST_DOUBLE_DIRNAME
from dx.key_registry import (
    MECHANISMS,
    RegistryError,
    active_key_files,
    load_registry,
    lookup,
)

FPR_A = "A" * 40
FPR_B = "B" * 40


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "keys"
    d.mkdir(parents=True)
    (d / "alice.asc").write_text("(pub)")
    (d / "bob.asc").write_text("(pub)")
    return d


def test_the_two_mechanisms_are_the_decisions_exact_wording():
    assert MECHANISMS["software"] == SOFTWARE_MECHANISM
    assert MECHANISMS["card"] == HARDWARE_MECHANISM
    assert set(MECHANISMS) == {"card", "software"}


def test_a_valid_registry_loads_and_derives_the_mechanism(keys):
    write_registry(keys, registry_entry("alice.asc", FPR_A, "Alice Author"),
                   registry_entry("bob.asc", FPR_B, "Bob Reviewer", residency="card"))
    reg = load_registry(keys)
    assert [k.file for k in reg] == ["alice.asc", "bob.asc"]
    assert lookup(reg, FPR_A).mechanism == SOFTWARE_MECHANISM
    assert lookup(reg, FPR_B).mechanism == HARDWARE_MECHANISM
    assert lookup(reg, " ".join(FPR_A[i:i + 4] for i in range(0, 40, 4)).lower()).holder == "Alice Author"


def test_missing_registry_is_refused(keys):
    with pytest.raises(RegistryError, match="key registry not found"):
        load_registry(keys)


def test_wrong_schema_is_refused(keys):
    (keys / "REGISTRY.json").write_text(json.dumps({"schema": "other", "keys": []}))
    with pytest.raises(RegistryError, match="schema"):
        load_registry(keys)


def test_a_listed_file_that_is_absent_is_refused(keys):
    write_registry(keys, registry_entry("alice.asc", FPR_A, "Alice Author"),
                   registry_entry("bob.asc", FPR_B, "Bob Reviewer"),
                   registry_entry("carol.asc", "C" * 40, "Carol"))
    with pytest.raises(RegistryError, match="carol.asc is registered but not present"):
        load_registry(keys)


def test_an_unlisted_key_file_is_refused(keys):
    write_registry(keys, registry_entry("alice.asc", FPR_A, "Alice Author"))
    with pytest.raises(RegistryError, match="does not list: bob.asc"):
        load_registry(keys)


def test_the_test_double_directory_is_not_the_registrys_business(keys):
    (keys / TEST_DOUBLE_DIRNAME).mkdir()
    (keys / TEST_DOUBLE_DIRNAME / "double.asc").write_text("(pub)")
    write_registry(keys, registry_entry("alice.asc", FPR_A, "Alice Author"),
                   registry_entry("bob.asc", FPR_B, "Bob Reviewer"))
    assert len(load_registry(keys)) == 2


def test_a_retired_key_is_inactive_and_refused_on_lookup(keys):
    write_registry(keys, registry_entry("alice.asc", FPR_A, "Alice Author", retired="2026-09-23"),
                   registry_entry("bob.asc", FPR_B, "Bob Reviewer"))
    reg = load_registry(keys)
    assert active_key_files(reg) == ["bob.asc"]
    with pytest.raises(RegistryError, match="retired on 2026-09-23"):
        lookup(reg, FPR_A)


def test_an_unknown_fingerprint_is_refused(keys):
    write_registry(keys, registry_entry("alice.asc", FPR_A, "Alice Author"),
                   registry_entry("bob.asc", FPR_B, "Bob Reviewer"))
    with pytest.raises(RegistryError, match="not in the registry"):
        lookup(load_registry(keys), "D" * 40)


@pytest.mark.parametrize("bad", [
    {"residency": "token"},
    {"residency": ""},
    {"fingerprint": "abc"},
    {"registered": "yesterday"},
    {"retired": "soon"},
    {"holder": ""},
    {"file": "../alice.asc"},
])
def test_malformed_entries_are_refused(keys, bad):
    entry = registry_entry("alice.asc", FPR_A, "Alice Author")
    entry.update(bad)
    write_registry(keys, entry, registry_entry("bob.asc", FPR_B, "Bob Reviewer"))
    with pytest.raises(RegistryError):
        load_registry(keys)


def test_duplicates_are_refused(keys):
    write_registry(keys, registry_entry("alice.asc", FPR_A, "Alice Author"),
                   registry_entry("bob.asc", FPR_A, "Bob Reviewer"))
    with pytest.raises(RegistryError, match="listed twice"):
        load_registry(keys)


def test_an_empty_key_list_is_refused(keys):
    (keys / "alice.asc").unlink()
    (keys / "bob.asc").unlink()
    (keys / "REGISTRY.json").write_text(json.dumps({"schema": "devswarm-ledger.key-registry.v1", "keys": []}))
    with pytest.raises(RegistryError, match="non-empty"):
        load_registry(keys)
