"""
OpenParty Observer CLI — read-only live view of a Room.

Usage:
    python observer_cli.py --room my-room --server ws://localhost:8765
"""

import asyncio
import json
import argparse
import sys
from datetime import datetime

import websockets


COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
}

AGENT_COLORS = ["\033[36m", "\033[33m", "\033[32m", "\033[35m", "\033[34m"]
agent_color_map: dict[str, str] = {}


def color_for(name: str) -> str:
    if name not in agent_color_map:
        idx = len(agent_color_map) % len(AGENT_COLORS)
        agent_color_map[name] = AGENT_COLORS[idx]
    return agent_color_map[name]


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def print_event(msg: dict):
    t = msg.get("type")
    c = COLORS

    if t == "joined":
        state = msg.get("room_state", {})
        participants = state.get("participants", [])
        print(f"\n{c['bold']}{'=' * 60}{c['reset']}")
        print(f"{c['bold']}  OpenParty Observer{c['reset']}")
        print(f"  Room:  {state.get('room_id', '?')}")
        print(f"  Topic: {state.get('topic', '?')[:70]}")
        if participants:
            print(f"  Agents: {', '.join(p['name'] for p in participants)}")
        else:
            print(f"  Agents: (waiting...)")
        print(f"{c['bold']}{'=' * 60}{c['reset']}\n")

        # Replay history if any
        history = msg.get("history", [])
        if history:
            print(f"{c['dim']}  [replaying {len(history)} messages]{c['reset']}")
            for entry in history:
                _print_message(entry)
            print()

    elif t == "agent_joined":
        col = color_for(msg["name"])
        print(
            f"{c['dim']}{now()}{c['reset']}  {col}++ {msg['name']} ({msg['model']}) joined{c['reset']}"
        )

    elif t == "agent_left":
        print(
            f"{c['dim']}{now()}{c['reset']}  {c['red']}-- {msg['name']} left  ({msg.get('agents_remaining', '?')} remaining){c['reset']}"
        )

    elif t == "turn_start":
        col = color_for(msg["name"])
        print(
            f"{c['dim']}{now()}{c['reset']}  {col}{c['bold']}» {msg['name']} is thinking...{c['reset']}",
            end="",
            flush=True,
        )

    elif t == "turn_end":
        latency = msg.get("latency_ms", 0)
        print(f"  {c['dim']}({latency}ms){c['reset']}")

    elif t == "message":
        _print_message(msg)

    elif t == "room_state":
        pass  # silently ignore periodic state updates

    else:
        # Unknown event — show dimly for debugging
        print(f"{c['dim']}{now()}  [{t}] {json.dumps(msg)[:80]}{c['reset']}")


def _print_message(entry: dict):
    c = COLORS
    name = entry.get("name", "?")
    content = entry.get("content", "")
    col = color_for(name)
    print(f"\n  {col}{c['bold']}{name}{c['reset']}")
    # Word-wrap at 70 chars
    for line in content.split("\n"):
        print(f"    {line}")


async def observe(room_id: str, server_url: str, name: str = "Observer"):
    print(f"Connecting to {server_url} ...")

    try:
        async with websockets.connect(server_url) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "join",
                        "role": "observer",
                        "room_id": room_id,
                        "name": name,
                    }
                )
            )

            async for raw in ws:
                msg = json.loads(raw)
                print_event(msg)

    except websockets.exceptions.ConnectionClosed:
        print("\n[Observer] Connection closed.")
    except ConnectionRefusedError:
        print(f"[Observer] Cannot connect to {server_url}. Is the server running?")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[Observer] Exiting.")


def main():
    parser = argparse.ArgumentParser(
        description="OpenParty Observer — watch a Room live"
    )
    parser.add_argument("--room", default="debate-001", help="Room ID to observe")
    parser.add_argument("--server", default="ws://localhost:8765", help="Server URL")
    parser.add_argument("--name", default="Observer", help="Observer display name")
    args = parser.parse_args()

    asyncio.run(observe(args.room, args.server, args.name))


if __name__ == "__main__":
    main()
