# dx-orchestrator

A sovereign, role-based orchestration layer for AI-driven software factories.

`dx` integrates:
- **pxx** — code generation engine (PyPI: `pxx-orchestrator>=2.5.4`)
- **PSOperator** — desktop automation and GUI verification
- **claude-sdlc-roles** — 38 governance role cards with separation-of-duties invariants
- **Your inference hardware** — T5810 / DGX mesh / asrock / Orin Nano / Mac Studio

The design goal is `output = min(generation, verification)` — verification is the binding constraint, and it stays with a named accountable human. Everything mechanical (role parsing, gate enforcement, hardware routing) runs LLM-free.

## Quick start

Ubuntu 24.04 and other PEP 668 systems refuse `pip install` outside a venv, so:

```bash
# create + activate a venv (uses `virtualenv` if `python3-venv` is not apt-installed)
python3 -m venv .venv || virtualenv -p python3 .venv
source .venv/bin/activate

pip install -e .
./scripts/setup_dependencies.sh
dx doctor
dx roles list
dx run T-001 --required_role backend-engineer --message "Hello" --dry-run
```

Without activation, use `.venv/bin/dx` explicitly. `dx doctor` and `dx run` both
resolve `pxx` by looking alongside `sys.executable` first, so the venv works
whether or not it is activated in the current shell.

## Commands

| Command | Description |
| --- | --- |
| `dx doctor` | Self-test: Python version, pxx, PSOperator, role cards, config, connectivity |
| `dx roles list` | List all 38 role cards (filter by `--fit` or `--slug`) |
| `dx roles validate` | Structural checks on role cards |
| `dx run` | Load role card → inject mandate → route to hardware → invoke pxx |
| `dx merge` | Pre-merge gate: GPG signer ≠ author for Partial/Anchored roles, optional GUI verify |
| `dx verify-gui` | Capture screen (SSH or PSOperator observer) → verify with Qwen 2.5 VL |

## Dependencies

- Python 3.11+
- `pxx-orchestrator>=2.5.4` (PyPI)
- `PyYAML`, `requests`
- **External clones** (handled by `setup_dependencies.sh`):
  - `~/ai/psoperator` — from https://github.com/cdnwetzel/psoperator
  - `~/ai/claude-sdlc-roles` — from https://github.com/cdnwetzel/claude-sdlc-roles

## Hardware manifest

`dx` reads routing config from `~/.config/dx/hardware_manifest.yml` (override with `DX_CONFIG`).

The default manifest points to:
- `backend-engineer` → T5810 labrouter (Qwen3.8-27B-FP8, ~33 tok/s)
- `frontend-engineer` → asrock (Qwen2.5-14B on RTX 5060 Ti)
- `security-architect` → DGX mesh (DeepSeek V4)
- GUI verification → Orin Nano (Qwen 2.5 VL 7B) via SSH screenshot

Edit the manifest to match your lab IPs before running real tasks.

## Status

- Phase 1: control plane on a low-power driver (2011 Mac Mini or Surface Pro 6 with WSL2). All inference is routed to lab hardware — no local model required.
- Phase 2: move to XPS 9510 for faster CLI response and optional local RTX 3050 Ti inference.

See `VISION.md` for the seven-pillar architecture (compute fabric → execution hands → governance → SDLC process → conversational commander → tactical control → orchestration kernel) and `checkpoint.md` for current state.

## License

MIT
