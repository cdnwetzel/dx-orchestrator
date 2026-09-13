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
    exactly the state that read green and then failed."""

    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in _OK


def classify_ollama(model: str, *, tags: list[str], resident: list[str]) -> ModelAvailability:
    """Classify an Ollama-backed model from its ``/api/tags`` (on disk) and
    ``/api/ps`` (resident) name lists."""
    if model in resident:
        return ModelAvailability(RESIDENT, f"{model} is resident")
    if model in tags:
        return ModelAvailability(
            ON_DISK_COLD, f"{model} is on disk but not resident — it will cold-load (may stall)"
        )
    return ModelAvailability(NOT_SERVED, f"{model} is not in /api/tags — not pulled on this node")


def classify_openai(model: str, *, served: list[str]) -> ModelAvailability:
    """Classify an OpenAI-compatible/vLLM model from its ``/v1/models`` list.
    Residency is opaque here, so a listed model is 'served', not 'resident'."""
    if model in served:
        return ModelAvailability(SERVED, f"{model} is served (residency opaque via /v1/models)")
    return ModelAvailability(NOT_SERVED, f"{model} is not in /v1/models")


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
