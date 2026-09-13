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
