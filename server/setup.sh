#!/usr/bin/env bash
# ============================================================================
#  AfterWord server - one-time setup for Linux / macOS
#
#  Usage:   ./setup.sh
#
#  What it does:
#    1. Creates a Python virtual environment in ./.venv
#    2. Installs Python dependencies from requirements.txt
#    3. Checks for ffmpeg; offers to install it via the system package
#       manager if missing
#
#  After it finishes, run `./.venv/bin/python app.py` to start the server.
# ============================================================================

set -euo pipefail
cd "$(dirname "$0")"

echo
echo "=== AfterWord server - setup ==="
echo

# --- 1. Check Python ---
PY=""
for cand in python3.11 python3.12 python3.13 python3.14 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        version=$("$cand" -c "import sys; print(sys.version_info.major, sys.version_info.minor)")
        major=$(echo "$version" | awk '{print $1}')
        minor=$(echo "$version" | awk '{print $2}')
        if [ "$major" = "3" ] && [ "$minor" -ge 11 ]; then
            PY="$cand"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: Python 3.11 or newer not found on PATH."
    echo "Install it via your package manager (e.g. apt install python3.11"
    echo "on Ubuntu 22.04+, or use pyenv/uv for newer versions)."
    exit 1
fi
echo "Using $PY ($("$PY" --version))"

# --- 2. Create / reuse venv ---
if [ ! -d ".venv" ]; then
    echo
    echo "Creating virtual environment in .venv ..."
    "$PY" -m venv .venv
else
    echo
    echo "Reusing existing venv in .venv"
fi

# --- 3. Install dependencies ---
echo
echo "Installing Python dependencies ..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt

# --- 4. Check / install ffmpeg ---
echo
echo "Checking for ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -version | head -n 1
else
    echo "ffmpeg not found on PATH."
    if command -v apt-get >/dev/null 2>&1; then
        echo "Attempting install via apt-get (sudo) ..."
        sudo apt-get update && sudo apt-get install -y ffmpeg \
            || echo "WARNING: apt-get install failed. Install ffmpeg manually."
    elif command -v dnf >/dev/null 2>&1; then
        echo "Attempting install via dnf (sudo) ..."
        sudo dnf install -y ffmpeg \
            || echo "WARNING: dnf install failed. Install ffmpeg manually."
    elif command -v yum >/dev/null 2>&1; then
        echo "Attempting install via yum (sudo) ..."
        sudo yum install -y ffmpeg \
            || echo "WARNING: yum install failed. Install ffmpeg manually."
    elif command -v brew >/dev/null 2>&1; then
        echo "Attempting install via brew ..."
        brew install ffmpeg \
            || echo "WARNING: brew install failed. Install ffmpeg manually."
    else
        echo "WARNING: no supported package manager found (apt/dnf/yum/brew)."
        echo "Install ffmpeg manually and ensure it is on PATH, or set"
        echo "AFTERWORD_FFMPEG_PATH to the binary location."
    fi
fi

echo
echo "=== Setup complete ==="
echo
echo "To start the server:"
echo "    .venv/bin/python app.py"
echo "Then open http://localhost:8000 in a browser."
echo
