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

from .config_loader import ConfigError, ConfigSnapshot, get_config_snapshot
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

    snapshot: ConfigSnapshot | None   # None when no manifest was loaded
    json_text: str        # the redacted canonical record — the bytes written
    sha256: str           # sha256(json_text.encode()) — held here, never in the artifact
    captured_utc: str
    unavailable_kind: str | None = None    # why no manifest was loaded (classify_unavailable)
    unavailable_detail: str | None = None

    def check(self, written_text: str) -> Check:
        """The `manifest_digest` check. `ok` means two things at once: a
        manifest was loaded and digested, AND the bytes that will be written
        equal the bytes captured. With no manifest loaded the check is NOT ok
        and says `unrecorded:` with the classified reason — the record is
        still written and verifiable, but there is no manifest binding to
        claim. Full digests in the detail, so a read-back can quote them."""
        bytes_ok = hashlib.sha256(written_text.encode("utf-8")).hexdigest() == self.sha256
        snap = self.snapshot
        if snap is None:
            return Check(
                ok=False,
                path="artifacts/config.json",
                detail=(
                    f"unrecorded: no manifest was loaded ({self.unavailable_kind}: "
                    f"{self.unavailable_detail}); effective={self.sha256}"
                    + ("" if bytes_ok else " (written bytes differ from the captured record)")
                ),
            )
        return Check(
            ok=bytes_ok,
            path="artifacts/config.json",
            detail=(
                f"normalized={snap.normalized_sha256 or 'null'} raw={snap.raw_sha256} "
                f"effective={self.sha256}"
                + ("" if bytes_ok else " (written bytes differ from the captured record)")
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


def classify_unavailable(exc: BaseException) -> tuple[str, str]:
    """Why the manifest could not be loaded, as a kind a reader can act on.

    `requested-*`: DX_CONFIG names a file explicitly, so its absence or
    unreadability is an operator error, never a legitimate "no manifest".
    `default-*`: nothing named a manifest and the default path is absent
    (legitimate for `dx merge` with DX_LEDGER_REPO set), unreadable, or
    invalid. The caller's own fail-closed behaviour on these is unchanged;
    this only labels the record.
    """
    requested = "DX_CONFIG" in os.environ
    if isinstance(exc, FileNotFoundError):
        kind = "missing" if requested else "absent"
    elif isinstance(exc, ConfigError):
        kind = "invalid"
    elif isinstance(exc, OSError):
        kind = "unreadable"
    else:
        kind = "error"
    return (f"{'requested' if requested else 'default'}-{kind}", redact(str(exc))[0])


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
    consumed_sections: dict[str, Any] | None = None,
    declaration: Any = None,
    declaration_error: str | None = None,
) -> EffectiveConfig:
    """Build the record at the consumption point. Everything the record
    needs is passed in by the caller that just resolved it; this function
    reads the environment once, here, and the loader's snapshot of the
    mapping those values came from.

    Three states are kept apart for everything consumed only on some
    paths: ``not-consumed`` (the command never read it), ``unavailable``
    (it tried and could not, with a redacted reason) and ``consumed`` (with
    ``empty`` true when what was read is an empty section). ``declaration``
    is the loaded `Declaration` object the tier decision will use — the
    digest is `source_sha256` from the bytes that load read, never a later
    read of the path — and ``declaration_error`` is the classified reason
    when loading failed. ``consumed_sections`` carries `gui_verification`
    and `psoperator` when `dx merge --verify-gui` reads them."""
    unavailable: dict[str, str] | None = None
    try:
        snapshot: ConfigSnapshot | None = get_config_snapshot()
    except (OSError, ConfigError) as exc:
        # `dx merge` with DX_LEDGER_REPO set runs without a manifest; the
        # record says so — classified — rather than inventing one. The
        # caller's own refusal on a missing or invalid manifest (dx run's
        # route resolution, dx merge's precheck) is untouched by this.
        snapshot = None
        kind, detail = classify_unavailable(exc)
        unavailable = {"kind": kind, "detail": detail}
    env_overrides = {
        name: _env_value(os.environ[name]) for name in OVERRIDE_ENV if name in os.environ
    }
    endpoint, stripped = strip_endpoint(getattr(route, "endpoint", None))
    if declaration is not None:
        decl_state: dict[str, Any] = {
            "state": "consumed",
            "sha256": getattr(declaration, "source_sha256", None),
            "source": str(getattr(declaration, "source", "")),
            "role": getattr(declaration, "role", None),
        }
    elif declaration_error is not None:
        decl_state = {"state": "unavailable", "reason": redact(declaration_error)[0]}
    elif approval is None:
        decl_state = {"state": "not-consumed", "reason": "no approval section"}
    else:
        decl_state = {"state": "not-consumed", "reason": "not read on this path"}

    def _consumed(name: str) -> dict[str, Any]:
        if consumed_sections is None or name not in consumed_sections:
            return {"state": "not-consumed"}
        value = consumed_sections[name]
        return {"state": "consumed", "value": value, "empty": not bool(value)}

    resolved: dict[str, Any] = {
        "ledger_repo": str(ledger_repo) if ledger_repo else None,
        "roles_path": str(roles_path) if roles_path else None,
        "evidence_root": str(evidence_root) if evidence_root else None,
        "branches": _section(branches),
        "review": _section(review),
        "approval": _section(approval),
        "declaration": decl_state,
        "gui_verification": _consumed("gui_verification"),
        "psoperator": _consumed("psoperator"),
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
        "manifest_unavailable": unavailable,
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
        unavailable_kind=unavailable["kind"] if unavailable else None,
        unavailable_detail=unavailable["detail"] if unavailable else None,
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
