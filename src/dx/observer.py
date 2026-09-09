"""Bind a PSOperator observer attestation to the frame dx is about to judge.

The observer signs a *perception* snapshot — an element inventory plus
``frame_hash = sha256(raw RGB pixel bytes)`` — under an owner-only symmetric
key. It never hands back an image. To use that signature as provenance for the
exact bytes dx sends to the vision model, dx recomputes the hash the observer's
way and requires it to match. A mismatch means the frame dx is judging is not
the frame the observer attested, so dx fails closed rather than record
provenance it cannot stand behind.

PSOperator and Pillow are optional dependencies; every import that needs them is
lazy, so ``dx`` runs without them when the observer path is not used.
"""

from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ObserverError(RuntimeError):
    """The observer was unreachable, or its attestation did not verify."""


@dataclass(frozen=True)
class ObserverProvenance:
    """A verified observer attestation, bound to the captured frame by hash."""

    key_id: str
    observer_epoch: str
    issued_at: float
    expires_at: float
    nonce: str
    frame_hash: str
    signature: str

    def as_json(self) -> dict[str, object]:
        # signature_verified / frame_hash_matches are always true here: this
        # object is only constructed after both checks pass. They are recorded
        # so a reader of the bundle need not re-derive that from the code.
        return {
            "key_id": self.key_id,
            "observer_epoch": self.observer_epoch,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "frame_hash": self.frame_hash,
            "signature": self.signature,
            "signature_verified": True,
            "frame_hash_matches": True,
        }


def frame_rgb_sha256(png: bytes) -> str:
    """sha256 over raw RGB pixel bytes — the exact input psoperator hashes.

    psoperator computes ``frame_hash`` as ``sha256(rgb.tobytes())`` on the RGB
    image, so binding a PNG requires decoding it to RGB pixels first, not
    hashing the encoded file.
    """
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:  # pragma: no cover - env-specific
        raise ObserverError(
            "Pillow is required to bind an observer attestation "
            "(pip install pillow, or install psoperator)"
        ) from exc
    with Image.open(io.BytesIO(png)) as img:
        return hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()


def verify_attested_frame(
    attestation: Any, png: bytes, key: Any, *, now: float | None = None
) -> ObserverProvenance:
    """Verify signature, freshness and frame binding; raise ObserverError on any.

    ``attestation`` is a psoperator ``AttestedSnapshot`` and ``key`` a loaded
    ``AttestationKey``. Kept import-light so the dataclass and the failure modes
    can be reasoned about without a running observer.
    """
    from psoperator.common.attestation import attestation_signature_matches

    if not attestation_signature_matches(key, attestation):
        raise ObserverError("observer attestation signature does not verify")
    body = attestation.body
    clock = time.time() if now is None else now
    if body.expires_at <= clock:
        raise ObserverError(
            f"observer attestation is expired (expires_at {body.expires_at} <= now {clock})"
        )
    attested = body.snapshot.frame_hash
    captured = frame_rgb_sha256(png)
    if captured != attested:
        raise ObserverError(
            "frame hash mismatch: the frame dx is judging is not the frame the "
            f"observer attested (attested {attested[:16]}…, captured {captured[:16]}…)"
        )
    return ObserverProvenance(
        key_id=body.key_id,
        observer_epoch=body.observer_epoch,
        issued_at=body.issued_at,
        expires_at=body.expires_at,
        nonce=body.nonce,
        frame_hash=attested,
        signature=attestation.signature,
    )


def observe_and_bind(
    png: bytes, *, host: str, port: int | str, key_path: str, now: float | None = None
) -> ObserverProvenance:
    """Fetch a fresh attestation from a running observer and bind it to ``png``.

    The binding holds only when the screen was static between dx's capture and
    the observer's — a settled window, which is the normal case for GUI
    verification. On a frame that changed, the hashes differ and this raises,
    which is the honest outcome: you cannot attest a frame that moved.
    """
    from psoperator.common.attestation import load_attestation_key
    from psoperator.services.observer_client import ObserverClient, ObserverUnavailable

    key = load_attestation_key(Path(key_path).expanduser())
    try:
        attestation = ObserverClient(host, int(port)).observe()
    except ObserverUnavailable as exc:
        raise ObserverError(f"observer unavailable: {exc}") from exc
    return verify_attested_frame(attestation, png, key, now=now)
