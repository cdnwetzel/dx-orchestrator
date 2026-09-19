"""Model-availability classification — the distinction TCP reachability can't make.

Pins the four states SP9's incident needed: resident (will serve), on-disk-cold
(will stall — the state that read green then failed), not-served (misconfigured),
and unreachable. The classifiers are pure; the HTTP probe is exercised with the
network stubbed so the states are asserted without a live node.
"""

from __future__ import annotations

from dx import model_probe as mp
from dx.model_probe import ModelAvailability, classify_ollama, classify_openai, probe_model


def test_a_resident_ollama_model_is_ok():
    a = classify_ollama("gpt-oss:20b", tags=["gpt-oss:20b", "q36-moe"], resident=["gpt-oss:20b"])
    assert a.status == mp.RESIDENT and a.ok


def test_an_on_disk_but_not_resident_model_is_not_ok():
    # SP9's exact case: pulled, but never resident -> will cold-load / stall.
    a = classify_ollama("q36-moe", tags=["q36-moe", "qwen2.5:14b"], resident=["qwen2.5:14b"])
    assert a.status == mp.ON_DISK_COLD and not a.ok
    assert "cold-load" in a.detail


def test_a_model_not_on_the_node_is_not_served():
    a = classify_ollama("llama3.3:70b", tags=["qwen2.5:14b"], resident=["qwen2.5:14b"])
    assert a.status == mp.NOT_SERVED and not a.ok


def test_an_openai_listed_model_is_served_not_resident():
    # /v1/models can't report residency, so the honest state is 'served', not 'resident'.
    a = classify_openai("Qwen3-Coder", served=["Qwen3-Coder", "Nemotron-3.5"])
    assert a.status == mp.SERVED and a.ok


def test_an_openai_model_absent_from_the_list_is_not_served():
    a = classify_openai("PS-Legal-72B", served=["Qwen3-Coder"])
    assert a.status == mp.NOT_SERVED and not a.ok


# --- the HTTP probe, network stubbed ----------------------------------------


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise mp.requests.HTTPError(f"{self._status}")

    def json(self):
        return self._payload


def test_probe_ollama_reads_tags_and_ps(monkeypatch):
    def fake_get(url, timeout):
        if url.endswith("/api/tags"):
            return _Resp({"models": [{"name": "gpt-oss:20b"}, {"name": "q36-moe"}]})
        if url.endswith("/api/ps"):
            return _Resp({"models": [{"name": "gpt-oss:20b"}]})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(mp.requests, "get", fake_get)
    assert probe_model("http://n/", "ollama", "gpt-oss:20b").status == mp.RESIDENT
    assert probe_model("http://n/", "ollama", "q36-moe").status == mp.ON_DISK_COLD


def test_probe_openai_reads_v1_models(monkeypatch):
    monkeypatch.setattr(
        mp.requests, "get", lambda url, timeout, headers=None: _Resp({"data": [{"id": "Qwen3-Coder"}]})
    )
    assert probe_model("http://r:8888", "openai-compatible", "Qwen3-Coder").status == mp.SERVED
    assert probe_model("http://r:8888", "openai-compatible", "Nemotron-3.5").status == mp.NOT_SERVED


def test_a_dead_endpoint_is_unreachable_not_an_exception(monkeypatch):
    def boom(url, timeout, headers=None):
        raise mp.requests.ConnectionError("refused")

    monkeypatch.setattr(mp.requests, "get", boom)
    a = probe_model("http://down:11434", "ollama", "x")
    assert a.status == mp.UNREACHABLE and not a.ok


def test_a_trailing_v1_is_normalized_not_doubled(monkeypatch):
    # config_loader strips /v1 for the runtime route; the probe must too, or an
    # endpoint written with /v1 probes /v1/v1/models and misclassifies.
    seen = {}

    def fake_get(url, timeout, headers=None):
        seen["url"] = url
        return _Resp({"data": [{"id": "Qwen3-Coder"}]})

    monkeypatch.setattr(mp.requests, "get", fake_get)
    assert probe_model("http://r:8888/v1", "openai-compatible", "Qwen3-Coder").status == mp.SERVED
    assert seen["url"] == "http://r:8888/v1/models"  # not /v1/v1/models


