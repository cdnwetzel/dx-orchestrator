"""Model-availability probing — the check that would have told SP9 in one second
what a fifteen-minute pair of load attempts did.

``dx doctor``'s network check tests TCP reachability, which a live node answers
even when the model bound to it cannot serve: the endpoint is up, the port
connects, and the *bound model* is on disk but not resident (so it cold-loads or,
on a capacity-degraded node, never loads at all). "Reachable" then reads green
while every task on that tier fails ``MODEL_UNAVAILABLE``. This module asks the
node what it can actually serve and classifies the answer.

Four states, because SP9's incident needed all of them:

* **resident** — the model is loaded now (ollama ``/api/ps``); it will serve.
* **on-disk-cold** — pulled but not resident (in ``/api/tags``, not ``/api/ps``);
  it must cold-load, which stalls or fails on a constrained node.
* **not-served** — reachable but the model is not offered at all (absent from
  ``/api/tags`` or the OpenAI ``/v1/models`` list); a misconfigured binding.
* **unreachable** — the endpoint itself did not answer.

For an OpenAI-compatible/vLLM endpoint residency is opaque, so a listed model is
reported **served** rather than resident — the honest limit of what ``/v1/models``
can tell us.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

RESIDENT = "resident"
ON_DISK_COLD = "on-disk-cold"
SERVED = "served"
NOT_SERVED = "not-served"
UNREACHABLE = "unreachable"

#: Statuses for which the model can plausibly answer a request right now.
_OK = frozenset({RESIDENT, SERVED})


@dataclass(frozen=True)
class ModelAvailability:
    """The result of a probe. ``ok`` is True only when the model can plausibly
    serve now — an on-disk-cold model is deliberately *not* ok, because that is
    exactly the state that read green and then failed.

    ``resolves_to`` is the served name the manifest's name actually resolves
    to, when it does (``qwen2.5-coder`` → ``qwen2.5-coder:latest`` on Ollama).
    ``hints`` are served names a *not-served* manifest name probably meant —
    advisory text for the operator, never a status."""

    status: str
    detail: str
    resolves_to: str | None = None
    hints: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in _OK


# ---------------------------------------------------------------------------
# Name resolution. Two questions used to hide inside `model in tags`, and only
# one of them has a deterministic answer:
#
#   Would a request with this name be served?  Ollama resolves a bare name to
#   `:latest`, drops `library/` and the registry prefix, and is case-insensitive;
#   an OpenAI-compatible id must match exactly. That is what the backends do,
#   so that is the rule — 42/42 against a hand-labeled corpus of real shapes
#   (`~/ai/review/typesafe/corpus/model-names.jsonl`), where exact membership
#   scored 34/42: `qwen2.5-coder` against `qwen2.5-coder:latest` read not-served
#   and the operator was told their binding was broken.
#
#   Which served name did the operator *mean*?  A judgment, not a rule. It is
#   offered only as a "did you mean" hint on a not-served result, from a
#   deliberately recall-leaning base-name comparison that a dev-time labeling
#   pass backed on 24 of the 26 not-served corpus rows. The status is never
#   changed by it (RL-007: a guess informs the operator; it never decides).
# ---------------------------------------------------------------------------
_OLLAMA_PREFIXES = ("registry.ollama.ai/library/", "library/")


def _ollama_key(name: str) -> str:
    """The name Ollama would look up: lower-cased, registry/library prefix
    dropped, bare name given `:latest`. A digest reference resolves only to
    itself, so it is left untagged."""
    n = name.strip().lower()
    for prefix in _OLLAMA_PREFIXES:
        if n.startswith(prefix):
            n = n[len(prefix):]
    if "@sha256:" in n or ":" in n:
        return n
    return f"{n}:latest"


def _resolve_ollama(model: str, names: list[str]) -> str | None:
    want = _ollama_key(model)
    for name in names:
        if _ollama_key(name) == want:
            return name
    return None


def _base(name: str) -> str:
    """Model base for hinting: org, tag and digest stripped, lower-cased,
    non-alphanumerics dropped so `gemma4` meets `gemma-4-26b-it`."""
    n = name.strip().lower().split("/")[-1].split("@")[0].split(":")[0]
    return "".join(ch for ch in n if ch.isalnum())


def hint_candidates(model: str, served: list[str]) -> tuple[str, ...]:
    """Served names a not-served ``model`` probably meant. Recall-leaning on
    purpose — a wrong hint on an already-failed check costs a glance; a missing
    one costs a trip to /api/tags."""
    want = _base(model)
    if not want:
        return ()
    # Either side may be the longer one: a manifest saying `gemma-4-26b-it`
    # against a node serving `gemma4:26b` is the same near-miss as the reverse.
    return tuple(
        name for name in served
        if (have := _base(name)) and (have.startswith(want) or want.startswith(have))
    )


def _not_served(model: str, where: str, served: list[str]) -> ModelAvailability:
    hints = hint_candidates(model, served)
    if len(hints) == 1:
        tail = f" — did you mean '{hints[0]}'?"
    elif hints:
        tail = " — similar names served: " + ", ".join(f"'{h}'" for h in hints) + " (ambiguous)"
    else:
        tail = " — not pulled on this node" if where == "/api/tags" else ""
    return ModelAvailability(NOT_SERVED, f"{model} is not in {where}{tail}", hints=hints)


def classify_ollama(model: str, *, tags: list[str], resident: list[str]) -> ModelAvailability:
    """Classify an Ollama-backed model from its ``/api/tags`` (on disk) and
    ``/api/ps`` (resident) name lists, resolving the name the way Ollama does."""
    hit = _resolve_ollama(model, resident)
    if hit is not None:
        return ModelAvailability(RESIDENT, f"{model} is resident (as {hit})", resolves_to=hit)
    hit = _resolve_ollama(model, tags)
    if hit is not None:
        return ModelAvailability(
            ON_DISK_COLD,
            f"{model} is on disk (as {hit}) but not resident — it will cold-load (may stall)",
            resolves_to=hit,
        )
    return _not_served(model, "/api/tags", tags)


def classify_openai(model: str, *, served: list[str]) -> ModelAvailability:
    """Classify an OpenAI-compatible/vLLM model from its ``/v1/models`` list.
    Ids are exact (case included); residency is opaque here, so a listed model
    is 'served', not 'resident'."""
    wanted = model.strip()
    if wanted in served:
        return ModelAvailability(
            SERVED, f"{model} is served (residency opaque via /v1/models)", resolves_to=wanted
        )
    return _not_served(model, "/v1/models", served)


def _names(models: object) -> list[str]:
    """Model names from a ``/api/tags``, ``/api/ps``, or ``/v1/models`` collection.
    All three contracts define a **list**; a missing or non-list value is a broken
    response, so raise (the caller turns that into ``unreachable``) rather than
    silently returning ``[]`` and misreading a served model as not-served. An empty
    list is legitimate (nothing resident / nothing served) and returns ``[]``."""
    if not isinstance(models, list):
        raise ValueError(f"expected a list collection, got {type(models).__name__}")
    out: list[str] = []
    for m in models:
        if isinstance(m, dict):
            name = m.get("name") or m.get("id")
            if isinstance(name, str):
                out.append(name)
    return out


def _auth_headers() -> dict[str, str]:
    """The credential `dx run` already passes to pxx. An OpenAI-compatible endpoint
    that requires it would 401 an unauthenticated probe, and an error body with no
    ``data`` would misclassify a served model as not-served — so carry it here too."""
    key = os.environ.get("PXX_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _json_object(resp: requests.Response) -> dict[str, object]:
    """A probe response is only usable if it is a 2xx carrying a JSON object.
    A 4xx/5xx (even with a JSON body) or a non-object payload is a failed probe,
    not a served/absent verdict — raise so the caller reports it unreachable."""
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def probe_model(
    endpoint: str, provider: str, model: str, *, timeout: float = 4.0
) -> ModelAvailability:
    """Ask ``endpoint`` what it serves and classify ``model`` against the answer.
    Any transport, status, or decode error is reported as ``unreachable`` — this is
    a non-critical diagnostic and must never raise into the caller (a raise here
    would abort every later model probe in ``dx doctor``)."""
    base = endpoint.rstrip("/")
    # dx's runtime route strips a trailing /v1 (config_loader.normalize_endpoint);
    # match it, or an endpoint written with /v1 would probe /v1/v1/models.
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    try:
        if provider == "ollama":
            tags = _names(_json_object(requests.get(f"{base}/api/tags", timeout=timeout)).get("models"))
            resident = _names(_json_object(requests.get(f"{base}/api/ps", timeout=timeout)).get("models"))
            return classify_ollama(model, tags=tags, resident=resident)
        # openai-compatible / vllm and anything else that speaks /v1/models. The
        # endpoint is operator-controlled manifest config, not request input, so the
        # static-analysis SSRF note does not apply.
        resp = requests.get(f"{base}/v1/models", timeout=timeout, headers=_auth_headers())
        served = _names(_json_object(resp).get("data"))
        return classify_openai(model, served=served)
    except (requests.RequestException, ValueError) as exc:
        return ModelAvailability(UNREACHABLE, f"{endpoint} did not answer ({type(exc).__name__})")


def probe_model_autodetect(endpoint: str, model: str, *, timeout: float = 4.0) -> ModelAvailability:
    """Probe an endpoint whose provider is not declared — notably
    ``psoperator.model_endpoint``, which is OpenAI-compatible by contract but is
    usually ollama-backed.

    Prefer the ollama residency path (``/api/ps``): it is the **only** way to catch
    *on-disk-cold*, and that is exactly the latent-landmine case this probe exists
    for (a model listed by ``/v1/models`` reads "served" even when it will not load).
    Fall back to ``/v1/models`` when the node does not speak the ollama API (a true
    vLLM/OpenAI endpoint), where residency is opaque anyway."""
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    # Step 1: is this an ollama node? /api/tags answering is the tell. Only a
    # failure HERE means "not ollama" and justifies the /v1/models fallback.
    try:
        tags = _names(_json_object(requests.get(f"{base}/api/tags", timeout=timeout)).get("models"))
    except (requests.RequestException, ValueError):
        return probe_model(endpoint, "openai-compatible", model, timeout=timeout)
    # Step 2: it IS ollama, so residency is the authoritative check. A /api/ps
    # failure now is UNREACHABLE — never fall back to /v1/models, which cannot see
    # residency and would read an on-disk-cold model as "served" (the exact blind
    # spot this probe exists to close).
    try:
        resident = _names(_json_object(requests.get(f"{base}/api/ps", timeout=timeout)).get("models"))
    except (requests.RequestException, ValueError) as exc:
        return ModelAvailability(UNREACHABLE, f"{endpoint} /api/ps did not answer ({type(exc).__name__})")
    return classify_ollama(model, tags=tags, resident=resident)


@dataclass(frozen=True)
class Latency:
    """A timed round-trip to a model. ``ok`` means the call returned; ``slow`` means
    it returned but past the warn threshold. A slow reading is a *point-in-time*
    fact — the model was slow **at probe time** — and does NOT by itself distinguish
    a persistently degraded node from one merely under concurrent load. It is the
    thing residency cannot see (a resident model that is nonetheless slow); the
    cause is for the operator to establish (re-probe idle; restart to test a stuck
    state). A failed/timed-out call is ``ok=False``."""

    ok: bool
    slow: bool
    elapsed_ms: float | None
    detail: str
    #: The ping came back with *something a model would say*. A resident model
    #: on a throttled node once answered ``?`` for every token, at 1.9 s, and
    #: read green on residency and latency both. ``ok`` keeps meaning "the call
    #: returned"; this is the garbage detector beside it, not a correctness check.
    sane: bool = True


_GARBAGE_CHARS = frozenset("?\ufffd")


def _ping_sane(provider: str, resp: requests.Response) -> tuple[bool, str]:
    """Did the 1-token ping come back with something a model would say?

    Insane: the generation reports ``done: false`` (aborted), the text is
    empty, or it is nothing but ``?`` / U+FFFD. Anything else passes — a
    letter, a word, punctuation. When the body carries no text at all
    (a proxy, an unexpected shape) the answer is *unknown*, reported sane:
    this flags garbage, it does not certify sense."""
    try:
        data = resp.json()
    except ValueError:
        return True, ""
    if not isinstance(data, dict):
        return True, ""
    if data.get("done", True) is not True:
        return False, "generation aborted (done=false)"
    text: object = None
    if provider == "ollama":
        text = data.get("response")
    else:
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = None
    if text is None:
        return True, ""
    stripped = str(text).strip()
    if not stripped:
        return False, "answered nothing"
    if set(stripped) <= _GARBAGE_CHARS:
        return False, f"answered {stripped[:8]!r} only"
    return True, ""


def measure_latency(
    endpoint: str, provider: str, model: str, *, warn_ms: float = 5000.0, timeout: float = 30.0
) -> Latency:
    """Time a minimal 1-token generation against ``model``. This is the ``--deep``
    check: a real round-trip (so it costs a call, hence opt-in), timing what
    residency can't — a resident model that is slow. A slow result means slow *now*
    (degraded node OR concurrent load), not a diagnosis; a timeout is itself the
    signal, so the caller uses a generous ``timeout`` and anything at/over
    ``warn_ms`` is flagged slow."""
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    try:
        start = time.monotonic()
        if provider == "ollama":
            resp = requests.post(
                f"{base}/api/generate",
                json={"model": model, "prompt": "ping", "stream": False, "options": {"num_predict": 1}},
                timeout=timeout,
            )
        else:
            resp = requests.post(
                f"{base}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=timeout,
                headers=_auth_headers(),
            )
        resp.raise_for_status()
        elapsed_ms = (time.monotonic() - start) * 1000.0
        slow = elapsed_ms >= warn_ms
        sane, why = _ping_sane(provider, resp)
        note = f"{elapsed_ms:.0f} ms" + (
            f" — slow (≥ {warn_ms:.0f} ms): node degraded or under load" if slow else ""
        ) + (f" — {why}: model may be degraded, reload it" if not sane else "")
        return Latency(ok=True, slow=slow, elapsed_ms=elapsed_ms, detail=note, sane=sane)
    except (requests.RequestException, ValueError) as exc:
        return Latency(ok=False, slow=True, elapsed_ms=None, detail=f"call failed ({type(exc).__name__})")


def measure_latency_autodetect(
    endpoint: str, model: str, *, warn_ms: float = 5000.0, timeout: float = 30.0
) -> Latency:
    """Latency for an endpoint whose provider is not declared (psoperator). Detect
    the node the same way :func:`probe_model_autodetect` does — ollama if
    ``/api/tags`` answers — then time it with the matching call."""
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    try:
        _json_object(requests.get(f"{base}/api/tags", timeout=timeout))
        provider = "ollama"
    except (requests.RequestException, ValueError):
        provider = "openai-compatible"
    return measure_latency(endpoint, provider, model, warn_ms=warn_ms, timeout=timeout)
