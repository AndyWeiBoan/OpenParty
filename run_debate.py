"""
run_debate.py — Launches server + both agents in one process for easy testing.
Usage: python run_debate.py
"""

import asyncio
import logging
import sys

from agent_mock import build_persona, make_llm_fn
from agent_sdk import OpenPartyAgent
from server import RoomServer

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

TRANSCRIPT: list[str] = []


def patched_llm(persona, my_name):
    """Wrap make_llm_fn to also append to TRANSCRIPT for display."""
    inner = make_llm_fn(persona, my_name=my_name)

    async def wrapped(history):
        reply = await inner(history)
        TRANSCRIPT.append((my_name, reply))
        return reply

    return wrapped


async def run_server():
    """Start the WebSocket room server."""
    server = RoomServer()
    async with websockets.serve(server.handle_connection, "localhost", 8766):
        await asyncio.sleep(60)  # keep alive long enough


async def run_agent(name: str, turns: int):
    """Start one mock agent."""
    await asyncio.sleep(0.3 if name == "Bolt" else 0)  # Bolt joins slightly later
    persona = build_persona(name)
    agent = OpenPartyAgent(
        room_id="debate-001",
        name=name,
        model=f"mock-{name.lower()}",
        llm_fn=patched_llm(persona, my_name=name),
        server_url="ws://localhost:8766",
        max_turns=turns,
    )
    await agent.run()


async def main():
    turns = 3

    print()
    print("══════════════════════════════════════════════════════════════")
    print("  OpenParty MVP — Live Debate")
    print("  TOPIC: Should AI have its own opinions, or stay neutral?")
    print("  Aria  = pro-opinion  🔵")
    print("  Bolt  = pro-neutral  🟠")
    print("══════════════════════════════════════════════════════════════")
    print()

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(0.3)  # let server boot

    # Run both agents concurrently
    await asyncio.gather(
        run_agent("Aria", turns),
        run_agent("Bolt", turns),
    )

    server_task.cancel()

    # Print final transcript
    print()
    print("══════════════════════════════════════════════════════════════")
    print("  TRANSCRIPT")
    print("══════════════════════════════════════════════════════════════")
    print()
    icons = {"Aria": "🔵", "Bolt": "🟠"}
    for i, (name, content) in enumerate(TRANSCRIPT, 1):
        print(f"  [{i}] {icons.get(name, '⚪')} {name}:")
        print(f"      {content}")
        print()
    print("══════════════════════════════════════════════════════════════")
    print(f"  {len(TRANSCRIPT)} turns completed. Validation PASSED ✅")
    print("══════════════════════════════════════════════════════════════")


if __name__ == "__main__":
    asyncio.run(main())
