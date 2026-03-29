"""
OpenParty - Gemini Agent (Google Gemini 2.0 Flash)
====================================================
Uses Google Gemini API (free tier via AI Studio key).

Requires: GEMINI_API_KEY env var
"""

import asyncio
import logging
import os

from google import genai
from google.genai import types

from agent_sdk import OpenPartyAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

MODEL = "gemma-3-27b-it"

SYSTEM_PROMPT = """You are Gemma, an open model made by Google, joining a multi-LLM conversation room called OpenParty.
You are talking with other AI agents from different companies (like Llama by Meta, Kimi by Moonshot AI).
Keep responses SHORT — 2-3 sentences only. Be direct and engage with the last point made.
Do not introduce yourself repeatedly. Just respond naturally to what was said."""


async def gemini_llm(payload: dict) -> str:
    history = payload.get("history", [])
    summary = payload.get("summary", "").strip()
    prompt = payload.get("prompt")
    ctx = payload.get("context", {})
    topic = ctx.get("topic", "")
    participants = ctx.get("participants", [])

    # Build a combined context preamble for Gemma (no system_instruction support)
    preamble_parts = [f"[System]: {SYSTEM_PROMPT}"]
    if topic:
        participant_str = ", ".join(f"{p['name']}" for p in participants)
        preamble_parts.append(
            f"[Room] Topic: {topic} | Participants: {participant_str}"
        )
    if summary:
        preamble_parts.append(f"[Earlier summary]: {summary}")

    preamble = "\n".join(preamble_parts)

    # Build Gemini-format contents
    contents = [
        types.Content(role="user", parts=[types.Part(text=preamble)]),
        types.Content(
            role="model", parts=[types.Part(text="Understood. Ready to engage.")]
        ),
    ]

    # Add recent history
    for entry in history:
        if "agent_id" not in entry:
            continue
        is_me = (
            "gemma" in entry.get("model", "").lower()
            or "gemini" in entry.get("model", "").lower()
        )
        role = "model" if is_me else "user"
        label = f"[{entry.get('name', '?')}]: " if not is_me else ""
        text = f"{label}{entry['content']}"

        # Merge consecutive same-role turns (Gemini API requirement)
        if contents and contents[-1].role == role:
            contents[-1].parts[0].text += f"\n{text}"
        else:
            contents.append(types.Content(role=role, parts=[types.Part(text=text)]))

    # First turn: inject the kickoff prompt
    if not history and prompt:
        contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

    config = types.GenerateContentConfig(max_output_tokens=120, temperature=0.8)

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL,
        contents=contents,
        config=config,
    )
    return response.text.strip()


async def main():
    agent = OpenPartyAgent(
        room_id="groq-debate-001",
        name="Gemini-2.0",
        model=MODEL,
        llm_fn=gemini_llm,
        max_turns=5,
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
