#!/usr/bin/env bash
# Build openparty-join standalone binary using PyInstaller.
# Output: dist/openparty-join  (macOS/Linux) or dist/openparty-join.exe (Windows)
#
# Requirements:
#   - Python 3.11+
#   - pip install pyinstaller aiohttp websockets claude-agent-sdk
#
# Usage:
#   bash build.sh

set -e

# Auto-detect venv python, fall back to system python3
if [ -f ".venv/bin/python" ]; then
    PYTHON=${PYTHON:-.venv/bin/python}
elif [ -f "venv/bin/python" ]; then
    PYTHON=${PYTHON:-venv/bin/python}
else
    PYTHON=${PYTHON:-python3}
fi

BINARY_NAME="openparty-join"
echo "Using Python: $PYTHON ($($PYTHON --version))"

echo "=== OpenParty Agent Builder ==="
echo

# ── Check dependencies ────────────────────────────────────────────────────────
echo "Checking dependencies..."
$PYTHON -c "import aiohttp"    || { echo "Missing: pip install aiohttp"; exit 1; }
$PYTHON -c "import websockets" || { echo "Missing: pip install websockets"; exit 1; }
$PYTHON -m pip show pyinstaller &>/dev/null || {
    echo "Installing PyInstaller..."
    $PYTHON -m pip install pyinstaller
}

# ── Collect extra data ────────────────────────────────────────────────────────
ADD_DATA=""

# claude_agent_sdk: Python package only (no bundled claude binary)
# The binary must be installed separately by the end user.
# We only bundle the Python SDK so the import works.
$PYTHON -c "import claude_agent_sdk" 2>/dev/null && HAS_SDK=1 || HAS_SDK=0

# ── Build ─────────────────────────────────────────────────────────────────────
echo "Building $BINARY_NAME..."

HIDDEN=""
if [ "$HAS_SDK" = "1" ]; then
    HIDDEN="--hidden-import=claude_agent_sdk"
fi

$PYTHON -m PyInstaller \
    --onefile \
    --name "$BINARY_NAME" \
    --hidden-import=aiohttp \
    --hidden-import=aiohttp.connector \
    --hidden-import=aiohttp.client \
    --hidden-import=websockets \
    --hidden-import=websockets.legacy \
    --hidden-import=websockets.legacy.client \
    $HIDDEN \
    openparty_join.py

chmod +x "dist/$BINARY_NAME"

echo
echo "=== Build complete ==="
echo "  Binary : dist/$BINARY_NAME"
echo
echo "Distribute this binary to remote users."
echo "They still need to install claude CLI or opencode separately:"
echo "  Claude   → https://docs.anthropic.com/en/docs/claude-code"
echo "  OpenCode → https://opencode.ai"
echo
echo "Usage on remote machine:"
echo "  ./openparty-join"
echo "  # Follow the interactive prompts"
