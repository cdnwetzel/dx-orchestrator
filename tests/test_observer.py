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


class TestFrameHashContract:
    """The cross-repo invariant with no guard until now: dx.observer's reader and
    psoperator's writer must agree on the frame hash. dx recomputes
    sha256(RGB pixels) of a PNG; psoperator computes it from a PIL image at
    capture. The day psoperator changes its RGB packing or `convert("RGB")`, this
    fails in CI — not fails closed at a customer's desk. Same discipline as the
    ledger's canonical-form contract test: exercise both real code paths and
    assert byte-identical output."""

    @staticmethod
    def _png(img) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_rgb_image_hashes_identically_both_ways(self):
        from psoperator.perception.capture import Frame

        img = Image.new("RGB", (7, 5), (10, 20, 30))
        img.putpixel((2, 3), (200, 100, 50))
        # psoperator's real writer path:
        psop_hash = Frame.from_image(0, img).sha256
        # dx's real reader path, from the encoded PNG:
        dx_hash = frame_rgb_sha256(self._png(img))
        assert dx_hash == psop_hash

    def test_agreement_holds_through_an_rgba_source(self):
        """psoperator converts to RGB; dx converts to RGB. A source with alpha
        must still land on the same digest, or the two would silently disagree on
        any screenshot with a cursor/overlay alpha channel."""
        from psoperator.perception.capture import Frame

        rgba = Image.new("RGBA", (6, 6), (12, 34, 56, 200))
        rgba.putpixel((1, 1), (250, 0, 0, 255))
        psop_hash = Frame.from_image(1, rgba).sha256  # converts to RGB internally
        dx_hash = frame_rgb_sha256(self._png(rgba))   # convert("RGB") on the PNG
        assert dx_hash == psop_hash

    def test_a_different_frame_does_not_collide(self):
        """The guard is worthless if every image hashes the same — prove the two
        agree on a *distinction*, not just a value."""
        from psoperator.perception.capture import Frame

        a = Image.new("RGB", (4, 4), (0, 0, 0))
        b = Image.new("RGB", (4, 4), (0, 0, 1))
        assert Frame.from_image(0, a).sha256 == frame_rgb_sha256(self._png(a))
        assert Frame.from_image(0, b).sha256 == frame_rgb_sha256(self._png(b))
        assert frame_rgb_sha256(self._png(a)) != frame_rgb_sha256(self._png(b))
