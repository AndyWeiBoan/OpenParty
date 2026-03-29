"""
OpenParty Agent SDK - MVP Experiment
=====================================
A minimal SDK for any LLM agent to:
- Connect to a Room Server
- Receive "your_turn" signals
- Generate a reply using any LLM
- Send the reply back to the room

Usage:
    agent = OpenPartyAgent(
        room_id="party-001",
        agent_id="gpt-agent",
        name="GPT-4o",
        model="gpt-4o",
        llm_fn=my_llm_function,  # async fn(history) -> str
        max_turns=5,
    )
    await agent.run()
"""

import asyncio
import json
import logging
import uuid
from typing import Callable, Awaitable

import websockets

log = logging.getLogger(__name__)


class OpenPartyAgent:
    def __init__(
        self,
        room_id: str,
        name: str,
        model: str,
        llm_fn: Callable[[list[dict]], Awaitable[str]],
        agent_id: str | None = None,
        server_url: str = "ws://localhost:8765",
        max_turns: int = 5,
    ):
        self.room_id = room_id
        self.agent_id = agent_id or str(uuid.uuid4())[:8]
        self.name = name
        self.model = model
        self.llm_fn = llm_fn
        self.server_url = server_url
        self.max_turns = max_turns
        self.turns_taken = 0

    async def run(self):
        log.info(f"[{self.name}] Connecting to {self.server_url} ...")

        async with websockets.connect(self.server_url) as ws:
            # Join the room
            await ws.send(
                json.dumps(
                    {
                        "type": "join",
                        "room_id": self.room_id,
                        "agent_id": self.agent_id,
                        "name": self.name,
                        "model": self.model,
                    }
                )
            )

            # Main loop
            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "joined":
                    log.info(
                        f"[{self.name}] Joined room '{self.room_id}'. "
                        f"Agents present: {[a['name'] for a in msg['agents_in_room']]}"
                    )

                elif msg_type == "agent_joined":
                    log.info(
                        f"[{self.name}] New agent joined: {msg['name']} ({msg['model']})"
                    )

                elif msg_type == "agent_left":
                    log.info(f"[{self.name}] Agent left: {msg['name']}")
                    # Only leave if the room now has nobody else to talk to.
                    # The server tells us the remaining count via agents_remaining.
                    # If the field is missing (older server), fall back to leaving
                    # only when the count is explicitly 0 or 1 (just us).
                    remaining = msg.get("agents_remaining", None)
                    if remaining is not None and remaining >= 2:
                        # Room still has enough participants — keep going
                        log.info(
                            f"[{self.name}] {remaining} agents still in room, continuing."
                        )
                    else:
                        log.info(f"[{self.name}] Room too small, leaving.")
                        await ws.send(json.dumps({"type": "leave"}))
                        break

                elif msg_type == "message":
                    # Just log what others say (we already handle our own messages via your_turn)
                    if msg["agent_id"] != self.agent_id:
                        log.info(
                            f"[{self.name}] Heard {msg['name']}: {msg['content'][:60]}..."
                        )

                elif msg_type == "your_turn":
                    if self.turns_taken >= self.max_turns:
                        log.info(
                            f"[{self.name}] Reached max turns ({self.max_turns}). Leaving."
                        )
                        await ws.send(json.dumps({"type": "leave"}))
                        break

                    history = msg.get("history", [])
                    prompt = msg.get("prompt")  # kickoff topic (first turn only)
                    summary = msg.get(
                        "summary", ""
                    )  # rolling summary of older turns (Phase 2)
                    context = msg.get(
                        "context", {}
                    )  # room metadata: topic, participants, total_turns

                    total_turns = context.get("total_turns", len(history))
                    log.info(
                        f"[{self.name}] My turn! "
                        f"(turn {self.turns_taken + 1}/{self.max_turns}, "
                        f"room history: {total_turns} msgs, "
                        f"sending: {len(history)} msgs)"
                    )

                    # Build payload for llm_fn
                    # llm_fn receives a dict so it has everything it needs:
                    #   history  — recent window (list of room message dicts)
                    #   summary  — compressed older history (str, may be empty)
                    #   prompt   — kickoff topic if first turn (str, may be None)
                    #   context  — room metadata (dict)
                    payload = {
                        "history": history,
                        "summary": summary,
                        "prompt": prompt,
                        "context": context,
                    }

                    # Call the LLM
                    try:
                        reply = await self.llm_fn(payload)
                    except Exception as e:
                        log.error(f"[{self.name}] LLM error: {e}")
                        reply = f"[Error generating response: {e}]"

                    # Send reply to room
                    await ws.send(
                        json.dumps(
                            {
                                "type": "message",
                                "content": reply,
                            }
                        )
                    )

                    self.turns_taken += 1
                    log.info(
                        f"[{self.name}] Sent reply ({self.turns_taken}/{self.max_turns})"
                    )
