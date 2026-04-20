#!/usr/bin/env bash
# setup.sh — Environment-aware installer for the RPI Agent.
# Detects hardware (ARM64 vs x86, NVIDIA GPU) and installs the correct
# PyTorch build before sentence-transformers to avoid bloated CUDA wheels on RPi.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()   { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()     { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header() { echo -e "\n${BOLD}$*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Phase 1: Hardware Detection ─────────────────────────────────────────────────
header "=== Phase 1: Hardware Detection ==="

ARCH=$(uname -m)
info "Architecture: ${ARCH}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 || ("$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 11) ]]; then
    error "Python 3.11+ required. Found: $PYTHON_VERSION"
    error "On Raspberry Pi: sudo apt install python3.11 python3.11-venv python3.11-dev"
    exit 1
fi
ok "Python ${PYTHON_VERSION}"

AVAILABLE_RAM_MB=$(free -m | awk '/^Mem:/ {print $7}')
info "Available RAM: ${AVAILABLE_RAM_MB} MB"
if [[ "$AVAILABLE_RAM_MB" -lt 512 ]]; then
    warn "Low available RAM (${AVAILABLE_RAM_MB} MB). Consider closing other processes before continuing."
fi

AVAILABLE_DISK_MB=$(df -m "${SCRIPT_DIR}" | awk 'NR==2 {print $4}')
info "Available disk: ${AVAILABLE_DISK_MB} MB"
if [[ "$AVAILABLE_DISK_MB" -lt 2048 ]]; then
    error "Insufficient disk space. Need at least 2048 MB, have ${AVAILABLE_DISK_MB} MB."
    exit 1
fi

HAS_GPU=false
CUDA_TAG=""
if [[ "$ARCH" == "x86_64" ]]; then
    if command -v nvidia-smi &>/dev/null; then
        CUDA_VERSION=$(nvidia-smi 2>/dev/null | grep -oP "CUDA Version: \K[0-9]+\.[0-9]+" || echo "")
        if [[ -n "$CUDA_VERSION" ]]; then
            HAS_GPU=true
            CUDA_MAJOR=$(echo "$CUDA_VERSION" | cut -d. -f1)
            CUDA_MINOR=$(echo "$CUDA_VERSION" | cut -d. -f2)
            CUDA_TAG="cu${CUDA_MAJOR}${CUDA_MINOR}"
            ok "NVIDIA GPU detected, CUDA ${CUDA_VERSION} (${CUDA_TAG})"
        else
            warn "nvidia-smi found but could not parse CUDA version — using CPU-only torch"
        fi
    else
        info "No NVIDIA GPU detected"
    fi
fi

if [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]]; then
    TORCH_STRATEGY="cpu-arm"
    TORCH_DESCRIPTION="CPU-only (ARM64 / Raspberry Pi)"
    TORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"
elif [[ "$HAS_GPU" == "true" ]]; then
    TORCH_STRATEGY="cuda"
    TORCH_DESCRIPTION="CUDA ${CUDA_VERSION} (${CUDA_TAG})"
    TORCH_INDEX_URL="https://download.pytorch.org/whl/${CUDA_TAG}"
else
    TORCH_STRATEGY="cpu-x86"
    TORCH_DESCRIPTION="CPU-only (x86_64)"
    TORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"
fi

header "=== Detection Summary ==="
echo -e "  Architecture:   ${BOLD}${ARCH}${NC}"
echo -e "  Python:         ${BOLD}${PYTHON_VERSION}${NC}"
echo -e "  Available RAM:  ${BOLD}${AVAILABLE_RAM_MB} MB${NC}"
echo -e "  Available disk: ${BOLD}${AVAILABLE_DISK_MB} MB${NC}"
echo -e "  PyTorch build:  ${BOLD}${TORCH_DESCRIPTION}${NC}"
echo ""

# ── Phase 2: Virtual Environment ───────────────────────────────────────────────
header "=== Phase 2: Virtual Environment ==="

VENV_DIR="${SCRIPT_DIR}/.venv"

