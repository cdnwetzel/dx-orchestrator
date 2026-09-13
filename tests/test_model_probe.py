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
    def __init__(self, payload):
        self._payload = payload

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
        mp.requests, "get", lambda url, timeout: _Resp({"data": [{"id": "Qwen3-Coder"}]})
    )
    assert probe_model("http://r:8888", "openai-compatible", "Qwen3-Coder").status == mp.SERVED
    assert probe_model("http://r:8888", "openai-compatible", "Nemotron-3.5").status == mp.NOT_SERVED


def test_a_dead_endpoint_is_unreachable_not_an_exception(monkeypatch):
    def boom(url, timeout):
        raise mp.requests.ConnectionError("refused")

    monkeypatch.setattr(mp.requests, "get", boom)
    a = probe_model("http://down:11434", "ollama", "x")
    assert a.status == mp.UNREACHABLE and not a.ok


def test_ok_is_only_resident_or_served():
    assert ModelAvailability(mp.RESIDENT, "").ok
    assert ModelAvailability(mp.SERVED, "").ok
    assert not ModelAvailability(mp.ON_DISK_COLD, "").ok
    assert not ModelAvailability(mp.NOT_SERVED, "").ok
    assert not ModelAvailability(mp.UNREACHABLE, "").ok
