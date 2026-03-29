# PyInstaller spec for openparty-agent (bridge.py)
# Includes the claude_agent_sdk bundled CLI binary.
#
# Build:
#   pyinstaller bridge.spec
#
# Output: dist/openparty-agent  (macOS/Linux) or dist/openparty-agent.exe (Windows)

import os
import sys

try:
    import claude_agent_sdk
    _sdk_dir = os.path.dirname(claude_agent_sdk.__file__)
    _bundled_dir = os.path.join(_sdk_dir, "_bundled")
    _claude_bin = os.path.join(_bundled_dir, "claude")
    if os.path.isfile(_claude_bin):
        binaries = [(_claude_bin, "claude_agent_sdk/_bundled")]
    else:
        print("[build] WARNING: claude_agent_sdk bundled binary not found — claude engine will not work in the output binary")
        binaries = []
except ImportError:
    print("[build] WARNING: claude_agent_sdk not installed — claude engine will not work in the output binary")
    binaries = []

a = Analysis(
    ["bridge.py"],
    pathex=[],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        "aiohttp",
        "aiohttp.connector",
        "aiohttp.client",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.client",
        "claude_agent_sdk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="openparty-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