def test_a_4xx_with_a_json_body_is_unreachable_not_not_served(monkeypatch):
    # An auth/error response carrying JSON must not read as "model absent".
    monkeypatch.setattr(mp.requests, "get", lambda url, timeout, headers=None: _Resp({"error": "unauthorized"}, status=401))
    a = probe_model("http://r:8888", "openai-compatible", "Qwen3-Coder")
    assert a.status == mp.UNREACHABLE


def test_a_non_object_json_body_is_unreachable_not_a_crash(monkeypatch):
    # A list/str payload must not raise AttributeError out of probe_model (which
    # would abort every later probe in dx doctor).
    monkeypatch.setattr(mp.requests, "get", lambda url, timeout, headers=None: _Resp(["not", "a", "map"]))
    assert probe_model("http://n:11434", "ollama", "x").status == mp.UNREACHABLE


def test_a_non_list_collection_is_unreachable_not_not_served(monkeypatch):
    # {"models": {}} / {"data": {}} are broken responses (the contracts are lists);
    # they must read unreachable, not misclassify a served model as absent.
    monkeypatch.setattr(mp.requests, "get", lambda url, timeout, headers=None: _Resp({"models": {}}))
    assert probe_model("http://n:11434", "ollama", "x").status == mp.UNREACHABLE
    monkeypatch.setattr(mp.requests, "get", lambda url, timeout, headers=None: _Resp({"data": {}}))
    assert probe_model("http://r:8888", "openai-compatible", "x").status == mp.UNREACHABLE


def test_autodetect_uses_ollama_residency_when_the_node_speaks_it(monkeypatch):
    # The psoperator case: an ollama-backed endpoint whose bound model is on disk
    # but not resident must read on-disk-cold — the landmine /v1/models would hide.
    def fake_get(url, timeout, headers=None):
        if url.endswith("/api/tags"):
            return _Resp({"models": [{"name": "q36-moe"}, {"name": "small"}]})
        if url.endswith("/api/ps"):
            return _Resp({"models": [{"name": "small"}]})  # q36-moe not resident
        raise AssertionError(f"should not hit {url}")

    monkeypatch.setattr(mp.requests, "get", fake_get)
    from dx.model_probe import probe_model_autodetect

    assert probe_model_autodetect("http://n:11434/v1", "q36-moe").status == mp.ON_DISK_COLD


def test_autodetect_does_not_fall_back_to_v1_after_a_confirmed_ollama_node(monkeypatch):
    # Once /api/tags confirms ollama, a /api/ps failure must read UNREACHABLE — NOT
    # fall back to /v1/models, which would call an on-disk-cold model "served" and
    # reopen the exact blind spot this probe closes.
    from dx.model_probe import probe_model_autodetect

    def fake_get(url, timeout, headers=None):
        if url.endswith("/api/tags"):
            return _Resp({"models": [{"name": "q36-moe"}]})   # it IS ollama
        if url.endswith("/api/ps"):
            raise mp.requests.ConnectionError("ps timed out")  # residency unknowable
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": "q36-moe"}]})        # would falsely say SERVED
        raise AssertionError(url)

    monkeypatch.setattr(mp.requests, "get", fake_get)
    a = probe_model_autodetect("http://n:11434/v1", "q36-moe")
    assert a.status == mp.UNREACHABLE, "must not launder on-disk-cold into served via /v1/models"


def test_autodetect_falls_back_to_v1_models_for_a_non_ollama_node(monkeypatch):
    from dx.model_probe import probe_model_autodetect

    def fake_get(url, timeout, headers=None):
        if url.endswith("/api/tags") or url.endswith("/api/ps"):
            raise mp.requests.HTTPError("404")  # not an ollama node
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": "Qwen3-Coder"}]})
        raise AssertionError(url)

    monkeypatch.setattr(mp.requests, "get", fake_get)
    assert probe_model_autodetect("http://r:8888/v1", "Qwen3-Coder").status == mp.SERVED


def test_pxx_api_key_is_sent_when_set(monkeypatch):
    monkeypatch.setenv("PXX_API_KEY", "secret-xyz")
    captured = {}

    def fake_get(url, timeout, headers=None):
        captured["headers"] = headers or {}
        return _Resp({"data": [{"id": "m"}]})

    monkeypatch.setattr(mp.requests, "get", fake_get)
    probe_model("http://r:8888", "openai-compatible", "m")
    assert captured["headers"].get("Authorization") == "Bearer secret-xyz"


