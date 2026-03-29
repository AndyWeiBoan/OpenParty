"""
OpenParty - Groq Agent (Kimi K2 by Moonshot AI)
=================================================
Uses Groq's free tier with Kimi K2 model by Moonshot AI.
A completely different model family from Llama — truly heterogeneous!

Requires: GROQ_API_KEY env var
"""

import asyncio
import logging
import os

from openai import AsyncOpenAI

from agent_sdk import OpenPartyAgent
from agent_groq_llama import build_messages


async def kimi_llm(payload: dict) -> str:
    messages = build_messages(payload, my_model=MODEL, system_prompt=SYSTEM_PROMPT)

    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=120,
        temperature=0.8,
    )
    return response.choices[0].message.content.strip()


async def main():
    agent = OpenPartyAgent(
        room_id="groq-debate-001",
        name="Kimi-K2",
        model=MODEL,
        llm_fn=kimi_llm,
        max_turns=5,
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
