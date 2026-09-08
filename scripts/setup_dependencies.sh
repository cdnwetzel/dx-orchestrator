#!/usr/bin/env bash
# setup_dependencies.sh — idempotent installer for dx-orchestrator prerequisites.
#
# Installs:
#   1. pxx-orchestrator (from PyPI)
#   2. psoperator (git clone + pip install -e .)
#   3. sdlc-agent-roles (git clone)
#   4. devswarm-ledger-reference (git clone)
#   5. Default hardware_manifest.yml in ~/.config/dx/
#
# Every clone is a public repository — no GitHub credentials are needed.
#
# Safe to re-run — every step checks for prior state before acting.

set -euo pipefail

echo "🔧 Setting up dx-orchestrator dependencies..."

# Preflight: PEP 668 blocks `pip install` outside a venv on Ubuntu 24.04+.
# Require the caller to have a venv active (VIRTUAL_ENV set) so the pip
# calls below land somewhere writable.
if [ -z "${VIRTUAL_ENV:-}" ]; then
    echo "❌ No virtualenv active." >&2
    echo "   Create one and activate it before running this script:" >&2
    echo "     virtualenv -p python3 .venv    # or: python3 -m venv .venv" >&2
    echo "     source .venv/bin/activate" >&2
    echo "     ./scripts/setup_dependencies.sh" >&2
    exit 1
fi
echo "✅ virtualenv active: ${VIRTUAL_ENV}"

# --- 1. pxx ---
if command -v pxx >/dev/null 2>&1; then
    echo "✅ pxx already installed ($(pxx --version 2>&1 | head -n1))"
else
    echo "📦 Installing pxx-orchestrator from PyPI..."
    pip install --upgrade "pxx-orchestrator>=2.5.4"
fi

# --- 2. PSOperator ---
PSOP_DIR="${HOME}/ai/psoperator"
if [ -d "${PSOP_DIR}/.git" ]; then
    echo "✅ PSOperator already cloned at ${PSOP_DIR}"
else
    echo "📦 Cloning PSOperator..."
    mkdir -p "$(dirname "${PSOP_DIR}")"
    git clone https://github.com/cdnwetzel/psoperator.git "${PSOP_DIR}"
    (cd "${PSOP_DIR}" && pip install -e .)
fi

# --- 3. sdlc-agent-roles ---
ROLES_DIR="${HOME}/ai/sdlc-agent-roles"
if [ -d "${ROLES_DIR}/.git" ]; then
    echo "✅ sdlc-agent-roles already cloned at ${ROLES_DIR}"
else
    echo "📦 Cloning sdlc-agent-roles..."
    mkdir -p "$(dirname "${ROLES_DIR}")"
    git clone https://github.com/cdnwetzel/sdlc-agent-roles.git "${ROLES_DIR}"
fi

# --- 3b. devswarm-ledger-reference (needed by dx merge for RL-003 checks) ---
# The public reference ledger: synthetic rows, a real signature, every gate
# exercisable. Point DX_LEDGER_REPO at your own ledger to gate real merges.
LEDGER_DIR="${HOME}/ai/devswarm-ledger-reference"
if [ -d "${LEDGER_DIR}/.git" ]; then
    echo "✅ devswarm-ledger-reference already cloned at ${LEDGER_DIR}"
else
    echo "📦 Cloning devswarm-ledger-reference..."
    mkdir -p "$(dirname "${LEDGER_DIR}")"
    git clone https://github.com/cdnwetzel/devswarm-ledger-reference.git "${LEDGER_DIR}"
fi

# --- 4. Hardware manifest ---
CONFIG_DIR="${HOME}/.config/dx"
CONFIG_FILE="${CONFIG_DIR}/hardware_manifest.yml"
mkdir -p "${CONFIG_DIR}"

if [ -f "${CONFIG_FILE}" ]; then
    echo "✅ hardware_manifest.yml already exists at ${CONFIG_FILE}"
else
    echo "📝 Creating default hardware_manifest.yml..."
    cat > "${CONFIG_FILE}" <<'YAML'
# dx hardware routing manifest.
#
# ─────────────────────────────────────────────────────────────────────────────
# THESE ARE PLACEHOLDERS. Replace every host below with your own endpoints
# before running a real task. `dx doctor` will report each one unreachable
# until you do — that is the intended signal, not a bug.
# ─────────────────────────────────────────────────────────────────────────────
#
# `endpoint` MUST NOT include a trailing /v1. pxx appends the right suffix
# per `provider`:
#   ollama   -> {endpoint}/api/tags
#   vllm     -> {endpoint}/v1/models
#   openai   -> {endpoint}/v1/models
# If you double the /v1, probes 404 and every task fails with MODEL_UNAVAILABLE.
#
# Optional top-level key:
#   roles_path: "/path/to/sdlc-agent-roles/skills/sdlc-role/roles"
# Overridden in turn by the DX_ROLES_PATH environment variable.

roles:
  backend-engineer:
    endpoint: "http://vllm-host.example:8000"     # a vLLM box for bulk generation
    provider: "vllm"
    model: "REPLACE-WITH-YOUR-MODEL"
    description: "Bulk code generation"
  frontend-engineer:
    endpoint: "http://ollama-host.example:11434"  # a smaller/faster Ollama box
    provider: "ollama"
    model: "REPLACE-WITH-YOUR-MODEL"
    description: "Fast UI/GUI generation"
  security-architect:
    endpoint: "http://vllm-host.example:8000"     # heaviest reasoning node
    provider: "vllm"
    model: "REPLACE-WITH-YOUR-MODEL"
    description: "Heavy reasoning / certification"
  default:
    endpoint: "http://localhost:11434"
    provider: "ollama"
    description: "Local Ollama fallback"

gui_verification:
  vlm_endpoint: "http://vlm-host.example:11434/api/generate"
  vlm_model: "qwen2.5vl:3b"
  ssh_host: "user@vlm-host.example"
  screenshot_cmd: "import -window root -"          # ImageMagick

psoperator:
  observer_port: 8764
  gatekeeper_port: 8765
  executor_port: 8766
  model_endpoint: "http://ollama-host.example:11434"
  model_name: "REPLACE-WITH-YOUR-MODEL"
  audit_log_path: "psoperator_audit.jsonl"
YAML
    echo "⚠️  Edit ${CONFIG_FILE} — every host in it is a placeholder."
fi

echo ""
echo "✅ All dependencies installed."
echo ""
echo "Next:"
echo "  1. Edit ${CONFIG_FILE}"
echo "  2. Run: dx doctor"
echo "  3. Run: dx roles list"
