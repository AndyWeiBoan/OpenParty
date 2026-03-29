"""
OpenParty - Claude Agent
=========================
Run this in another terminal:
    python agent_claude.py

Requires: ANTHROPIC_API_KEY env var
"""

import asyncio
import logging
import os

import anthropic

from agent_sdk import OpenPartyAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are Claude, participating in a multi-LLM discussion party called OpenParty.
You are talking with other AI agents (possibly GPT-4o, Gemini, etc.).
Keep your responses concise (2-3 sentences max). Be thoughtful and engage with what others say.
Do NOT repeat what was already said. Build on the conversation naturally."""


async def claude_llm(history: list[dict]) -> str:
    """Call Anthropic Claude with the conversation history."""

    # Convert room history to Anthropic message format
    # Anthropic requires alternating user/assistant messages
    messages = []

    for entry in history:
        # Treat other agents as "user", Claude as "assistant"
        is_claude = "claude" in entry.get("model", "").lower()
        role = "assistant" if is_claude else "user"
        speaker_label = f"[{entry['name']}]: " if not is_claude else ""
        content = f"{speaker_label}{entry['content']}"

        # Merge consecutive same-role messages (Anthropic requirement)
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += f"\n{content}"
        else:
            messages.append({"role": role, "content": content})

    # Anthropic requires starting with "user"
    if not messages:
        messages = [{"role": "user", "content": "Please start the conversation."}]
    elif messages[0]["role"] == "assistant":
        messages.insert(0, {"role": "user", "content": "[conversation start]"})

    response = await client.messages.create(
        model="claude-opus-4-5",
        system=SYSTEM_PROMPT,
        messages=messages,
        max_tokens=150,
    )

    return response.content[0].text.strip()


async def main():
    agent = OpenPartyAgent(
        room_id="experiment-001",
        name="Claude",
        model="claude-opus-4-5",
        llm_fn=claude_llm,
        max_turns=5,
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
