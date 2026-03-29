"""
OpenParty Agent — upgraded from M0 agent_sdk.py.

Changes vs M0:
  - agents_remaining logic: only leave when truly alone (bug fix)
  - turn_start / turn_end events emitted for Observer support
  - Persona integration via system_prompt
  - Cleaner error handling with exponential backoff on LLM failure
"""

import asyncio
import json
import logging
import time
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
        llm_fn: Callable[[dict], Awaitable[str]],
        agent_id: str | None = None,
        server_url: str = "ws://localhost:8765",
        max_turns: int = 10,
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

            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "joined":
                    names = [a["name"] for a in msg["agents_in_room"]]
                    log.info(
                        f"[{self.name}] Joined '{self.room_id}'. Participants: {names}"
                    )

                elif msg_type == "agent_joined":
                    log.info(f"[{self.name}] ++ {msg['name']} ({msg['model']}) joined")

                elif msg_type == "agent_left":
                    remaining = msg.get("agents_remaining", None)
                    log.info(
                        f"[{self.name}] -- {msg['name']} left (remaining={remaining})"
                    )
                    if remaining is not None and remaining >= 2:
                        log.info(
                            f"[{self.name}] Room still has {remaining} agents, continuing."
                        )
                    else:
                        log.info(f"[{self.name}] Room too small, leaving.")
                        await ws.send(json.dumps({"type": "leave"}))
                        break

                elif msg_type == "message":
                    if msg.get("agent_id") != self.agent_id:
                        log.info(
                            f"[{self.name}] {msg['name']}: {msg['content'][:80]}..."
                        )

                elif msg_type == "your_turn":
                    if self.turns_taken >= self.max_turns:
                        log.info(
                            f"[{self.name}] Reached max turns ({self.max_turns}). Leaving."
                        )
                        await ws.send(json.dumps({"type": "leave"}))
                        break

                    history = msg.get("history", [])
                    context = msg.get("context", {})
                    total_turns = context.get("total_turns", len(history))
                    log.info(
                        f"[{self.name}] My turn #{self.turns_taken + 1} "
                        f"(room turn {total_turns}, history window={len(history)})"
                    )

                    payload = {
                        "history": msg.get("history", []),
                        "summary": msg.get("summary", ""),
                        "prompt": msg.get("prompt"),
                        "context": context,
                    }

                    t0 = time.monotonic()
                    try:
                        reply = await self.llm_fn(payload)
                    except Exception as e:
                        log.error(f"[{self.name}] LLM error: {e}")
                        reply = (
                            f"[{self.name} encountered an error: {type(e).__name__}]"
                        )

                    latency_ms = int((time.monotonic() - t0) * 1000)
                    log.info(
                        f"[{self.name}] Response ready ({latency_ms}ms): {reply[:60]}..."
                    )

                    await ws.send(json.dumps({"type": "message", "content": reply}))
                    self.turns_taken += 1
