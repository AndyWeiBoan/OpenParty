"""
OpenParty - GPT-4o Agent
=========================
Run this in one terminal:
    python agent_gpt.py

Requires: OPENAI_API_KEY env var
"""

import asyncio
import logging
import os

from openai import AsyncOpenAI

from agent_sdk import OpenPartyAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

SYSTEM_PROMPT = """You are GPT-4o, participating in a multi-LLM discussion party called OpenParty.
You are talking with other AI agents (possibly Claude, Gemini, etc.).
Keep your responses concise (2-3 sentences max). Be curious and engage with what others say.
Do NOT repeat what was already said. Build on the conversation naturally."""


async def gpt_llm(history: list[dict]) -> str:
    """Call OpenAI GPT-4o with the conversation history."""

    # Convert room history to OpenAI message format
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for entry in history:
        # Treat other agents as "user", ourselves as "assistant"
        role = "assistant" if entry.get("model", "").startswith("gpt") else "user"
        speaker_label = f"[{entry['name']}]: " if role == "user" else ""
        messages.append({"role": role, "content": f"{speaker_label}{entry['content']}"})

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=150,
        temperature=0.8,
    )

    return response.choices[0].message.content.strip()


async def main():
    agent = OpenPartyAgent(
        room_id="experiment-001",
        name="GPT-4o",
        model="gpt-4o",
        llm_fn=gpt_llm,
        max_turns=5,
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
