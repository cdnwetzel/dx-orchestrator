# dx-orchestrator

[![CI](https://github.com/cdnwetzel/dx-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/cdnwetzel/dx-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A sovereign, role-based orchestration layer for AI-driven software factories.

`dx` integrates:
- **pxx** — code generation engine (PyPI: `pxx-orchestrator>=2.5.4`)
- **PSOperator** — desktop automation and GUI verification
- **claude-sdlc-roles** — 38 governance role cards with separation-of-duties invariants
- **devswarm-ledger** — hash-chained Git ledger and GPG approval records
- **Your inference hardware** — whatever speaks vLLM or Ollama on your LAN

The design goal is `output = min(generation, verification)` — verification is the
binding constraint, and it stays with a named accountable human. Everything
mechanical (role parsing, gate enforcement, hardware routing) runs LLM-free.
**Every gate in `dx` is code, not a prompt.**

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

`TUTORIAL.md` is a longer, validated walkthrough from a clean box to a real
code-generation task.

## Commands

| Command | Description |
| --- | --- |
| `dx --version` | Print the installed version |
| `dx doctor` | Self-test: Python version, pxx, PSOperator, role cards, ledger, gpg, config, connectivity |
| `dx roles list` | List role cards (filter by `--fit`, `--seat`, `--anchored`, `--slug`; `--json`) |
| `dx roles validate` | Structural checks on role cards |
| `dx run` | Load role card → inject mandate → route to hardware → invoke pxx |
| `dx merge` | Pre-merge gate: RL-003 signature checks, optional GUI verify |
| `dx verify-gui` | Capture screen (SSH or PSOperator observer) → check with a VLM |

Exit codes: `0` success, `1` error or gate failure, `2` Anchored role refused.

## Configuration

`dx` reads routing config from `~/.config/dx/hardware_manifest.yml`. The seed
written by `setup_dependencies.sh` contains **placeholder hosts only** — edit it
before running real tasks. `dx doctor` reports each unreachable endpoint until
you do; that is the intended signal.

```yaml
roles:
  backend-engineer:
    endpoint: "http://vllm-host.example:8000"   # NO trailing /v1 — see below
    provider: "vllm"                            # ollama | vllm | openai
    model: "your-model-name"
  default:
    endpoint: "http://localhost:11434"
    provider: "ollama"

gui_verification:
  vlm_endpoint: "http://vlm-host.example:11434/api/generate"
  vlm_model: "qwen2.5vl:3b"
  ssh_host: "user@vlm-host.example"
```

**`endpoint` must not end in `/v1`.** `pxx` appends its own suffix per provider
(`/api/tags` for ollama, `/v1/models` for vllm and openai). A doubled `/v1`
returns 404 and every task fails with `MODEL_UNAVAILABLE`.

### Environment overrides

| Variable | Overrides |
| --- | --- |
| `DX_CONFIG` | Path to the hardware manifest |
| `DX_ROLES_PATH` | Role-card directory (also settable as `roles_path:` in the manifest) |
| `DX_LEDGER_REPO` | Path to the `devswarm-ledger` clone |
| `DX_VLM_ENDPOINT` / `DX_VLM_MODEL` | GUI verification model endpoint and name |
| `DX_GUI_SSH_HOST` | Host to capture screenshots from |
| `PSOPERATOR_REPO` / `PSOPERATOR_SNAPSHOT_DIR` | PSOperator clone and snapshot locations |

There are no hardcoded remote hosts anywhere in the package. An unconfigured
`gui_verification` section is an error, not a fallback to somebody else's box.

## Dependencies

- Python 3.11+
- `pxx-orchestrator>=2.5.4` (PyPI), `PyYAML`, `requests`
- `gpg` and `ssh` on the host
- **External clones** (handled by `setup_dependencies.sh`, two are private and
  need `gh auth login`):
  - `~/ai/psoperator` — https://github.com/cdnwetzel/psoperator
  - `~/ai/claude-sdlc-roles` — https://github.com/cdnwetzel/claude-sdlc-roles
  - `~/ai/devswarm-ledger` — https://github.com/cdnwetzel/devswarm-ledger

See `RESOURCES.md` for the full footprint and for fleet sizing requirements.

## Development

```bash
pip install -e ".[dev]"
pytest          # 146 tests
ruff check .
```

The test suite is hermetic — it runs against synthetic fixtures in
`tests/fixtures/` and never needs the private sibling repos, a GPG keyring, or
network access. CI runs ruff plus pytest on Python 3.11, 3.12 and 3.13, builds
the package, and checks that `LICENSE` ships inside the wheel.

If you add a gate, add a test for it. Three fail-open defects shipped in 0.2.0
precisely because the gates had none — see `CHANGELOG.md § 0.3.0 Security`.

## Documentation

| File | Contents |
| --- | --- |
| `TUTORIAL.md` | Validated walkthrough, clean box → real generated code |
| `VISION.md` | Seven-pillar architecture, red lines, lessons learned |
| `RESOURCES.md` | Footprint, host tooling, fleet sizing |
| `CHANGELOG.md` | Release history |
| `checkpoint.md` | Current state and next work |

## Status

- **Phase 1 (now)** — control plane on a low-power driver. All inference routed
  to lab hardware; no local model required.
- **Phase 2** — faster driver box, optional small local model for prototyping.
- **Phase 3** — Openterface Mini-KVM adds a framebuffer hash as a second GUI gate.
- **Phase 4** — team mode: a second GPG key in the ledger, Author ≠ Signer
  enforced at the Git level.

Deliberately not implemented yet: evidence-bundle generation in `dx run`, and
the actual `git merge` + ledger append in `dx merge`. Both are stubbed with the
design recorded in `VISION.md § Reference formats`.

## License

MIT — see [LICENSE](LICENSE).
