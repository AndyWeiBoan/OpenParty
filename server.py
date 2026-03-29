"""
OpenParty Room Server — M2
===========================
WebSocket hub that:
- Creates rooms by room_id
- Broadcasts messages from one agent to all others in the same room
- Tracks who is in the room and assigns speaker turns (round-robin)
- Supports read-only Observer connections (role: "observer")
- Emits structured events (turn_start, turn_end, room_state) for Observer UI

M2 changes vs M0:
  - host="0.0.0.0" for cross-machine access
  - Observer support (role: "observer" in join message)
  - agents_remaining in agent_left (bug fix: agents don't all leave together)
  - Turn reassignment when current speaker leaves
  - Richer event types: turn_start, turn_end, room_state
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SLIDING_WINDOW_SIZE = 20


@dataclass
class Agent:
    ws: WebSocketServerProtocol
    agent_id: str
    name: str
    model: str
    room_id: str


@dataclass
class Observer:
    ws: WebSocketServerProtocol
    observer_id: str
    name: str


@dataclass
class Room:
    room_id: str
    agents: dict[str, Agent] = field(default_factory=dict)
    observers: dict[str, Observer] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    current_speaker: Optional[str] = None
    topic: str = (
        "Today's topic: Should AI have its own opinions, or always stay neutral? "
        "Please share your view directly."
    )
    rolling_summary: str = ""  # Phase 2: filled by async summariser
    turn_started_at: float = 0.0  # monotonic timestamp when current turn began

    def context_window(self) -> list[dict]:
        return self.history[-SLIDING_WINDOW_SIZE:]

    def next_speaker(self, exclude_id: str) -> Optional[Agent]:
        agent_ids = list(self.agents.keys())
        if len(agent_ids) <= 1:
            return None
        try:
            current_idx = agent_ids.index(exclude_id)
        except ValueError:
            current_idx = -1
        next_idx = (current_idx + 1) % len(agent_ids)
        if agent_ids[next_idx] == exclude_id:
            next_idx = (next_idx + 1) % len(agent_ids)
        return self.agents[agent_ids[next_idx]]

    def room_state_payload(self) -> dict:
        """Snapshot of room state — sent to observers on each turn boundary."""
        return {
            "type": "room_state",
            "room_id": self.room_id,
            "topic": self.topic,
            "turn_count": len(self.history),
            "current_speaker": self.current_speaker,
            "participants": [
                {"agent_id": a.agent_id, "name": a.name, "model": a.model}
                for a in self.agents.values()
            ],
            "observers": len(self.observers),
        }


class RoomServer:
    def __init__(self):
        self.rooms: dict[str, Room] = {}

    def get_or_create_room(self, room_id: str) -> Room:
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(room_id=room_id)
            log.info(f"Room created: {room_id}")
        return self.rooms[room_id]

    async def _broadcast(
        self,
        room: Room,
        message: dict,
        exclude_id: Optional[str] = None,
        agents_only: bool = False,
    ):
        """Send message to all agents (and observers unless agents_only=True)."""
        payload = json.dumps(message)
        targets = [a.ws for aid, a in room.agents.items() if aid != exclude_id]
        if not agents_only:
            targets += [o.ws for o in room.observers.values()]
        if targets:
            await asyncio.gather(
                *[ws.send(payload) for ws in targets], return_exceptions=True
            )

    async def _send_your_turn(self, room: Room, agent: Agent, kickoff: bool = False):
        """Send your_turn to an agent and emit turn_start to observers."""
        room.current_speaker = agent.agent_id
        room.turn_started_at = time.monotonic()

        your_turn_payload = {
            "type": "your_turn",
            "history": room.context_window(),
            "summary": room.rolling_summary,
            "context": {
                "topic": room.topic,
                "participants": [
                    {"name": a.name, "model": a.model} for a in room.agents.values()
                ],
                "total_turns": len(room.history),
            },
        }
        if kickoff:
            your_turn_payload["prompt"] = room.topic

        await agent.ws.send(json.dumps(your_turn_payload))

        # Notify observers
        await self._broadcast(
            room,
            {
                "type": "turn_start",
                "agent_id": agent.agent_id,
                "name": agent.name,
                "model": agent.model,
                "turn_number": len(room.history) + 1,
            },
            agents_only=False,
        )

        log.info(f"[{room.room_id}] → turn to {agent.name}")

    async def handle_connection(self, ws: WebSocketServerProtocol):
        identity: Optional[Agent | Observer] = None
        room: Optional[Room] = None

        try:
            raw = await ws.recv()
            msg = json.loads(raw)

            if msg.get("type") != "join":
                await ws.send(
                    json.dumps(
                        {"type": "error", "message": "First message must be 'join'"}
                    )
                )
                return

            room_id = msg.get("room_id", "default")
            role = msg.get("role", "agent")  # "agent" or "observer"
            name = msg.get("name", "unknown")
            room = self.get_or_create_room(room_id)

            # ── Observer path ─────────────────────────────────────────────────
            if role == "observer":
                import uuid

                observer_id = msg.get("observer_id", str(uuid.uuid4())[:8])
                obs = Observer(ws=ws, observer_id=observer_id, name=name)
                room.observers[observer_id] = obs
                identity = obs

                log.info(f"Observer joined | room={room_id} | name={name}")

                await ws.send(
                    json.dumps(
                        {
                            "type": "joined",
                            "role": "observer",
                            "room_id": room_id,
                            "observer_id": observer_id,
                            "room_state": room.room_state_payload(),
                            "history": room.context_window(),
                        }
                    )
                )

                # Observers just listen — they don't send messages
                async for raw_msg in ws:
                    pass  # ignore any incoming from observer (future: allow chat-in)

                return

            # ── Agent path ────────────────────────────────────────────────────
            agent_id = msg.get("agent_id", name)
            model = msg.get("model", "unknown")

            agent = Agent(
                ws=ws, agent_id=agent_id, name=name, model=model, room_id=room_id
            )
            room.agents[agent_id] = agent
            identity = agent

            log.info(f"Agent joined | room={room_id} | agent={name} ({model})")

            await ws.send(
                json.dumps(
                    {
                        "type": "joined",
                        "role": "agent",
                        "room_id": room_id,
                        "agent_id": agent_id,
                        "agents_in_room": [
                            {"agent_id": a.agent_id, "name": a.name, "model": a.model}
                            for a in room.agents.values()
                        ],
                    }
                )
            )

            await self._broadcast(
                room,
                {
                    "type": "agent_joined",
                    "agent_id": agent_id,
                    "name": name,
                    "model": model,
                    "agents_in_room": len(room.agents),
                },
                exclude_id=agent_id,
            )

            # Kickoff when second agent arrives
            if len(room.agents) >= 2 and room.current_speaker is None:
                first_agent = next(iter(room.agents.values()))
                await self._send_your_turn(room, first_agent, kickoff=True)

            # Main message loop
            async for raw_msg in ws:
                msg = json.loads(raw_msg)

                if msg["type"] == "message":
                    content = msg["content"]
                    timestamp = datetime.now(timezone.utc).isoformat()
                    latency_ms = int((time.monotonic() - room.turn_started_at) * 1000)

                    entry = {
                        "agent_id": agent_id,
                        "name": name,
                        "model": model,
                        "content": content,
                        "timestamp": timestamp,
                    }
                    room.history.append(entry)
                    log.info(f"[{room_id}] {name} ({latency_ms}ms): {content[:80]}...")

                    # Broadcast message to agents + observers
                    await self._broadcast(room, {"type": "message", **entry})

                    # Emit turn_end for observers
                    await self._broadcast(
                        room,
                        {
                            "type": "turn_end",
                            "agent_id": agent_id,
                            "name": name,
                            "latency_ms": latency_ms,
                            "turn_number": len(room.history),
                        },
                    )

                    # Hand turn to next agent
                    next_agent = room.next_speaker(exclude_id=agent_id)
                    if next_agent:
                        await self._send_your_turn(room, next_agent)

                elif msg["type"] == "leave":
                    break

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
        finally:
            if room is None:
                return

            # Clean up observer
            if isinstance(identity, Observer):
                room.observers.pop(identity.observer_id, None)
                log.info(f"Observer left | room={room.room_id} | name={identity.name}")
                return

            # Clean up agent
            if isinstance(identity, Agent):
                agent = identity
                room.agents.pop(agent.agent_id, None)
                remaining = len(room.agents)
                log.info(
                    f"Agent left | room={room.room_id} | agent={agent.name} | remaining={remaining}"
                )

                await self._broadcast(
                    room,
                    {
                        "type": "agent_left",
                        "agent_id": agent.agent_id,
                        "name": agent.name,
                        "agents_remaining": remaining,
                    },
                )

                # Reassign turn if the speaker just left
                if room.current_speaker == agent.agent_id and remaining >= 2:
                    next_agent = next(iter(room.agents.values()))
                    await self._send_your_turn(room, next_agent)
                    log.info(
                        f"Turn reassigned to {next_agent.name} after {agent.name} left"
                    )


async def main():
    server = RoomServer()
    host = "0.0.0.0"
    port = 8765

    log.info(f"OpenParty Room Server starting on ws://{host}:{port}")
    log.info("Cross-machine agents welcome. Waiting for connections...")

    async with websockets.serve(server.handle_connection, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
