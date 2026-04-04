"""
OpenParty Room Server — M3
===========================
WebSocket hub that:
- Creates rooms by room_id
- Broadcasts messages from one agent to all others in the same room
- Tracks who is in the room and assigns speaker turns (round-robin)
- Supports read-only Observer connections (role: "observer")
- Emits structured events (turn_start, turn_end, room_state) for Observer UI
- Owner kickoff: agents wait until room owner sends first message to start

M3 changes vs M2:
  - Owner kickoff: discussion starts only when owner sends first message
  - Owner's first message sets the topic for the session
  - Agents are notified to wait for owner if no owner has spoken yet
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

_MENTION_RE = re.compile(r"#([\w][\w\-]*)")

import aiohttp
import websockets
from websockets.server import WebSocketServerProtocol

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
OPENCODE_PORT = 4096
OPENCODE_URL = f"http://127.0.0.1:{OPENCODE_PORT}"


def _check_opencode_installed() -> bool:
    return shutil.which("opencode") is not None


def _check_claude_installed() -> bool:
    """True if claude_agent_sdk with bundled binary is available."""
    try:
        import claude_agent_sdk
        bundled = os.path.join(
            os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude"
        )
        return os.path.isfile(bundled)
    except ImportError:
        pass
    return shutil.which("claude") is not None


async def _opencode_healthy() -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{OPENCODE_URL}/global/health",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as r:
                return r.status == 200
    except Exception:
        return False

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
    is_owner: bool = False


@dataclass
class Room:
    room_id: str
    agents: dict[str, Agent] = field(default_factory=dict)
    observers: dict[str, Observer] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    current_speaker: Optional[str] = None
    topic: str = ""  # Set by owner's first message
    rolling_summary: str = ""  # Phase 2: filled by async summariser
    turn_started_at: float = 0.0  # monotonic timestamp when current turn began
    owner_kicked_off: bool = False  # True after owner sends first message
    turn_pending: bool = False  # True while an agent is actively thinking
    round_speakers: set = field(default_factory=set)  # agents who spoke this round
    broadcast_pending: Optional[set] = None  # None = sequential mode; set = agent IDs yet to respond in broadcast
    # Private message support: maps history index → set of agent_ids allowed to see that entry
    private_visibility: dict = field(default_factory=dict)
    # Set to the target agent_id(s) while a private turn is in progress; None otherwise
    current_private_for: Optional[set] = None

    def context_window(self, agent_id: Optional[str] = None) -> list[dict]:
        """Return recent history window, filtering out private entries invisible to agent_id."""
        window = self.history[-SLIDING_WINDOW_SIZE:]
        if agent_id is None or not self.private_visibility:
            return window
        start_idx = max(0, len(self.history) - SLIDING_WINDOW_SIZE)
        return [
            entry
            for i, entry in enumerate(window)
            if (start_idx + i) not in self.private_visibility
            or agent_id in self.private_visibility[start_idx + i]
        ]

    def next_speaker(self, exclude_id: str) -> Optional[Agent]:
        """Return next agent who hasn't spoken this round. None if all have spoken."""
        agent_ids = list(self.agents.keys())
        # Find the next agent in join order who hasn't spoken this round
        try:
            start_idx = agent_ids.index(exclude_id)
        except ValueError:
            start_idx = -1
        for i in range(1, len(agent_ids) + 1):
            candidate_id = agent_ids[(start_idx + i) % len(agent_ids)]
            if candidate_id != exclude_id and candidate_id not in self.round_speakers:
                return self.agents[candidate_id]
        return None  # All agents have spoken this round

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
        self.spawned_procs: list[asyncio.subprocess.Process] = []
        self.opencode_proc: Optional[asyncio.subprocess.Process] = None
        self.available_engines: list[str] = []  # ["opencode", "claude"]

    async def startup(self):
        """Check installed tools and start opencode serve if available."""
        if _check_opencode_installed():
            if await _opencode_healthy():
                log.info("opencode serve already running — reusing")
                self.available_engines.append("opencode")
            else:
                log.info("Starting opencode serve...")
                log_path = os.path.join(_SERVER_DIR, "opencode_serve.log")
                lf = open(log_path, "w")
                self.opencode_proc = await asyncio.create_subprocess_exec(
                    "opencode", "serve", "--port", str(OPENCODE_PORT),
                    stdout=lf, stderr=lf,
                )
                # Wait up to 5 s for it to become healthy
                for _ in range(10):
                    await asyncio.sleep(0.5)
                    if await _opencode_healthy():
                        log.info(f"opencode serve started (pid={self.opencode_proc.pid})")
                        self.available_engines.append("opencode")
                        break
                else:
                    log.warning("opencode serve did not become healthy in time")
        else:
            log.info("opencode not installed — skipping")

        if _check_claude_installed():
            self.available_engines.append("claude")
            log.info("claude_agent_sdk detected — claude engine available")
        else:
            log.info("claude CLI not found — claude engine unavailable")

        log.info(f"Available engines: {self.available_engines}")

    async def _spawn_agent_process(self, room: Room, name: str, model_id: str, engine: str = "opencode", owner_name: str = "") -> bool:
        """Spawn a bridge.py subprocess and track it. Returns True on success."""
        bridge_path = os.path.join(_SERVER_DIR, "bridge.py")
        log_path = os.path.join(_SERVER_DIR, f"agent_{name}.log")

        cmd = [sys.executable, bridge_path, "--room", room.room_id, "--name", name, "--engine", engine]
        if engine == "opencode":
            cmd += ["--opencode-model", model_id, "--model", model_id]
        else:
            # claude engine: model_id is "claude/<model-name>", extract the model name
            claude_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id
            cmd += ["--model", claude_model]
        if owner_name:
            cmd += ["--owner-name", owner_name]

        try:
            log_file = open(log_path, "w")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=log_file,
                cwd=_SERVER_DIR,
            )
            self.spawned_procs.append(proc)
            log.info(f"Spawned agent '{name}' ({engine}/{model_id}) | pid={proc.pid} | room={room.room_id}")
            return True
        except Exception as e:
            log.error(f"Failed to spawn agent '{name}': {e}")
            return False

    async def shutdown(self):
        """Terminate all spawned agent and opencode serve processes."""
        all_procs = self.spawned_procs[:]
        if self.opencode_proc and self.opencode_proc.returncode is None:
            all_procs.append(self.opencode_proc)
        for proc in all_procs:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.spawned_procs.clear()

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
        observers_only: bool = False,
    ):
        """Send message to all agents (and observers unless agents_only=True).
        observers_only=True sends only to observers, skipping agents entirely."""
        payload = json.dumps(message)
        if observers_only:
            targets = list(o.ws for o in room.observers.values())
        else:
            targets = [a.ws for aid, a in room.agents.items() if aid != exclude_id]
            if not agents_only:
                targets += [o.ws for o in room.observers.values()]
        if targets:
            await asyncio.gather(
                *[ws.send(payload) for ws in targets], return_exceptions=True
            )

    async def _send_broadcast_turn(self, room: Room):
        """Send your_turn to ALL agents simultaneously (broadcast mode)."""
        agents = list(room.agents.values())
        if not agents:
            return

        room.broadcast_pending = {a.agent_id for a in agents}
        room.current_speaker = None
        room.turn_pending = False
        room.turn_started_at = time.monotonic()

        context_base = {
            "topic": room.topic,
            "participants": [{"name": a.name, "model": a.model} for a in agents],
            "total_turns": len(room.history),
        }

        # Build per-agent payloads so private history entries are properly filtered
        await asyncio.gather(
            *[
                a.ws.send(json.dumps({
                    "type": "your_turn",
                    "broadcast": True,
                    "history": room.context_window(a.agent_id),
                    "summary": room.rolling_summary,
                    "context": context_base,
                }))
                for a in agents
            ],
            return_exceptions=True,
        )

        # Notify observers: one turn_start per agent
        for agent in agents:
            await self._broadcast(room, {
                "type": "turn_start",
                "agent_id": agent.agent_id,
                "name": agent.name,
                "model": agent.model,
                "broadcast": True,
                "turn_number": len(room.history) + 1,
            }, agents_only=False)

        log.info(f"[{room.room_id}] broadcast → {[a.name for a in agents]}")

    async def _send_your_turn(self, room: Room, agent: Agent, kickoff: bool = False):
        """Send your_turn to an agent and emit turn_start to observers."""
        room.current_speaker = agent.agent_id
        room.turn_started_at = time.monotonic()
        room.turn_pending = True

        your_turn_payload = {
            "type": "your_turn",
            "history": room.context_window(agent.agent_id),
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
                is_owner = msg.get("owner", False)
                obs = Observer(ws=ws, observer_id=observer_id, name=name, is_owner=is_owner)

                # Kick out any existing owner if a new owner joins
                if is_owner:
                    for old_id, old_obs in list(room.observers.items()):
                        if old_obs.is_owner and old_id != observer_id:
                            log.info(f"Replacing old owner '{old_obs.name}' with '{name}'")
                            try:
                                await old_obs.ws.send(json.dumps({
                                    "type": "error",
                                    "message": f"你已被新的 owner '{name}' 取代，連線關閉。",
                                }))
                                await old_obs.ws.close()
                            except Exception:
                                pass
                            room.observers.pop(old_id, None)

                room.observers[observer_id] = obs
                identity = obs

                log.info(f"Observer joined | room={room_id} | name={name} | owner={is_owner}")

                await ws.send(
                    json.dumps(
                        {
                            "type": "joined",
                            "role": "observer",
                            "room_id": room_id,
                            "observer_id": observer_id,
                            "is_owner": is_owner,
                            "room_state": room.room_state_payload(),
                            "history": room.context_window(),
                            "available_engines": self.available_engines,
                        }
                    )
                )

                async for raw_msg in ws:
                    if not is_owner:
                        continue
                    try:
                        owner_msg = json.loads(raw_msg)
                    except Exception:
                        continue
                    msg_type = owner_msg.get("type")

                    # ── spawn_agent: server spawns a bridge subprocess ────────
                    if msg_type == "spawn_agent":
                        agent_name = owner_msg.get("name", "agent")   # different var from observer name
                        model_id = owner_msg.get("model", "")
                        engine = owner_msg.get("engine", "opencode")
                        if engine not in self.available_engines:
                            await ws.send(json.dumps({
                                "type": "spawn_result",
                                "name": agent_name,
                                "model": model_id,
                                "success": False,
                                "reason": f"engine '{engine}' not available on this server",
                            }))
                            continue
                        ok = await self._spawn_agent_process(room, agent_name, model_id, engine, owner_name=name)
                        await ws.send(json.dumps({
                            "type": "spawn_result",
                            "name": agent_name,
                            "model": model_id,
                            "engine": engine,
                            "success": ok,
                        }))
                        continue

                    # ── kick_all: owner removes every agent from the room ─────
                    if msg_type == "kick_all":
                        targets = list(room.agents.values())
                        if targets:
                            # Remove from room FIRST so subsequent owner messages
                            # don't see stale agents and send them your_turn.
                            for t in targets:
                                room.agents.pop(t.agent_id, None)
                            room.current_speaker = None
                            room.turn_pending = False
                            room.round_speakers = set()

                            await self._broadcast(room, {
                                "type": "system_message",
                                "text": "All agents were kicked from the room",
                            })

                            # Close WebSockets fire-and-forget (they're already removed)
                            async def _close(ws):
                                try:
                                    await asyncio.wait_for(ws.close(), timeout=2.0)
                                except Exception:
                                    pass
                            asyncio.ensure_future(
                                asyncio.gather(*[_close(t.ws) for t in targets])
                            )
                        continue

                    # ── kick_agent: owner removes an agent from the room ──────
                    if msg_type == "kick_agent":
                        kick_name = owner_msg.get("agent_name", "")
                        target = next(
                            (a for a in room.agents.values() if a.name == kick_name),
                            None,
                        )
                        if target:
                            log.info(f"[{room_id}] Owner kicked agent '{kick_name}'")
                            # Remove immediately so next owner message doesn't route to it
                            room.agents.pop(target.agent_id, None)
                            if room.current_speaker == target.agent_id:
                                room.current_speaker = None
                                room.turn_pending = False
                            room.round_speakers.discard(target.agent_id)
                            await self._broadcast(room, {
                                "type": "system_message",
                                "text": f"{kick_name} was kicked from the room",
                            })
                            try:
                                await target.ws.close()
                            except Exception:
                                pass
                        else:
                            await ws.send(json.dumps({
                                "type": "system_message",
                                "text": f"找不到成員 '{kick_name}'",
                            }))
                        continue

                    # ── broadcast: owner fires message to all agents at once ───
                    if msg_type == "broadcast":
                        content = owner_msg.get("content", "").strip()
                        if not content:
                            continue
                        if not room.agents:
                            await ws.send(json.dumps({
                                "type": "system_message",
                                "text": "目前沒有 agent 可以廣播，請先用 /add-agent 加入。",
                            }))
                            continue
                        timestamp = datetime.now(timezone.utc).isoformat()
                        entry = {
                            "agent_id": observer_id,
                            "name": name,
                            "model": "human",
                            "content": f"[broadcast] {content}",
                            "timestamp": timestamp,
                        }
                        room.history.append(entry)
                        room.round_speakers = set()
                        if not room.owner_kicked_off:
                            room.owner_kicked_off = True
                            room.topic = content
                        log.info(f"[{room_id}] [broadcast] {name}: {content[:80]}")
                        await self._broadcast(room, {"type": "message", **entry})
                        await self._send_broadcast_turn(room)
                        continue

                    if msg_type != "message":
                        continue
                    content = owner_msg.get("content", "").strip()
                    if not content:
                        continue
                    timestamp = datetime.now(timezone.utc).isoformat()
                    entry = {
                        "agent_id": observer_id,
                        "name": name,
                        "model": "human",
                        "content": content,
                        "timestamp": timestamp,
                    }

                    # ── Private message: #mention detected ───────────────────
                    mentions = _MENTION_RE.findall(content)
                    if mentions:
                        mention_set = set(mentions)
                        private_targets = [
                            a for a in room.agents.values() if a.name in mention_set
                        ]
                        if private_targets:
                            target = private_targets[0]
                            hist_idx = len(room.history)
                            room.history.append(entry)
                            room.private_visibility[hist_idx] = {target.agent_id}
                            room.current_private_for = {target.agent_id}
                            room.round_speakers = set()
                            room.broadcast_pending = None
                            if not room.owner_kicked_off:
                                room.owner_kicked_off = True
                                room.topic = content
                            log.info(
                                f"[{room_id}] [whisper→{target.name}] {name}: {content[:80]}"
                            )
                            # Broadcast to observers only (other agents must not see this)
                            await self._broadcast(room, {
                                "type": "message",
                                "is_private": True,
                                "private_to": [target.name],
                                **entry,
                            }, observers_only=True)
                            if not room.turn_pending:
                                await self._send_your_turn(room, target)
                            continue

                    # ── Normal (public) message ───────────────────────────────
                    room.history.append(entry)
                    log.info(f"[{room_id}] [owner] {name}: {content[:80]}")
                    await self._broadcast(room, {"type": "message", **entry})

                    # Each owner message starts a new round (also cancels any ongoing broadcast)
                    room.round_speakers = set()
                    room.broadcast_pending = None

                    if not room.owner_kicked_off and len(room.agents) >= 1:
                        room.owner_kicked_off = True
                        room.topic = content
                        log.info(f"[{room_id}] Owner kickoff! Topic set: {content[:60]}")

                    # Give first unspoken agent a turn (kickoff or new round)
                    if room.owner_kicked_off and len(room.agents) >= 1 and not room.turn_pending:
                        first_agent = next(iter(room.agents.values()))
                        await self._send_your_turn(room, first_agent, kickoff=not room.owner_kicked_off)
                    elif room.owner_kicked_off and len(room.agents) == 0:
                        await ws.send(json.dumps({
                            "type": "waiting_for_owner",
                            "message": "目前房間沒有 agent，請用 /add-agent 加入。",
                        }))

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

            # Notify agents to wait for owner kickoff (if owner hasn't spoken yet)
            if not room.owner_kicked_off:
                await self._broadcast(
                    room,
                    {
                        "type": "waiting_for_owner",
                        "message": "Waiting for room owner to set the topic and start the discussion.",
                        "agents_in_room": len(room.agents),
                    },
                    agents_only=True,
                )
            # If owner already kicked off and no turn is currently in progress,
            # give the newly joined agent a turn immediately so discussion continues.
            elif not room.turn_pending:
                await self._send_your_turn(room, agent, kickoff=True)

            # Main message loop
            async for raw_msg in ws:
                msg = json.loads(raw_msg)

                if msg["type"] == "update_model":
                    new_model = msg.get("model", "").strip()
                    if new_model and new_model != agent.model:
                        log.info(f"[{room_id}] {name} model updated: {agent.model} → {new_model}")
                        agent.model = new_model
                        model = new_model  # keep local var in sync for entry dicts
                        await self._broadcast(room, {
                            "type": "model_updated",
                            "agent_id": agent_id,
                            "name": name,
                            "model": new_model,
                        }, exclude_id=agent_id)
                    continue

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

                    # Check if this reply is part of a private turn
                    is_private_reply = (
                        room.current_private_for is not None
                        and agent_id in room.current_private_for
                    )

                    hist_idx = len(room.history)
                    room.history.append(entry)
                    if is_private_reply:
                        room.private_visibility[hist_idx] = room.current_private_for.copy()
                        room.current_private_for = None

                    log.info(f"[{room_id}] {name} ({latency_ms}ms): {content[:80]}...")

                    # Private replies go only to observers; public replies go to everyone
                    if is_private_reply:
                        await self._broadcast(room, {
                            "type": "message",
                            "is_private": True,
                            **entry,
                        }, observers_only=True)
                    else:
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

                    # Mark this agent as having spoken this round
                    room.round_speakers.add(agent_id)

                    if is_private_reply:
                        # ── Private round complete: return control to owner ───
                        room.turn_pending = False
                        room.current_speaker = None
                        log.info(f"[{room_id}] Private round complete. Waiting for owner.")
                        await self._broadcast(room, {
                            "type": "waiting_for_owner",
                            "message": "私訊回覆完成，等待下一則訊息。",
                        })
                    elif room.broadcast_pending is not None:
                        # ── Broadcast mode: track who's still pending ────────
                        room.broadcast_pending.discard(agent_id)
                        if not room.broadcast_pending:
                            room.broadcast_pending = None
                            room.current_speaker = None
                            log.info(f"[{room_id}] Broadcast round complete. Waiting for owner.")
                            await self._broadcast(room, {
                                "type": "waiting_for_owner",
                                "message": "All agents have responded. Waiting for your next message.",
                            })
                    else:
                        # ── Sequential mode: pass turn to next agent ─────────
                        room.turn_pending = False
                        next_agent = room.next_speaker(exclude_id=agent_id)
                        if next_agent:
                            await self._send_your_turn(room, next_agent)
                        else:
                            room.current_speaker = None
                            log.info(f"[{room_id}] Round complete. Waiting for owner.")
                            await self._broadcast(room, {
                                "type": "waiting_for_owner",
                                "message": "All agents have responded. Waiting for your next message.",
                            })

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

                # Clean up broadcast_pending if this agent hadn't responded yet
                if room.broadcast_pending is not None:
                    room.broadcast_pending.discard(agent.agent_id)
                    if not room.broadcast_pending:
                        room.broadcast_pending = None
                        room.current_speaker = None
                        log.info(f"[{room.room_id}] Broadcast round complete (agent left).")
                        await self._broadcast(room, {
                            "type": "waiting_for_owner",
                            "message": "All agents have responded. Waiting for your next message.",
                        })

                # If this agent was the target of a private turn, clear the private state
                if room.current_private_for and agent.agent_id in room.current_private_for:
                    room.current_private_for = None

                # Reassign turn if the speaker just left (sequential mode only)
                elif room.current_speaker == agent.agent_id:
                    if remaining >= 1:
                        next_agent = next(iter(room.agents.values()))
                        await self._send_your_turn(room, next_agent)
                        log.info(
                            f"Turn reassigned to {next_agent.name} after {agent.name} left"
                        )
                    else:
                        # No agents left — reset turn state entirely
                        room.current_speaker = None
                        room.turn_pending = False
                        room.round_speakers = set()
                        log.info("All agents gone — room turn state reset")


async def main():
    server = RoomServer()
    host = "0.0.0.0"
    port = 8765

    log.info(f"OpenParty Room Server starting on ws://{host}:{port}")
    log.info("Cross-machine agents welcome. Waiting for connections...")

    await server.startup()

    try:
        async with websockets.serve(server.handle_connection, host, port, ping_interval=60, ping_timeout=300):
            log.info(f"Server ready. Engines: {server.available_engines}")
            await asyncio.Future()
    finally:
        await server.shutdown()
        log.info("All spawned agents terminated.")


if __name__ == "__main__":
    asyncio.run(main())
