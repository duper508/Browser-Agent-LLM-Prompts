#!/usr/bin/env bash
set -e

echo "=== Browser-Agent LLM Prompts — Environment Setup ==="
echo

# 1. Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 is not installed."
    echo "Please install Python 3.10+ and re-run this script."
    echo "  Ubuntu/Debian:  sudo apt install python3 python3-venv python3-pip"
    echo "  macOS (brew):   brew install python@3.11"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1)
echo "[OK] $PYTHON_VERSION found"

# 2. Ensure pip and venv are available
PY_MINOR=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if ! command -v pip3 &>/dev/null; then
    echo "pip3 not found. Installing ..."
    sudo apt install -y python3-pip "python${PY_MINOR}-venv"
elif ! dpkg -s "python${PY_MINOR}-venv" &>/dev/null 2>&1; then
    echo "python3-venv not found. Installing ..."
    sudo apt install -y "python${PY_MINOR}-venv"
fi

# 3. Create / activate virtual environment
VENV_DIR="./venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    echo "[OK] Virtual environment created"
else
    echo "[OK] Virtual environment already exists at $VENV_DIR"
fi

# Activate
source "$VENV_DIR/bin/activate"
echo "[OK] Virtual environment activated"

# 4. Check for NVIDIA GPU / CUDA
echo
echo "--- GPU Check ---"
if command -v nvidia-smi &>/dev/null; then
    echo "[OK] nvidia-smi found"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null || true
else
    echo "[WARN] nvidia-smi not found. vLLM requires a CUDA-capable GPU."
    echo "       The model server will not start without GPU support."
fi

# 5. Install pip dependencies
echo
echo "--- Installing Python dependencies ---"
pip install --upgrade pip
pip install -r requirements.txt
echo "[OK] Python dependencies installed"

# 6. Install Playwright browsers
echo
echo "--- Installing Playwright browsers ---"
playwright install
echo "[OK] Playwright browsers installed"

echo
echo "=== Setup complete ==="
echo "Activate the environment with:  source ./venv/bin/activate"
echo "Then start the model with:      python start_model.py"