if [[ -d "$VENV_DIR" ]]; then
    VENV_PYTHON="${VENV_DIR}/bin/python"
    if [[ -f "$VENV_PYTHON" ]]; then
        VENV_PY_VERSION=$("$VENV_PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        if [[ "$VENV_PY_VERSION" != "$PYTHON_VERSION" ]]; then
            warn "Existing venv uses Python ${VENV_PY_VERSION}, current is ${PYTHON_VERSION} — recreating..."
            rm -rf "$VENV_DIR"
        else
            ok "Using existing venv (Python ${VENV_PY_VERSION})"
        fi
    fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at .venv ..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

PIP="${VENV_DIR}/bin/pip"
PYTHON="${VENV_DIR}/bin/python"

info "Upgrading pip, setuptools, wheel..."
"$PIP" install --quiet --upgrade pip setuptools wheel

# ── Phase 3: PyTorch ────────────────────────────────────────────────────────────
header "=== Phase 3: PyTorch ==="

INSTALLED_TORCH=$("$PYTHON" -c "import torch; print(torch.__version__)" 2>/dev/null || echo "")
TORCH_NEEDS_INSTALL=true

if [[ -n "$INSTALLED_TORCH" ]]; then
    if [[ "$TORCH_STRATEGY" == "cpu-arm" && "$INSTALLED_TORCH" == *"+cu"* ]]; then
        warn "CUDA-enabled torch (${INSTALLED_TORCH}) found on ARM64 — wasting disk space."
        warn "Uninstalling and replacing with CPU-only version..."
        "$PIP" uninstall -y torch torchvision torchaudio 2>/dev/null || true
    elif [[ "$TORCH_STRATEGY" == "cuda" && "$INSTALLED_TORCH" != *"+${CUDA_TAG}"* ]]; then
        warn "torch ${INSTALLED_TORCH} found but expected ${CUDA_TAG} build — reinstalling..."
        "$PIP" uninstall -y torch torchvision torchaudio 2>/dev/null || true
    else
        ok "Correct torch already installed: ${INSTALLED_TORCH}"
        TORCH_NEEDS_INSTALL=false
    fi
fi

if [[ "$TORCH_NEEDS_INSTALL" == "true" ]]; then
    info "Installing PyTorch (${TORCH_DESCRIPTION})..."
    info "Index URL: ${TORCH_INDEX_URL}"
    info "This may take several minutes on first install..."
    "$PIP" install torch --index-url "$TORCH_INDEX_URL"

    INSTALLED_TORCH=$("$PYTHON" -c "import torch; print(torch.__version__)" 2>/dev/null || echo "FAILED")
    if [[ "$INSTALLED_TORCH" == "FAILED" ]]; then
        error "PyTorch installation failed. Check network connectivity and disk space."
        exit 1
    fi
    ok "PyTorch ${INSTALLED_TORCH} installed"
fi

# ── Phase 4: Package Groups ─────────────────────────────────────────────────────
header "=== Phase 4: Package Groups ==="

install_group() {
    local name="$1"
    local req_file="${SCRIPT_DIR}/requirements/${2}.txt"
    info "Installing ${name}..."
    "$PIP" install --quiet -r "$req_file"
    ok "${name} installed"
}

install_group "base (anthropic, httpx, psutil, croniter, pyyaml)" "base"
install_group "memory (mem0ai, chromadb, sentence-transformers)"  "memory"
install_group "voice (elevenlabs, groq)"                          "voice"
install_group "telegram (python-telegram-bot, watchdog)"          "telegram"
install_group "google (Calendar, Gmail)"                          "google"

# ── Phase 5: Verification ───────────────────────────────────────────────────────
header "=== Phase 5: Verification ==="

VERIFY_FAILED=false

verify_import() {
    local module="$1"
    local display="${2:-$1}"
    if "$PYTHON" -c "import ${module}" 2>/dev/null; then
        ok "${display}"
    else
        error "Failed to import: ${display}"
        VERIFY_FAILED=true
    fi
}

verify_import "anthropic"
verify_import "dotenv"             "python-dotenv"
verify_import "torch"
verify_import "sentence_transformers"
verify_import "mem0"               "mem0ai"
verify_import "chromadb"
verify_import "elevenlabs"
verify_import "groq"
verify_import "telegram"           "python-telegram-bot"
verify_import "googleapiclient"    "google-api-python-client"
verify_import "croniter"
verify_import "watchdog"
verify_import "yaml"               "pyyaml"
verify_import "psutil"
verify_import "httpx"

echo ""
TORCH_VER=$("$PYTHON" -c "import torch; print(torch.__version__)")
CUDA_AVAIL=$("$PYTHON" -c "import torch; print(torch.cuda.is_available())")
info "Torch version:  ${TORCH_VER}"
info "CUDA available: ${CUDA_AVAIL}"

if [[ "$VERIFY_FAILED" == "true" ]]; then
    echo ""
    error "Some packages failed to import. Check the errors above."
    exit 1
fi

# ── Phase 6: .env ───────────────────────────────────────────────────────────────
header "=== Phase 6: Configuration ==="

if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    if [[ -f "${SCRIPT_DIR}/.env.example" ]]; then
        cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
        warn ".env created from .env.example — edit it and fill in your API keys."
    else
        warn ".env not found. Copy .env.example to .env and fill in your API keys."
    fi
else
    ok ".env already exists"
fi

info "Ensuring runtime directories exist..."
mkdir -p "${SCRIPT_DIR}/data"
mkdir -p "${SCRIPT_DIR}/credentials"
ok "Runtime directories ready (data/, credentials/)"

# ── Done ────────────────────────────────────────────────────────────────────────
header "=== Setup Complete ==="
ok "All packages installed successfully."
echo ""
echo -e "Next steps:"
echo -e "  1. ${BOLD}source .venv/bin/activate${NC}"
echo -e "  2. Edit ${BOLD}.env${NC} with your API keys (if not done yet)"
echo -e "  3. ${BOLD}python main.py cli${NC}       — test in CLI mode"
echo -e "  4. ${BOLD}python main.py telegram${NC}  — run Telegram bot"
