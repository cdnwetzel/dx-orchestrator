"""Verifying a PSOperator observer attestation and binding it to a frame.

These run entirely in-process against a provisioned test key and a synthetic
signed snapshot — no running observer — the same way the GPG gate is proved
against committed keys. They pin the three ways the binding can fail: a bad
signature, an expired envelope, and a frame that is not the frame attested.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io

import pytest

# psoperator and Pillow are optional deps of the observer path. Collected-but-
# skipped (like the real-card tests) rather than importorskip'd, so the pinned
# README test count is the same whether or not they are installed — CI installs
# them, so these RUN there and the no-skip gate covers them.
_HAVE_DEPS = (
    importlib.util.find_spec("psoperator") is not None
    and importlib.util.find_spec("PIL") is not None
)
pytestmark = pytest.mark.skipif(
    not _HAVE_DEPS, reason="psoperator/Pillow not installed (CI installs them)"
)

from dx.observer import (  # noqa: E402  (lazy deps: importable without psoperator)
    ObserverError,
    frame_rgb_sha256,
    verify_attested_frame,
)

if _HAVE_DEPS:
    from PIL import Image
    from psoperator.common.attestation import SnapshotSigner, provision_attestation_key
    from psoperator.common.schema import PerceptionSnapshot


def _png_and_hash() -> tuple[bytes, str, tuple[int, int]]:
    img = Image.new("RGB", (6, 4), (10, 20, 30))
    img.putpixel((1, 1), (200, 100, 50))
    frame_hash = hashlib.sha256(img.tobytes()).hexdigest()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), frame_hash, img.size


def _attestation(signer, frame_hash, size):
    snap = PerceptionSnapshot(
        frame_id=1, captured_at=1000.0, frame_hash=frame_hash, screen_size=size, elements=()
    )
    return signer.sign(snap)


@pytest.fixture
def key(tmp_path):
    return provision_attestation_key(tmp_path / "obs.key", "dx-test-observer")


def test_rgb_hash_matches_psoperators_definition():
    png, expected, _ = _png_and_hash()
    assert frame_rgb_sha256(png) == expected


def test_a_valid_attestation_binds_and_records(key):
    png, frame_hash, size = _png_and_hash()
    signer = SnapshotSigner(key, ttl_s=10.0, clock=lambda: 1000.0)
    att = _attestation(signer, frame_hash, size)

    prov = verify_attested_frame(att, png, key, now=1001.0)
    assert prov.frame_hash == frame_hash
    assert prov.key_id == key.key_id
    j = prov.as_json()
    assert j["signature_verified"] is True and j["frame_hash_matches"] is True


def test_a_wrong_key_fails_the_signature(key, tmp_path):
    png, frame_hash, size = _png_and_hash()
    signer = SnapshotSigner(key, ttl_s=10.0, clock=lambda: 1000.0)
    att = _attestation(signer, frame_hash, size)

    other = provision_attestation_key(tmp_path / "other.key", "dx-other-observer")
    with pytest.raises(ObserverError, match="signature"):
        verify_attested_frame(att, png, other, now=1001.0)


def test_an_expired_attestation_is_refused(key):
    png, frame_hash, size = _png_and_hash()
    signer = SnapshotSigner(key, ttl_s=5.0, clock=lambda: 1000.0)
    att = _attestation(signer, frame_hash, size)

    with pytest.raises(ObserverError, match="expired"):
        verify_attested_frame(att, png, key, now=1000.0 + 6.0)


def test_a_frame_that_was_not_attested_is_refused(key):
    _, frame_hash, size = _png_and_hash()
    signer = SnapshotSigner(key, ttl_s=10.0, clock=lambda: 1000.0)
    att = _attestation(signer, frame_hash, size)

    # a different image → different RGB hash → the binding must fail closed
    other = Image.new("RGB", (6, 4), (99, 99, 99))
    buf = io.BytesIO()
    other.save(buf, format="PNG")
    with pytest.raises(ObserverError, match="frame hash mismatch"):
        verify_attested_frame(att, buf.getvalue(), key, now=1001.0)
