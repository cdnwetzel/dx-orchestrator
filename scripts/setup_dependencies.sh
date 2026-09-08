#!/usr/bin/env bash
# setup_dependencies.sh — idempotent installer for dx-orchestrator prerequisites.
#
# Installs:
#   1. pxx-orchestrator (from PyPI)
#   2. psoperator (git clone + pip install -e .)
#   3. claude-sdlc-roles (git clone)
#   4. Default hardware_manifest.yml in ~/.config/dx/
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

# Preflight: two of the three clones are private repos. If gh is installed,
# it must be logged in. If gh isn't installed, git clone will fall through
# to https and fail on the private ones — surface that clearly upfront.
if command -v gh >/dev/null 2>&1; then
    if ! gh auth status >/dev/null 2>&1; then
        echo "❌ gh is installed but not authenticated." >&2
        echo "   Run: gh auth login" >&2
        echo "   (needed to clone the private claude-sdlc-roles and devswarm-ledger repos)" >&2
        exit 1
    fi
    echo "✅ gh authenticated as $(gh api user --jq .login)"
else
    echo "⚠️  gh CLI not installed. Private-repo clones will fail." >&2
    echo "   Install: https://cli.github.com/  then run: gh auth login" >&2
    echo "   (public pxx install will still succeed)" >&2
fi

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

# --- 3. claude-sdlc-roles ---
ROLES_DIR="${HOME}/ai/claude-sdlc-roles"
if [ -d "${ROLES_DIR}/.git" ]; then
    echo "✅ claude-sdlc-roles already cloned at ${ROLES_DIR}"
else
    echo "📦 Cloning claude-sdlc-roles..."
    mkdir -p "$(dirname "${ROLES_DIR}")"
    # Private repo — use gh CLI (falls back to https if gh is not installed)
    if command -v gh >/dev/null 2>&1; then
        gh repo clone cdnwetzel/claude-sdlc-roles "${ROLES_DIR}"
    else
        git clone https://github.com/cdnwetzel/claude-sdlc-roles.git "${ROLES_DIR}"
    fi
fi

# --- 3b. devswarm-ledger (needed by dx merge for RL-003 signature checks) ---
LEDGER_DIR="${HOME}/ai/devswarm-ledger"
if [ -d "${LEDGER_DIR}/.git" ]; then
    echo "✅ devswarm-ledger already cloned at ${LEDGER_DIR}"
else
    echo "📦 Cloning devswarm-ledger..."
    mkdir -p "$(dirname "${LEDGER_DIR}")"
    if command -v gh >/dev/null 2>&1; then
        gh repo clone cdnwetzel/devswarm-ledger "${LEDGER_DIR}"
    else
        git clone https://github.com/cdnwetzel/devswarm-ledger.git "${LEDGER_DIR}"
    fi
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
# dx hardware routing manifest — edit to match your lab
roles:
  backend-engineer:
    endpoint: "http://t5810.lab:8004/v1"      # labrouter → T5810 (Qwen3.8-27B-FP8 on 2× A4500)
    description: "Bulk generation, ~33 tok/s"
  frontend-engineer:
    endpoint: "http://asrock.lab:11434/v1"     # asrock (RTX 5060 Ti)
    description: "Fast UI/GUI generation"
  security-architect:
    endpoint: "http://dgx.lab:8000/v1"      # DGX mesh
    description: "Heavy reasoning / certification"
  default:
    endpoint: "http://localhost:11434/v1"
    description: "Local Ollama fallback"

gui_verification:
  vlm_endpoint: "http://orin.lab:11434/api/generate"   # Orin Nano Qwen 2.5 VL 7B
  ssh_host: "operator@orin.lab"
  screenshot_cmd: "import -window root -"                # ImageMagick

psoperator:
  observer_port: 8764
  gatekeeper_port: 8765
  executor_port: 8766
  model_endpoint: "http://asrock.lab:11434/v1"
  model_name: "ui-tars-1.5-7b"
  audit_log_path: "psoperator_audit.jsonl"
YAML
    echo "⚠️  Edit ${CONFIG_FILE} to match your actual lab IPs."
fi

echo ""
echo "✅ All dependencies installed."
echo ""
echo "Next:"
echo "  1. Edit ${CONFIG_FILE}"
echo "  2. Run: dx doctor"
echo "  3. Run: dx roles list"