def test_ok_is_only_resident_or_served():
    assert ModelAvailability(mp.RESIDENT, "").ok
    assert ModelAvailability(mp.SERVED, "").ok
    assert not ModelAvailability(mp.ON_DISK_COLD, "").ok
    assert not ModelAvailability(mp.NOT_SERVED, "").ok
    assert not ModelAvailability(mp.UNREACHABLE, "").ok


# --- --deep latency probe -------------------------------------------------


def test_latency_fast_call_is_not_slow(monkeypatch):
    from dx.model_probe import measure_latency

    monkeypatch.setattr(mp.requests, "post", lambda url, json, timeout, headers=None: _Resp({"ok": True}))
    lat = measure_latency("http://n:11434", "ollama", "m", warn_ms=5000.0)
    assert lat.ok and not lat.slow and lat.elapsed_ms is not None


def test_latency_over_threshold_is_flagged_slow(monkeypatch):
    import dx.model_probe as m

    # freeze a big elapsed by advancing the module's monotonic clock between calls
    ticks = iter([100.0, 106.0])  # 6 s elapsed
    monkeypatch.setattr(m.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(m.requests, "post", lambda url, json, timeout, headers=None: _Resp({"ok": True}))
    lat = m.measure_latency("http://n:11434", "ollama", "m", warn_ms=5000.0)
    assert lat.ok and lat.slow and "degraded or under load" in lat.detail


def test_latency_failed_call_is_not_ok(monkeypatch):
    from dx.model_probe import measure_latency

    def boom(url, json, timeout, headers=None):
        raise mp.requests.ConnectionError("refused")

    monkeypatch.setattr(mp.requests, "post", boom)
    lat = measure_latency("http://n:11434", "ollama", "m")
    assert not lat.ok and lat.slow and lat.elapsed_ms is None


def test_latency_openai_uses_chat_completions(monkeypatch):
    from dx.model_probe import measure_latency

    seen = {}

    def fake_post(url, json, timeout, headers=None):
        seen["url"] = url
        return _Resp({"ok": True})

    monkeypatch.setattr(mp.requests, "post", fake_post)
    measure_latency("http://r:8888/v1", "openai-compatible", "m")
    assert seen["url"] == "http://r:8888/v1/chat/completions"


def test_measure_latency_autodetect_uses_ollama_generate_when_node_is_ollama(monkeypatch):
    from dx.model_probe import measure_latency_autodetect

    seen = {}

    def fake_get(url, timeout, headers=None):
        seen["get"] = url
        return _Resp({"models": []})  # /api/tags answers -> it's ollama

    def fake_post(url, json, timeout, headers=None):
        seen["post"] = url
        return _Resp({"ok": True})

    monkeypatch.setattr(mp.requests, "get", fake_get)
    monkeypatch.setattr(mp.requests, "post", fake_post)
    lat = measure_latency_autodetect("http://n:11434/v1", "m")
    assert seen["get"] == "http://n:11434/api/tags"  # detection actually probed /api/tags
    assert lat.ok and seen["post"] == "http://n:11434/api/generate"  # ollama path, not /v1/chat


def test_measure_latency_autodetect_falls_back_to_chat_for_non_ollama(monkeypatch):
    from dx.model_probe import measure_latency_autodetect

    seen = {}

    def fake_get(url, timeout, headers=None):
        seen["get"] = url
        raise mp.requests.HTTPError("404")  # not ollama

    def fake_post(url, json, timeout, headers=None):
        seen["post"] = url
        return _Resp({"ok": True})

    monkeypatch.setattr(mp.requests, "get", fake_get)
    monkeypatch.setattr(mp.requests, "post", fake_post)
    lat = measure_latency_autodetect("http://r:8888/v1", "m")
    assert seen["get"] == "http://r:8888/api/tags"  # detection probed /api/tags first
    assert lat.ok and seen["post"] == "http://r:8888/v1/chat/completions"


# ---------------------------------------------------------------------------
# Name resolution — the corpus in tests/fixtures/model-names.jsonl
# ---------------------------------------------------------------------------
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from dx.model_probe import NOT_SERVED, hint_candidates  # noqa: E402

_CORPUS = [
    json.loads(line)
    for line in (Path(__file__).parent / "fixtures" / "model-names.jsonl").read_text().splitlines()
    if line.strip()
]


def _classify(row):
    if row["provider"] == "ollama":
        return classify_ollama(row["manifest"], tags=row["served"], resident=[])
    return classify_openai(row["manifest"], served=row["served"])


@pytest.mark.parametrize("row", _CORPUS, ids=[f"{r['provider']}:{r['manifest'] or '<empty>'}" for r in _CORPUS])
def test_resolution_matches_what_the_backend_would_do(row):
    """`resolves` is the served name a request with this manifest name actually
    hits — Ollama's :latest/library//case rules, OpenAI's exact ids — labeled
    by hand for 42 real shapes. Exact membership got 34 of them."""
    a = _classify(row)
    if row["resolves"] == "none":
        assert a.status == NOT_SERVED, (row, a)
        assert a.resolves_to is None
    else:
        assert a.status != NOT_SERVED, (row, a)
        assert a.resolves_to == row["resolves"], (row, a)


@pytest.mark.parametrize("row", [r for r in _CORPUS if r["resolves"] == "none"], ids=[f"{r['provider']}:{r['manifest'] or '<empty>'}" for r in _CORPUS if r["resolves"] == "none"])
def test_not_served_carries_the_agreed_hint(row):
    """A hint never changes the status; it is text for the operator. The
    expectations were fixed against a dev-time labeling pass (24/26 agreement)
    and are pinned here so the heuristic cannot drift silently."""
    a = _classify(row)
    assert a.status == NOT_SERVED
    if row["hint"] == "none":
        assert a.hints == () and "did you mean" not in a.detail
    elif row["hint"] == "ambiguous":
        assert len(a.hints) > 1 and "ambiguous" in a.detail
    else:
        assert a.hints == (row["hint"],)
        assert f"did you mean '{row['hint']}'?" in a.detail


def test_the_sp9_shape_is_not_served_with_a_hint():
    """The incident this module exists for: the manifest says the model, the
    node has it under a size/quant tag, and the operator was told the binding
    was broken with nothing to go on."""
    a = classify_ollama("qwen2.5-coder", tags=["qwen2.5-coder:32b-instruct-q4_K_M"], resident=[])
    assert a.status == NOT_SERVED and not a.ok
    assert a.hints == ("qwen2.5-coder:32b-instruct-q4_K_M",)
    assert "did you mean 'qwen2.5-coder:32b-instruct-q4_K_M'?" in a.detail


def test_a_bare_ollama_name_resolves_to_latest_for_residency_too():
    """The same rule decides resident vs on-disk-cold: a bare name in the
    manifest must find `name:latest` in /api/ps, or a resident model reads cold."""
    a = classify_ollama("phi4", tags=["phi4:latest"], resident=["phi4:latest"])
    assert a.status == "resident" and a.resolves_to == "phi4:latest"


def test_ollama_is_case_insensitive_but_openai_is_not():
    assert classify_ollama("Qwen2.5-Coder:32B", tags=["qwen2.5-coder:32b"], resident=[]).ok is False  # cold, but resolved
    assert classify_ollama("Qwen2.5-Coder:32B", tags=["qwen2.5-coder:32b"], resident=[]).resolves_to == "qwen2.5-coder:32b"
    a = classify_openai("qwen/qwen3-coder", served=["Qwen/Qwen3-Coder"])
    assert a.status == NOT_SERVED and a.hints == ("Qwen/Qwen3-Coder",)


def test_a_digest_reference_resolves_only_to_itself():
    a = classify_ollama("qwen2.5-coder@sha256:0123", tags=["qwen2.5-coder:7b"], resident=[])
    assert a.status == NOT_SERVED
    assert a.hints == ("qwen2.5-coder:7b",)


def test_a_hint_never_promotes_the_status():
    """RL-007 in miniature: the guess is text, the status is the rule."""
    for row in _CORPUS:
        a = _classify(row)
        if a.hints:
            assert a.status == NOT_SERVED and not a.ok


def test_hint_candidates_is_empty_for_an_empty_name():
    assert hint_candidates("", ["qwen2.5-coder:7b"]) == ()
    assert hint_candidates("   ", ["qwen2.5-coder:7b"]) == ()
