"""The configuration a run consumed, captured once and written verbatim (S3).

A bundle used to record the routing it resolved and the command it ran, but
not the manifest it resolved them from: a D-11 read-back could quote the
file, never the run. This module captures, at the moment `dx run` or
`dx merge` resolves its configuration and before anything executes, one
immutable record — the manifest snapshot (digests only), the overrides in
force, and the resolved values dx used — serialises it once, redacts it
once, and digests the result. Emission writes those bytes verbatim and the
`manifest_digest` check verifies the bytes written against the digest held
in the record, which the artifact itself does not contain.

Secret safety is by allowlist, not by hope: no manifest value is copied
except the resolved fields named here; environment values are recorded only
when they are filesystem paths; endpoints lose userinfo, query and
fragment; and the whole record passes the bundle's redaction filter before
it is digested. Digests fingerprint a configuration — a reader holding a
candidate file can test "is this the one?" — and store nothing of it.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config_loader import ConfigSnapshot, get_config_snapshot
from .evidence import Check
from .redact import redact

#: Environment variables that change which values dx uses without touching
#: the manifest. Names are always recorded when set; values only when they
#: are paths (see _env_value).
OVERRIDE_ENV = ("DX_CONFIG", "DX_LEDGER_REPO", "DX_ROLES_PATH", "DX_EVIDENCE_DIR", "PSOPERATOR_REPO")


def _env_value(value: str) -> str:
    """A path is recorded as itself; anything else as a digest marker, so a
    token someone exported under a DX_* name never lands in a bundle."""
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return expanded
    return f"<non-path, sha256:{hashlib.sha256(value.encode()).hexdigest()}>"


def strip_endpoint(url: str | None) -> tuple[str | None, list[str]]:
    """``scheme://host[:port]/path`` only. Returns the stripped URL and the
    names of the parts removed (userinfo, query, fragment)."""
    if not url:
        return url, []
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url, []
    removed: list[str] = []
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
        removed.append("userinfo")
    if parts.query:
        removed.append("query")
    if parts.fragment:
        removed.append("fragment")
    return urlunsplit((parts.scheme, netloc, parts.path, "", "")), removed


def _section(obj: Any) -> Any:
    if obj is None:
        return None
    if is_dataclass(obj) and not isinstance(obj, type):
        out = asdict(obj)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in out.items()}
    return obj


@dataclass(frozen=True)
class EffectiveConfig:
    """Captured once; carried by value; written verbatim."""

    snapshot: ConfigSnapshot | None   # None when no manifest was loaded (merge with DX_LEDGER_REPO only)
    json_text: str        # the redacted canonical record — the bytes written
    sha256: str           # sha256(json_text.encode()) — held here, never in the artifact
    captured_utc: str

    def check(self, written_text: str) -> Check:
        """The `manifest_digest` check: the bytes that will be written equal
        the bytes captured. Full digests in the detail, so a read-back can
        quote them."""
        ok = hashlib.sha256(written_text.encode("utf-8")).hexdigest() == self.sha256
        snap = self.snapshot
        manifest_part = (
            f"normalized={snap.normalized_sha256 or 'null'} raw={snap.raw_sha256}"
            if snap is not None else "manifest=none (no manifest was loaded)"
        )
        return Check(
            ok=ok,
            path="artifacts/config.json",
            detail=(
                f"{manifest_part} effective={self.sha256}"
                + ("" if ok else " (written bytes differ from the captured record)")
            ),
        )

    def observe(self) -> str:
        """The post-run observation artifact for this record's manifest, or
        a recorded `unavailable` when no manifest was loaded."""
        if self.snapshot is None:
            return json.dumps({"path": None, "observed_utc": _now(), "post_run_raw_sha256": None,
                               "unavailable": "no manifest was loaded"},
                              indent=2, sort_keys=True) + "\n"
        return observe_manifest(self.snapshot.path)


def capture_effective_config(
    *,
    cli_overrides: dict[str, Any],
    route: Any = None,
    branches: Any = None,
    review: Any = None,
    approval: Any = None,
    ledger_repo: Path | None = None,
    roles_path: Path | None = None,
    evidence_root: Path | None = None,
) -> EffectiveConfig:
    """Build the record at the consumption point. Everything the record
    needs is passed in by the caller that just resolved it; this function
    reads the environment once, here, and the loader's snapshot of the
    mapping those values came from."""
    try:
        snapshot: ConfigSnapshot | None = get_config_snapshot()
        manifest_unavailable: str | None = None
    except FileNotFoundError as exc:
        # `dx merge` with DX_LEDGER_REPO set runs without a manifest; the
        # record says so rather than inventing one or refusing the gate.
        snapshot, manifest_unavailable = None, redact(str(exc))[0]
    env_overrides = {
        name: _env_value(os.environ[name]) for name in OVERRIDE_ENV if name in os.environ
    }
    endpoint, stripped = strip_endpoint(getattr(route, "endpoint", None))
    resolved: dict[str, Any] = {
        "ledger_repo": str(ledger_repo) if ledger_repo else None,
        "roles_path": str(roles_path) if roles_path else None,
        "evidence_root": str(evidence_root) if evidence_root else None,
        "branches": _section(branches),
        "review": _section(review),
        "approval": _section(approval),
        "route": None if route is None else {
            "endpoint": endpoint,
            "endpoint_stripped": stripped,
            "model": getattr(route, "model", None),
            "provider": getattr(route, "provider", None),
            "timeout_s": getattr(route, "timeout_s", None),
        },
    }
    record = {
        "manifest": snapshot.as_json() if snapshot is not None else None,
        "manifest_unavailable": manifest_unavailable,
        "overrides_env": env_overrides,
        "overrides_cli": cli_overrides,
        "resolved": resolved,
        "captured_utc": _now(),
    }
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    # Redact at capture, before digesting, with the same filter the bundle
    # writer applies at the write: the digest is then of the bytes that land.
    redacted, findings = redact(text)
    if findings:
        record["redaction"] = {"findings": findings}
        redacted, _ = redact(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return EffectiveConfig(
        snapshot=snapshot,
        json_text=redacted,
        sha256=hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        captured_utc=str(record["captured_utc"]),
    )


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def observe_manifest(path: Path) -> str:
    """The post-run observation: one more read of the manifest at emission,
    as a separate artifact. Informational only — an unreadable file is a
    recorded `unavailable`, never a failure, and the error text is redacted
    like every other artifact string."""
    observed: dict[str, Any] = {
        "path": str(path),
        "observed_utc": _now(),
        "post_run_raw_sha256": None,
        "unavailable": None,
    }
    try:
        observed["post_run_raw_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        observed["unavailable"] = redact(f"{type(exc).__name__}: {exc}")[0]
    return json.dumps(observed, indent=2, sort_keys=True) + "\n"
