"""
OpenParty - Groq Agent (Llama 3.3 70B)
========================================
Uses Groq's free tier with Llama 3.3 70B model.
Groq is OpenAI-compatible, so we just swap base_url.

Requires: GROQ_API_KEY env var
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

client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.environ.get("GROQ_API_KEY"),
)

MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """You are Llama, an AI made by Meta, joining a multi-LLM conversation room called OpenParty.
You are talking with another AI agent who may have different opinions.
Keep responses SHORT — 2-3 sentences only. Be direct and build on what was just said.
Do not introduce yourself repeatedly. Just engage with the conversation."""


def build_messages(payload: dict, my_model: str, system_prompt: str) -> list[dict]:
    """
    Convert the OpenParty payload into OpenAI-format messages.

    payload keys:
      history  — list of recent room messages (sliding window)
      summary  — str, compressed older history (empty if Phase 2 not active)
      prompt   — str, kickoff topic (only on first turn, may be None)
      context  — dict with topic, participants, total_turns
    """
    messages = [{"role": "system", "content": system_prompt}]

    # If there's a rolling summary, inject it so the agent knows what happened earlier
    summary = payload.get("summary", "").strip()
    if summary:
        messages.append(
            {
                "role": "system",
                "content": f"[Summary of earlier conversation]\n{summary}",
            }
        )

    # Inject room context header (topic + who's in the room)
    ctx = payload.get("context", {})
    topic = ctx.get("topic", "")
    participants = ctx.get("participants", [])
    total_turns = ctx.get("total_turns", 0)
    if topic or participants:
        participant_str = ", ".join(f"{p['name']} ({p['model']})" for p in participants)
        messages.append(
            {
                "role": "system",
                "content": (
                    f"[Room Info]\n"
                    f"Topic: {topic}\n"
                    f"Participants: {participant_str}\n"
                    f"Total turns so far: {total_turns}"
                ),
            }
        )

    # Convert recent history
    history = payload.get("history", [])
    for entry in history:
        if "agent_id" not in entry:
            continue
        is_me = my_model in entry.get("model", "")
        role = "assistant" if is_me else "user"
        label = f"[{entry.get('name', '?')}]: " if not is_me else ""
        messages.append({"role": role, "content": f"{label}{entry['content']}"})

    # First turn: no history yet, use the kickoff prompt as the opening user message
    prompt = payload.get("prompt")
    if prompt and not history:
        messages.append({"role": "user", "content": prompt})
    elif len([m for m in messages if m["role"] == "user"]) == 0:
        messages.append(
            {"role": "user", "content": "Please continue the conversation."}
        )

    return messages


async def llama_llm(payload: dict) -> str:
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
        name="Llama-3.3",
        model=MODEL,
        llm_fn=llama_llm,
        max_turns=5,
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
