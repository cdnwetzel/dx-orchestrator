"""The ledger's key registry — what the merge gate records about a signature's
mechanism instead of assuming it.

Charter Decision 0020 item 7 (2026-09-30): every SIGNED and MERGED row carries
a ``mechanism=`` value **derived by the gate from the registry**, never supplied
by the signer, the caller or a flag. RL-010 as amended by Decision 0017 names
two mechanisms and only two: a hardware-resident, non-exportable key with a
gesture per signature, or the interactive software fallback, which every row
it signs must announce as non-compliant.

The registry is ``docs/keys/REGISTRY.json`` beside the public keys. It lists
every key file with its fingerprint, holder, residency, registration date and
retirement date. Three consequences the verifier enforces:

* a key file the registry does not list does not verify — an unlisted ``.asc``
  is an error, not a key;
* a retired key leaves the keyring — its file stays only so the rows it signed
  keep verifying by hand;
* the holder must equal the bare name in the key's uid, which is what ledger
  rows carry as ``author_human`` — a registry that names a different person
  from the key is refused rather than trusted.

Fail closed throughout: a missing or malformed registry refuses every
signature. A ledger without a registry has no recorded mechanism, and the
decision does not allow an assumed one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from dx.approval_key import TEST_DOUBLE_DIRNAME

REGISTRY_FILENAME = "REGISTRY.json"
REGISTRY_SCHEMA = "devswarm-ledger.key-registry.v1"

#: residency → the exact ``mechanism=`` value a row records (Decision 0020 §7).
MECHANISMS: dict[str, str] = {
    "card": "hardware (RL-010: non-exportable key, gesture per signature)",
    "software": "fallback (RL-010 non-compliant: software key)",
}

_FPR_RE = re.compile(r"^[0-9A-F]{40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RegistryError(RuntimeError):
    """The registry is missing, malformed, or disagrees with the key directory."""


@dataclass(frozen=True)
class RegisteredKey:
    file: str
    fingerprint: str
    holder: str
    residency: str
    registered: str
    retired: str | None

    @property
    def active(self) -> bool:
        return self.retired is None

    @property
    def mechanism(self) -> str:
        return MECHANISMS[self.residency]


def _str_field(entry: dict[str, object], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{where}: `{key}` must be a non-empty string")
    return value.strip()


def load_registry(keys_dir: Path) -> tuple[RegisteredKey, ...]:
    """Read and validate ``<keys_dir>/REGISTRY.json``.

    Every listed file must exist, every ``.asc`` in the directory must be
    listed (the test-double subdirectory excepted, as in ledger_utils), and no
    fingerprint may appear twice. The order is the file's.
    """
    path = keys_dir / REGISTRY_FILENAME
    if not path.is_file():
        raise RegistryError(
            f"key registry not found at {path}. Every approval key must be "
            f"registered with its holder and residency (Decision 0020 §7); "
            f"without the registry no signature verifies."
        )
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"{path}: not readable as JSON: {exc}") from exc
    if not isinstance(loaded, dict) or loaded.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError(
            f"{path}: expected an object with schema={REGISTRY_SCHEMA!r}"
        )
    raw_keys = loaded.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise RegistryError(f"{path}: `keys` must be a non-empty list")

    keys: list[RegisteredKey] = []
    seen_fprs: set[str] = set()
    seen_files: set[str] = set()
    for i, entry in enumerate(raw_keys):
        where = f"{path}: keys[{i}]"
        if not isinstance(entry, dict):
            raise RegistryError(f"{where}: expected an object")
        file = _str_field(entry, "file", where)
        if "/" in file or "\\" in file or not file.endswith(".asc"):
            raise RegistryError(f"{where}: `file` must be a bare `<name>.asc` in docs/keys/")
        fpr = _str_field(entry, "fingerprint", where).replace(" ", "").upper()
        if not _FPR_RE.match(fpr):
            raise RegistryError(f"{where}: `fingerprint` must be 40 hex characters")
        holder = _str_field(entry, "holder", where)
        residency = _str_field(entry, "residency", where)
        if residency not in MECHANISMS:
            raise RegistryError(
                f"{where}: `residency` must be one of {sorted(MECHANISMS)}, found {residency!r}"
            )
        registered = _str_field(entry, "registered", where)
        if not _DATE_RE.match(registered):
            raise RegistryError(f"{where}: `registered` must be YYYY-MM-DD")
        retired_raw = entry.get("retired")
        retired: str | None
        if retired_raw is None:
            retired = None
        elif isinstance(retired_raw, str) and _DATE_RE.match(retired_raw):
            retired = retired_raw
        else:
            raise RegistryError(f"{where}: `retired` must be null or YYYY-MM-DD")
        if fpr in seen_fprs:
            raise RegistryError(f"{where}: fingerprint {fpr} is listed twice")
        if file in seen_files:
            raise RegistryError(f"{where}: file {file} is listed twice")
        if not (keys_dir / file).is_file():
            raise RegistryError(f"{where}: {file} is registered but not present in {keys_dir}")
        seen_fprs.add(fpr)
        seen_files.add(file)
        keys.append(RegisteredKey(file, fpr, holder, residency, registered, retired))

    unlisted = sorted(
        asc.name
        for asc in keys_dir.glob("*.asc")
        if asc.parent.name != TEST_DOUBLE_DIRNAME and asc.name not in seen_files
    )
    if unlisted:
        raise RegistryError(
            f"{keys_dir} holds key file(s) the registry does not list: "
            f"{', '.join(unlisted)}. Register each with its holder and residency, "
            f"or remove it; an unlisted key does not verify."
        )
    return tuple(keys)


def active_key_files(keys: tuple[RegisteredKey, ...]) -> list[str]:
    return [k.file for k in keys if k.active]


def lookup(keys: tuple[RegisteredKey, ...], fingerprint: str) -> RegisteredKey:
    """The registry entry for a fingerprint; refused when absent or retired."""
    fpr = fingerprint.replace(" ", "").upper()
    for key in keys:
        if key.fingerprint == fpr:
            if not key.active:
                raise RegistryError(
                    f"key {fpr} ({key.holder}, {key.file}) was retired on "
                    f"{key.retired} and signs nothing new"
                )
            return key
    raise RegistryError(f"key {fpr} is not in the registry")


__all__ = [
    "MECHANISMS",
    "REGISTRY_FILENAME",
    "REGISTRY_SCHEMA",
    "RegisteredKey",
    "RegistryError",
    "active_key_files",
    "load_registry",
    "lookup",
]
