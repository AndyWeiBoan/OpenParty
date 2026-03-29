"""
run_real_debate.py
===================
Launches server + 3 real LLM agents in one process:
  - Llama 3.3 70B  (Meta, via Groq)
  - Kimi K2        (Moonshot AI, via Groq)
  - Gemini 2.0     (Google, via AI Studio)

Usage:
    export GROQ_API_KEY="..."
    export GEMINI_API_KEY="..."
    python run_real_debate.py
"""

import asyncio
import logging
import os
import sys

import websockets

from server import RoomServer
from agent_sdk import OpenPartyAgent
from agent_groq_llama import llama_llm
from agent_groq_kimi import kimi_llm
from agent_gemini import gemini_llm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

ROOM_ID = "real-debate-001"
SERVER_URL = "ws://localhost:8767"
TOPIC = "Today's topic: Will AI agents eventually replace human decision-making in most professional fields? Share your view."
TURNS = 3

TRANSCRIPT: list[tuple[str, str]] = []


def wrap_llm(name: str, fn):
    async def wrapped(history):
        reply = await fn(history)
        TRANSCRIPT.append((name, reply))
        return reply

    return wrapped


async def run_server():
    server = RoomServer()
    server.rooms  # init

    # Override topic for this debate
    async def _handle(ws):
        room_id_peek = ROOM_ID
        if room_id_peek not in server.rooms:
            room = server.get_or_create_room(room_id_peek)
            room.topic = TOPIC
        await server.handle_connection(ws)

    async with websockets.serve(_handle, "localhost", 8767):
        await asyncio.sleep(120)


async def run_agent(name: str, model: str, llm_fn, delay: float = 0):
    await asyncio.sleep(delay)
    agent = OpenPartyAgent(
        room_id=ROOM_ID,
        name=name,
        model=model,
        llm_fn=wrap_llm(name, llm_fn),
        server_url=SERVER_URL,
        max_turns=TURNS,
    )
    await agent.run()


async def main():
    # Check keys
    missing = []
    if not os.environ.get("GROQ_API_KEY"):
        missing.append("GROQ_API_KEY")
    if not os.environ.get("GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")
    if missing:
        print(f"❌ Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    print()
    print("══════════════════════════════════════════════════════════════════")
    print("  OpenParty — Real LLM Debate")
    print(f"  TOPIC: {TOPIC}")
    print()
    print("  🦙 Llama 3.3 70B  (Meta, via Groq)")
    print("  🌙 Kimi K2        (Moonshot AI, via Groq)")
    print("  ✨ Gemma 3 27B    (Google open model, via AI Studio)")
    print("══════════════════════════════════════════════════════════════════")
    print()

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(0.5)  # let server boot

    await asyncio.gather(
        run_agent("Llama-3.3", "llama-3.3-70b-versatile", llama_llm, delay=0),
        run_agent("Kimi-K2", "moonshotai/kimi-k2-instruct", kimi_llm, delay=0.4),
        run_agent("Gemma-27B", "gemma-3-27b-it", gemini_llm, delay=0.8),
    )

    server_task.cancel()

    # Print transcript
    print()
    print("══════════════════════════════════════════════════════════════════")
    print("  TRANSCRIPT")
    print("══════════════════════════════════════════════════════════════════")
    print()
    icons = {"Llama-3.3": "🦙", "Kimi-K2": "🌙", "Gemma-27B": "✨"}
    for i, (name, content) in enumerate(TRANSCRIPT, 1):
        print(f"  [{i}] {icons.get(name, '🤖')} {name}:")
        print(f"      {content}")
        print()
    print("══════════════════════════════════════════════════════════════════")
    print(f"  {len(TRANSCRIPT)} turns completed ✅")
    print("══════════════════════════════════════════════════════════════════")


if __name__ == "__main__":
    asyncio.run(main())
