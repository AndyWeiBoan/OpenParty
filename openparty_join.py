"""
openparty-join — Interactive setup wizard for remote OpenParty agents.

Detects installed engines (claude CLI, opencode), guides the user through
model selection, then connects to an OpenParty room as an AI agent.

Does NOT bundle claude or opencode — users must install them separately.

Usage:
    python openparty_join.py                        # interactive wizard
    python openparty_join.py --server ws://x:8765   # skip server prompt
"""

import asyncio
import json
import shutil
import subprocess
import sys

import aiohttp


# ── Constants ───────────────────────────────────────────────────────────────

OPENCODE_PORT = 4096
OPENCODE_URL = f"http://127.0.0.1:{OPENCODE_PORT}"
DEFAULT_SERVER = "ws://localhost:8765"

INSTALL_CLAUDE = "https://docs.anthropic.com/en/docs/claude-code"
INSTALL_OPENCODE = "https://opencode.ai"


# ── Detection helpers ────────────────────────────────────────────────────────

def _detect_claude() -> bool:
    """True if claude CLI (or claude_agent_sdk bundled binary) is available."""
    if shutil.which("claude"):
        return True
    try:
        import claude_agent_sdk, os
        bundled = os.path.join(
            os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude"
        )
        return os.path.isfile(bundled)
    except ImportError:
        return False


def _detect_opencode() -> bool:
    return shutil.which("opencode") is not None


async def _opencode_healthy(url: str = OPENCODE_URL) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/global/health",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as r:
                return r.status == 200
    except Exception:
        return False


async def _start_opencode_serve() -> bool:
    """Start opencode serve in background and wait up to 6 s."""
    print("  Starting opencode serve...", end="", flush=True)
    try:
        subprocess.Popen(
            ["opencode", "serve", "--port", str(OPENCODE_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f" failed ({e})")
        return False

    for _ in range(12):
        await asyncio.sleep(0.5)
        if await _opencode_healthy():
            print(" ✓")
            return True
    print(" timed out")
    return False


async def _fetch_opencode_models(url: str = OPENCODE_URL) -> list[dict]:
    """Return list of {display, model_id, provider} from connected providers."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/provider",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
    except Exception:
        return []

    connected = set(data.get("connected", []))
    result = []
    for provider in data.get("all", []):
        pid = provider.get("id", "")
        if pid not in connected:
            continue
        models = provider.get("models", {})
        items = models.values() if isinstance(models, dict) else models
        for m in items:
            mid = m.get("id", "")
            mname = m.get("name", mid)
            result.append({
                "display": f"{pid} / {mname}",
                "model_id": mid,
                "provider": pid,
            })
    return result


# ── UI helpers ───────────────────────────────────────────────────────────────

def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{text}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def pick(items: list[str], title: str = "Choose") -> int:
    """Show numbered menu, return 0-based index."""
    print(f"\n{title}:")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    while True:
        try:
            raw = input(f"  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(items):
                return idx
        print(f"  Please enter a number between 1 and {len(items)}")


# ── Main wizard ──────────────────────────────────────────────────────────────

async def wizard():
    print("=" * 50)
    print("  OpenParty — Join as AI Agent")
    print("=" * 50)
    print()

    # ── Connection details ───────────────────────────────────────────────────
    server = prompt("Server URL", DEFAULT_SERVER)
    room   = prompt("Room ID")
    if not room:
        print("Room ID cannot be empty.")
        sys.exit(1)
    name = prompt("Your display name")
    if not name:
        print("Display name cannot be empty.")
        sys.exit(1)

    # ── Engine detection ─────────────────────────────────────────────────────
    print("\nDetecting available engines...")
    has_claude   = _detect_claude()
    has_opencode = _detect_opencode()

    print(f"  {'✓' if has_claude   else '✗'} claude CLI")
    print(f"  {'✓' if has_opencode else '✗'} opencode")

    available_engines = []
    if has_claude:
        available_engines.append("claude")
    if has_opencode:
        available_engines.append("opencode")

    if not available_engines:
        print()
        print("No compatible engine found. Please install at least one:")
        print(f"  Claude CLI  → {INSTALL_CLAUDE}")
        print(f"  OpenCode    → {INSTALL_OPENCODE}")
        sys.exit(1)

    # ── Engine selection ─────────────────────────────────────────────────────
    engine_labels = {
        "claude":   "claude  (Claude Max / Pro subscription)",
        "opencode": "opencode  (multiple providers, some free)",
    }

    if len(available_engines) == 1:
        engine = available_engines[0]
        print(f"\nUsing engine: {engine_labels[engine]}")
    else:
        idx = pick([engine_labels[e] for e in available_engines], "Choose engine")
        engine = available_engines[idx]

    # ── Model selection (opencode only) ──────────────────────────────────────
    model_id = ""

    if engine == "opencode":
        print()
        if not await _opencode_healthy():
            ok = await _start_opencode_serve()
            if not ok:
                print("Could not start opencode serve. Make sure opencode is installed correctly.")
                sys.exit(1)

        print("  Fetching available models...", end="", flush=True)
        models = await _fetch_opencode_models()
        print(f" {len(models)} found")

        if not models:
            print("\nNo models available from connected providers.")
            print("Tip: run 'opencode' and log in to a provider first.")
            sys.exit(1)

        # Separate free vs authenticated models (free = provider "opencode")
        free    = [m for m in models if m["provider"] == "opencode"]
        paid    = [m for m in models if m["provider"] != "opencode"]

        sections = []
        if free:
            sections.append(("Free models (no API key required)", free))
        if paid:
            sections.append(("Provider models (require API key / login)", paid))

        flat_items  = []
        flat_models = []
        for section_title, section_models in sections:
            flat_items.append(f"── {section_title} ──")
            flat_models.append(None)          # separator, not selectable
            for m in section_models:
                flat_items.append(f"  {m['display']}")
                flat_models.append(m)

        # Pick loop — skip separators
        print()
        while True:
            idx = pick(flat_items, "Choose model")
            if flat_models[idx] is not None:
                chosen = flat_models[idx]
                model_id = chosen["model_id"]
                print(f"  Selected: {chosen['display']}")
                break
            print("  That's a section header — please pick a model number.")

    # ── Confirmation ─────────────────────────────────────────────────────────
    print()
    print("─" * 50)
    print(f"  Server : {server}")
    print(f"  Room   : {room}")
    print(f"  Name   : {name}")
    print(f"  Engine : {engine}")
    if model_id:
        print(f"  Model  : {model_id}")
    print("─" * 50)
    confirm = prompt("\nJoin room? (y/n)", "y").lower()
    if confirm not in ("y", "yes", ""):
        print("Cancelled.")
        sys.exit(0)

    # ── Launch bridge ─────────────────────────────────────────────────────────
    print(f"\nConnecting to {server} as '{name}'...")

    # Import and run AgentBridge directly (same process, no subprocess)
    try:
        from bridge import AgentBridge
    except ImportError:
        print("Error: bridge.py not found in the same directory.")
        sys.exit(1)

    bridge = AgentBridge(
        room_id=room,
        name=name,
        model=model_id if model_id else "claude-sonnet",
        server_url=server,
        max_turns=50,
        allowed_tools=[],
        engine=engine,
        opencode_url=OPENCODE_URL,
        opencode_model=model_id,
    )
    await bridge.run()


def main():
    try:
        asyncio.run(wizard())
    except KeyboardInterrupt:
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
