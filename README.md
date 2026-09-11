# dx-orchestrator

[![CI](https://github.com/cdnwetzel/dx-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/cdnwetzel/dx-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A sovereign, role-based orchestration layer for AI-driven software factories.

`dx` integrates:
- **pxx** — code generation engine (PyPI: `pxx-orchestrator>=2.5.4`)
- **PSOperator** — desktop automation and GUI verification
- **sdlc-agent-roles** — 40 governance role cards with separation-of-duties invariants
- **devswarm-ledger-reference** — hash-chained ledger format and GPG approval records
- **Your inference hardware** — anything OpenAI-compatible, or Ollama, on your LAN or your own box

The design goal is `output = min(generation, verification)` — verification is the
binding constraint, and it stays with a named accountable human. Everything
mechanical (role parsing, gate enforcement, hardware routing) runs LLM-free.
**Every gate in `dx` is code, not a prompt.**

## Reproducing this

Every dependency is public and MIT. Five repositories, one of which you install
from PyPI rather than clone:

| Repo | What it provides | How you get it |
| --- | --- | --- |
| this one | control plane, all gates | `git clone` |
| [`pxx`](https://github.com/cdnwetzel/pxx) | code-generation engine | PyPI: `pxx-orchestrator>=2.5.4` |
| [`psoperator`](https://github.com/cdnwetzel/psoperator) | desktop automation, GUI verification | `git clone` |
| [`sdlc-agent-roles`](https://github.com/cdnwetzel/sdlc-agent-roles) | the 40 role cards `dx` is governed by | `git clone` |
| [`devswarm-ledger-reference`](https://github.com/cdnwetzel/devswarm-ledger-reference) | ledger format, verifier, a signed reference trace | `git clone` |

`scripts/setup_dependencies.sh` fetches all of them. No GitHub credentials are
needed.

**The reference ledger is synthetic on purpose.** It carries three chain-verified
rows and one real GPG signature so `dx merge` can be exercised end-to-end — and
so every way the gate *fails* can be reproduced. It attests to no real work.
Point `DX_LEDGER_REPO` at your own ledger to gate real merges.

The test suite is the part built to be evaluated from outside: 506 tests,
including real-GPG signature checks against committed keys, all runnable with no
lab hardware, no keyring, no network and none of the sibling clones. If you are
here to assess whether the gates hold, `pytest` is the honest surface.

The role-card *format* is documented and the parser is exercised against
synthetic cards in `tests/fixtures/roles/`, so `DX_ROLES_PATH` will happily point
at your own deck.

## Quick start

Ubuntu 24.04 and other PEP 668 systems refuse `pip install` outside a venv, so:

```bash
# create + activate a venv (uses `virtualenv` if `python3-venv` is not apt-installed)
python3 -m venv .venv || virtualenv -p python3 .venv
source .venv/bin/activate

pip install -e .
./scripts/setup_dependencies.sh
$EDITOR ~/.config/dx/hardware_manifest.yml   # every host in the seed is a placeholder
dx doctor
dx roles list
dx run T-001 --required_role backend-engineer --message "Hello" --dry-run
```

Without activation, use `.venv/bin/dx` explicitly. `dx doctor` and `dx run` both
resolve `pxx` by looking alongside `sys.executable` first, so the venv works
whether or not it is activated in the current shell.

For day-to-day coding, `USAGE.md` shows the simple loop: fill in
`templates/spec.md`, hand it to `dx run`, and get a consistent result. A filled
example is in `templates/spec.example.md`. `TUTORIAL.md` is a longer, validated
walkthrough from a clean box to a real code-generation task.

## Commands

| Command | Description |
| --- | --- |
| `dx --version` | Print the installed version |
| `dx doctor` | Self-test: Python version, pxx, PSOperator, role cards, ledger, gpg, config, connectivity |
| `dx roles list` | List role cards (filter by `--fit`, `--seat`, `--anchored`, `--slug`; `--json`) |
| `dx roles validate` | Structural checks on role cards |
| `dx run` | Load role card → inject mandate → route to hardware → invoke pxx |
| `dx merge` | Merge gate: RL-003 checks, then `git merge --no-ff` (`--repo`) and `SIGNED`/`MERGED` ledger rows → writes a `dx.merge_gate.v1` bundle |
| `dx verify-gui` | Capture screen (SSH or PSOperator observer) → check with a VLM → write a `dx.gui_verification.v1` bundle |

Exit codes: `0` success, `1` error or gate failure, `2` Anchored role refused, `3` the task itself failed.

`2` is reserved for dx's own governance decision and is never produced by a tool dx shells out to — pxx exits 2 in the wild, so its code is printed rather than returned. If you read `2`, policy refused the run; if you read `3`, the run was allowed and the work failed.

## Evidence

Every `dx run` writes a `dx.role_task.v1` evidence bundle — a directory, not a
file:

```
<task_id>/<utc-timestamp>/
├── README.md        what happened, for a human
├── manifest.json    the same, for a machine
├── SHA256SUMS       tamper-evidence
└── artifacts/       prompt, routed endpoint/model/provider, the diff, git status
```

Verify one anywhere, with no Python and no network:

```bash
cd <bundle> && sha256sum -c SHA256SUMS
```

Bundles land in `~/.local/state/dx/evidence` by default — deliberately *outside*
the repository under edit, so receipts never end up in the tree pxx is
committing. Override with `--evidence-dir` or `DX_EVIDENCE_DIR`. `--no-evidence`
skips emission entirely.

**Failed runs get bundles too.** A store that only records successes is a
highlight reel. And a run whose receipt cannot be written fails closed: the task
may have succeeded, but a receipted run that produced no receipt is not one.

Every bundle carries a mandatory `boundary` block stating what it does **not**
prove — that the code is correct, that anyone reviewed it, that any test of the
generated behaviour was run. A bundle without one is a claim wearing a receipt's
clothing, so the writer refuses to emit it.

`dx verify-gui` writes its own family, `dx.gui_verification.v1`: the screenshot
it judged stored verbatim under `artifacts/`, the model's YES/NO answer, and a
`boundary` block whose first line is that the answer is advisory evidence, never
a proof (RL-007 — `dx merge` still requires a GPG signature). Same
`--evidence-dir` / `DX_EVIDENCE_DIR` / `--no-evidence` controls; `--task` sets
the id it is filed under (default `verify-gui`). Add `--observer` and the bundle also carries a **verified** PSOperator observer attestation bound to the exact frame by hash (signature and freshness checked, `PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH` required); it fails closed if that provenance cannot be obtained.

`dx.staged_action.v1` is the fourth family, for the desktop staging layer: a proposed multi-step action bound to a perceived world-state, with a bundle-level risk class and the named human seat it routes to — a draft for a human to approve, never an act. See `docs/` and the `workflow-stager` / `workflow-operator` role cards.

`dx merge` writes a `dx.merge_gate.v1` bundle too: the RL-003 decision and its facts — the ledger head the signature was checked against, who signed, whether duties were separated, any advisory GUI check, and the merge commit when `--repo` is given. A refused merge gets a bundle as well, with the failure reason. The path is announced on stderr so `dx merge`'s stdout stays the pinned RL-003 transcript.

## Configuration

`dx` reads routing config from `~/.config/dx/hardware_manifest.yml`. The seed
written by `setup_dependencies.sh` contains **placeholder hosts only** — edit it
before running real tasks. `dx doctor` reports each unreachable endpoint until
you do; that is the intended signal.

```yaml
roles:
  backend-engineer:
    endpoint: "http://vllm-host.example:8000"   # NO trailing /v1 — see below
    provider: "vllm"                            # see the provider table below
    model: "your-model-name"
  default:
    endpoint: "http://localhost:11434"
    provider: "ollama"

gui_verification:
  vlm_endpoint: "http://vlm-host.example:11434/api/generate"
  vlm_model: "qwen2.5vl:3b"
  ssh_host: "user@vlm-host.example"
```

### Providers — any OpenAI-compatible stack works

| `provider` | Use it for |
| --- | --- |
| `ollama` | Ollama |
| `vllm` | vLLM |
| `openai` | OpenAI, or an endpoint that mimics it exactly |
| `openai-compatible` | **anything else OpenAI-shaped** — llama.cpp server, LM Studio, TGI, LiteLLM, a router or proxy, a hosted API |

`dx` does not validate this field; it passes it to `pxx`, which treats an
unrecognised value as `openai-compatible` rather than failing. So wiring `dx` to
your own inference stack is a manifest edit, not a code change. Routing is
per-role, so different roles can sit on different backends.

Endpoints needing a key: export `PXX_API_KEY` — `dx` passes the environment
through to `pxx` untouched.

**A trailing `/v1` is stripped automatically.** `pxx` appends its own suffix per
provider (`/api/tags` for ollama, `/v1/models` for everything OpenAI-shaped), so
a base URL ending in `/v1` would be probed as `/v1/v1/models`, return 404, and
fail every task with `MODEL_UNAVAILABLE`. This matters most for hosted services,
which publish their base URL *with* the `/v1` — pasting the vendor's own string
is the common case, not a mistake. `dx` corrects it at run time and says so:

```
ℹ️  endpoint https://api.example/v1 → https://api.example (pxx appends its own /v1 …)
```

The correction is never silent, and `dx doctor` still flags the manifest so the
file ends up saying what actually runs.

### Environment overrides

| Variable | Overrides |
| --- | --- |
| `DX_CONFIG` | Path to the hardware manifest |
| `DX_ROLES_PATH` | Role-card directory (also settable as `roles_path:` in the manifest) |
| `DX_EVIDENCE_DIR` | Where evidence bundles are written (default `~/.local/state/dx/evidence`) |
| `DX_LEDGER_REPO` | Path to a ledger repo — set this to your own operational ledger; the default is the public reference one |
| `DX_VLM_ENDPOINT` / `DX_VLM_MODEL` | GUI verification model endpoint and name |
| `DX_VLM_TIMEOUT` | Seconds to wait on the VLM (default 30; manifest: `gui_verification.timeout_s`) |
| `DX_GUI_SSH_HOST` | Host to capture screenshots from |
| `PSOPERATOR_REPO` / `PSOPERATOR_SNAPSHOT_DIR` | PSOperator clone and snapshot locations |
| `PSOPERATOR_OBSERVER_PORT` / `PSOPERATOR_GATEKEEPER_PORT` / `PSOPERATOR_EXECUTOR_PORT` | PSOperator service ports (manifest: `psoperator.*_port`) |
| `PSOPERATOR_OBSERVER_HOST` | Observer host for `dx verify-gui --observer` (default `127.0.0.1`) |
| `PSOPERATOR_OBSERVER_ATTESTATION_KEY_PATH` | Owner-only observer attestation key, required by `dx verify-gui --observer` |
| `PSOPERATOR_MODEL_ENDPOINT` / `PSOPERATOR_MODEL_NAME` | Model the GUI agent drives |
| `PSOPERATOR_AUDIT_LOG_PATH` | Where PSOperator writes its audit log |

There are no hardcoded remote hosts anywhere in the package. An unconfigured
`gui_verification` section is an error, not a fallback to somebody else's box.

## Dependencies

- Python 3.11+
- `pxx-orchestrator>=2.5.4` (PyPI), `PyYAML`, `requests`
- `gpg` and `ssh` on the host
- **External clones** (all public; handled by `setup_dependencies.sh`):
  - `~/ai/psoperator` — https://github.com/cdnwetzel/psoperator
  - `~/ai/sdlc-agent-roles` — https://github.com/cdnwetzel/sdlc-agent-roles
  - `~/ai/devswarm-ledger-reference` — https://github.com/cdnwetzel/devswarm-ledger-reference

See `RESOURCES.md` for the full footprint and for fleet sizing requirements.

## Development

```bash
pip install -e ".[dev]"
pytest                     # the full suite
ruff check .              # lint + import order
mypy src/dx --strict      # no untyped defs, no implicit Any
pytest --cov=dx           # CI floor is 88%
```

The test suite is hermetic — it runs against synthetic fixtures in
`tests/fixtures/` and never needs the sibling clones, a GPG keyring, or
network access. CI runs ruff plus pytest on Python 3.11, 3.12 and 3.13, builds
the package, and checks that `LICENSE` ships inside the wheel.

If you add a gate, add tests for the ways it can **wrongly pass** — not only
the ways it correctly fails. Three fail-open defects shipped in 0.2.0 precisely
because the gates had none, and all three looked fine on the happy path. See
`CONTRIBUTING.md` and `CHANGELOG.md § 0.3.0 Security`.

`tests/test_gpg_integration.py` runs the RL-003 signature gate against committed
real GPG keys — usable, revoked and expired — and `tests/test_docs_consistency.py`
fails the build if this README drifts from the implementation.

## Documentation

| File | Contents |
| --- | --- |
| `USAGE.md` | Spec-driven coding: fill a template, run it, get a consistent result |
| `templates/spec.md` | The reusable spec template (a filled example in `templates/spec.example.md`) |
| `TUTORIAL.md` | Validated walkthrough, clean box → real generated code |
| `VISION.md` | Seven-pillar architecture, red lines, lessons learned |
| `RESOURCES.md` | Footprint, host tooling, fleet sizing |
| `CHANGELOG.md` | Release history |
| `SECURITY.md` | Trust boundaries, disclosure route, what dx does *not* protect |
| `CONTRIBUTING.md` | Dev setup, the gates, fixture rules |
| `ROADMAP.md` | What closing the gap to a full end-to-end run actually takes |
| `checkpoint.md` | Current state and next work |
| `RELEASE_READINESS.md` | Cross-repo open-source readiness: the five repos, what each scan found, cost to publish |

## Status

- **Phase 1 (now)** — control plane on a low-power driver. All inference routed
  to lab hardware; no local model required.
- **Phase 2** — faster driver box, optional small local model for prototyping.
- **Phase 3** — Openterface Mini-KVM adds a framebuffer hash as a second GUI gate.
- **Phase 4** — team mode: a second GPG key in the ledger, Author ≠ Signer
  enforced at the Git level.

Both pipeline stubs are now closed: `dx run` writes evidence bundles (0.9.0) and
`dx merge` performs the `git merge --no-ff` and appends `SIGNED`/`MERGED` rows
(0.10.0). What remains open is stated in `ROADMAP.md`: the RL-010 ceremony —
a signature made interactively over a ledger whose rows attest to real work —
and `dx verify-gui` against a live desktop.

## License

MIT — see [LICENSE](LICENSE).
