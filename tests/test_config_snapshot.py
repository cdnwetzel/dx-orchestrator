"""S3 — the manifest-digest evidence: one read, a collision-free canonical
form, and a captured configuration record that is written verbatim."""
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from dx.config_loader import (
    CanonicalizationError,
    canonical_manifest_json,
    get_config_snapshot,
    load_config,
    untag_canonical,
)
from dx.effective_config import (
    capture_effective_config,
    observe_manifest,
    strip_endpoint,
)
from dx.redact import redact

BASE = 'roles:\n  default:\n    endpoint: "http://h.invalid:1"\n    provider: "vllm"\n    model: "m"\n'


def _manifest(tmp_path: Path, monkeypatch, text: str = BASE, name: str = "m.yml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("DX_CONFIG", str(path))
    return path


def _nmd(mapping) -> str:
    return hashlib.sha256(canonical_manifest_json(mapping)).hexdigest()


# --- one read, one record ---------------------------------------------------

def test_one_read_per_load_and_the_digests_come_from_it(tmp_path, monkeypatch):
    path = _manifest(tmp_path, monkeypatch)
    reads = []
    real = Path.read_bytes

    def counting(self):
        data = real(self)
        if self == path:
            reads.append(data)
        return data
    monkeypatch.setattr(Path, "read_bytes", counting)
    load_config(force=True)
    snap = get_config_snapshot()
    snap_again = get_config_snapshot()
    assert len(reads) == 1, "load_config must read the file exactly once"
    assert snap is snap_again, "get_config_snapshot never re-reads"
    assert snap.raw_sha256 == hashlib.sha256(reads[0]).hexdigest()
    assert snap.size_bytes == len(reads[0])
    assert snap.normalized_sha256 == _nmd(load_config())


def test_force_replaces_snapshot_and_mapping_together(tmp_path, monkeypatch):
    path = _manifest(tmp_path, monkeypatch)
    load_config(force=True)
    before = get_config_snapshot()
    path.write_text(BASE + "ledger:\n  repo: /x\n", encoding="utf-8")
    assert get_config_snapshot() is before, "no re-read without force"
    load_config(force=True)
    after = get_config_snapshot()
    assert after.raw_sha256 != before.raw_sha256
    assert "ledger" in load_config()


def test_dx_config_change_yields_a_new_snapshot(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, name="a.yml")
    load_config()
    a = get_config_snapshot()
    _manifest(tmp_path, monkeypatch, BASE + "# other\n", name="b.yml")
    b = get_config_snapshot()
    assert a.path != b.path and a.raw_sha256 != b.raw_sha256
    assert a.normalized_sha256 == b.normalized_sha256


# --- raw versus normalized -------------------------------------------------

def test_comment_whitespace_and_order_change_raw_not_normalized(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    load_config(force=True)
    a = get_config_snapshot()
    reordered = ('# a comment\nroles:\n\n  default:\n    model: "m"\n    provider: "vllm"\n'
                 '    endpoint: "http://h.invalid:1"\n')
    _manifest(tmp_path, monkeypatch, reordered, name="r.yml")
    b = get_config_snapshot()
    assert a.raw_sha256 != b.raw_sha256
    assert a.normalized_sha256 == b.normalized_sha256


def test_a_value_change_changes_both(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    a = get_config_snapshot()
    _manifest(tmp_path, monkeypatch, BASE.replace('"m"', '"m2"'), name="v.yml")
    b = get_config_snapshot()
    assert a.raw_sha256 != b.raw_sha256 and a.normalized_sha256 != b.normalized_sha256


# --- the canonicalization domain, row by row --------------------------------

def test_mapping_keys_carry_the_k_prefix_and_tags_the_t_prefix():
    out = json.loads(canonical_manifest_json({"a": {"b": [1, "x", None, True]}}))
    assert out == {"k:a": {"k:b": [1, "x", None, True]}}


def test_a_key_that_looks_like_a_tag_cannot_collide_with_a_tag():
    literal = {"t:timestamp": "2026-10-12"}          # a string value under an odd key
    real = {"when": dt.date(2026, 10, 12)}           # a real timestamp value
    assert json.loads(canonical_manifest_json(literal)) == {"k:t:timestamp": "2026-10-12"}
    assert json.loads(canonical_manifest_json(real)) == {"k:when": {"t:timestamp": "2026-10-12"}}
    assert _nmd(literal) != _nmd(real)
    odd = {"__yaml__": "x"}
    assert json.loads(canonical_manifest_json(odd)) == {"k:__yaml__": "x"}


def test_timestamps_from_yaml_are_tagged_and_round_trip(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, BASE + "d: 2026-10-12\nt: 2026-10-12T10:00:00\n", name="t.yml")
    m = load_config(force=True)
    assert isinstance(m["d"], dt.date) and isinstance(m["t"], dt.datetime)
    tagged = json.loads(canonical_manifest_json(m))
    assert tagged["k:d"] == {"t:timestamp": "2026-10-12"}
    assert untag_canonical(tagged) == m


def test_binary_is_content_addressed_not_reversible(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, BASE + "b: !!binary aGk=\n", name="b.yml")
    m = load_config(force=True)
    assert m["b"] == b"hi"
    tagged = json.loads(canonical_manifest_json(m))
    assert tagged["k:b"] == {"t:binary": {"sha256": hashlib.sha256(b"hi").hexdigest(), "len": 2}}
    back = untag_canonical(tagged)
    assert back["b"] == tagged["k:b"], "binary comes back as its tag, never as bytes"
    assert _nmd({"b": b"hi"}) != _nmd({"b": b"ho"})
    assert _nmd({"b": b"hi"}) == _nmd({"b": b"hi"})


def test_non_finite_floats_are_tagged(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, BASE + "n: .nan\np: .inf\nm: -.inf\n", name="f.yml")
    tagged = json.loads(canonical_manifest_json(load_config(force=True)))
    assert tagged["k:n"] == {"t:float": "nan"}
    assert tagged["k:p"] == {"t:float": "inf"} and tagged["k:m"] == {"t:float": "-inf"}


def test_non_string_key_is_unsupported_but_loads(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, BASE + "1: a\n", name="k.yml")
    load_config(force=True)                      # no new failure mode
    snap = get_config_snapshot()
    assert snap.normalized_sha256 is None
    assert "non-string key" in snap.normalization_error
    assert snap.raw_sha256


def test_cycle_is_unsupported_but_loads(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, BASE + "a: &a [1, *a]\n", name="c.yml")
    load_config(force=True)
    snap = get_config_snapshot()
    assert snap.normalized_sha256 is None and "cycle" in snap.normalization_error
    with pytest.raises(CanonicalizationError):
        canonical_manifest_json(load_config())


def test_duplicate_keys_differ_raw_and_agree_normalized(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, BASE + "x: 1\nx: 2\n", name="d.yml")
    dup = get_config_snapshot()
    _manifest(tmp_path, monkeypatch, BASE + "x: 2\n", name="e.yml")
    single = get_config_snapshot()
    assert dup.raw_sha256 != single.raw_sha256
    assert dup.normalized_sha256 == single.normalized_sha256


def test_anchors_are_resolved_before_hashing(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, "base: &b\n  k: v\nroles:\n  default: *b\n", name="a.yml")
    _ = load_config(force=True)
    a = get_config_snapshot()
    _manifest(tmp_path, monkeypatch, "base:\n  k: v\nroles:\n  default:\n    k: v\n", name="n.yml")
    b = get_config_snapshot()
    assert a.normalized_sha256 == b.normalized_sha256


def test_round_trip_over_the_supported_types():
    original = {"s": "x", "i": 3, "f": 1.5, "b": True, "n": None, "l": [1, {"q": "r"}],
                "d": dt.date(2026, 1, 2), "t": dt.datetime(2026, 1, 2, 3, 4, 5)}
    assert untag_canonical(json.loads(canonical_manifest_json(original))) == original


# --- the captured record ---------------------------------------------------

def _capture(tmp_path, monkeypatch, **kw):
    _manifest(tmp_path, monkeypatch)
    load_config(force=True)
    return capture_effective_config(cli_overrides={"task_branch": True}, **kw)


def test_the_record_holds_digests_and_no_manifest_values(tmp_path, monkeypatch):
    eff = _capture(tmp_path, monkeypatch)
    rec = json.loads(eff.json_text)
    assert rec["manifest"]["raw_sha256"] == eff.snapshot.raw_sha256
    assert "effective_sha256" not in eff.json_text, "the artifact never contains its own digest"
    assert rec["overrides_cli"] == {"task_branch": True}
    assert set(rec) == {"manifest", "manifest_unavailable", "overrides_env", "overrides_cli",
                        "resolved", "captured_utc"}
    assert rec["manifest_unavailable"] is None
    assert set(rec["resolved"]) == {"ledger_repo", "roles_path", "evidence_root", "branches",
                                    "review", "approval", "route"}
    assert eff.sha256 == hashlib.sha256(eff.json_text.encode()).hexdigest()


def test_env_overrides_record_names_and_paths_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DX_LEDGER_REPO", str(tmp_path / "ledger"))
    monkeypatch.setenv("DX_ROLES_PATH", "not-a-path-token-value")
    eff = _capture(tmp_path, monkeypatch)
    env = json.loads(eff.json_text)["overrides_env"]
    assert env["DX_LEDGER_REPO"] == str(tmp_path / "ledger")
    assert env["DX_ROLES_PATH"].startswith("<non-path, sha256:")
    assert "not-a-path-token-value" not in eff.json_text


def test_endpoint_loses_userinfo_query_and_fragment(tmp_path, monkeypatch):
    class Route:
        endpoint = "http://u:p@h.invalid:1/path?k=v#frag"
        model, provider, timeout_s = "m", "vllm", None
    eff = _capture(tmp_path, monkeypatch, route=Route())
    route = json.loads(eff.json_text)["resolved"]["route"]
    assert route["endpoint"] == "http://h.invalid:1/path"
    assert route["endpoint_stripped"] == ["userinfo", "query", "fragment"]
    assert "u:p@" not in eff.json_text and "k=v" not in eff.json_text
    assert strip_endpoint("not a url") == ("not a url", [])


def test_a_credential_in_an_override_is_redacted_at_capture_and_the_digest_matches_the_write(tmp_path, monkeypatch):
    monkeypatch.setenv("DX_EVIDENCE_DIR", "/tmp/sk-ant-api03-" + "A" * 95)  # an absolute path shaped like a key
    eff = _capture(tmp_path, monkeypatch)
    assert "sk-ant-api03" not in eff.json_text
    assert "[REDACTED:" in eff.json_text
    written, _ = redact(eff.json_text)          # what _finalize will do once more
    assert written == eff.json_text, "redaction is idempotent, so the written bytes equal the captured bytes"
    assert eff.check(written).ok


def test_the_check_fails_when_the_bytes_differ(tmp_path, monkeypatch):
    eff = _capture(tmp_path, monkeypatch)
    assert eff.check(eff.json_text).ok
    bad = eff.check(eff.json_text + " ")
    assert not bad.ok and "differ" in bad.detail
    assert eff.sha256 in bad.detail and eff.snapshot.raw_sha256 in bad.detail


# --- the observation -------------------------------------------------------

def test_observation_records_a_hash_or_unavailable_never_raises(tmp_path):
    path = tmp_path / "m.yml"
    path.write_text("a: 1\n")
    obs = json.loads(observe_manifest(path))
    assert obs["post_run_raw_sha256"] == hashlib.sha256(b"a: 1\n").hexdigest()
    assert obs["unavailable"] is None
    gone = json.loads(observe_manifest(tmp_path / "missing.yml"))
    assert gone["post_run_raw_sha256"] is None
    assert gone["unavailable"].startswith("FileNotFoundError")


def test_observation_error_text_is_redacted(tmp_path):
    secret_dir = tmp_path / ("sk-ant-api03-" + "B" * 95)
    obs = json.loads(observe_manifest(secret_dir / "m.yml"))
    assert "sk-ant-api03" not in obs["unavailable"]
    assert "[REDACTED:" in obs["unavailable"]


def test_no_manifest_is_recorded_as_such_not_invented(tmp_path, monkeypatch):
    """`dx merge` with DX_LEDGER_REPO set runs without a manifest; the record
    says so, the check still verifies the written bytes, and the observation
    is `unavailable`."""
    monkeypatch.setenv("DX_CONFIG", str(tmp_path / "absent.yml"))
    eff = capture_effective_config(cli_overrides={})
    rec = json.loads(eff.json_text)
    assert rec["manifest"] is None and rec["manifest_unavailable"].startswith("Hardware manifest not found")
    assert eff.snapshot is None
    check = eff.check(eff.json_text)
    assert check.ok and "manifest=none" in check.detail
    assert json.loads(eff.observe())["unavailable"] == "no manifest was loaded"
