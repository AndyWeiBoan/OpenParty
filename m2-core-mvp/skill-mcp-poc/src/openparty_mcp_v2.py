"""
OpenParty MCP Server v2
========================
Connects Claude Code (or any MCP-compatible AI tool) to an OpenParty Room.

Changes from v1 (M1):
  - Compatible with M2 server.py (Observer events: turn_start, turn_end, room_state)
  - Cleaner your_turn loop: waits properly without busy-polling
  - send_and_wait(): one-call convenience for Claude — generate reply + send in one step
  - Reduced log noise on stderr (only INFO by default)

Setup:
  claude mcp add --transport stdio openparty -- python /path/to/openparty_mcp_v2.py

Then in Claude Code say:
  "Join room code-review-001 as Architect and discuss Redis caching"
"""

import asyncio
import json
import logging
import sys
import uuid
from typing import Optional

import websockets
from mcp.server.fastmcp import FastMCP

# Logs go to stderr only — stdout is reserved for MCP JSON-RPC
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [MCP] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Global state (one MCP process = one Room connection) ──────────────────────
_ws = None
_agent_id: Optional[str] = None
_room_id: Optional[str] = None
_agent_name: Optional[str] = None
_server_url: str = "ws://localhost:8765"

_pending_messages: list[dict] = []  # buffered room messages
_your_turn_event: Optional[asyncio.Event] = None
_your_turn_payload: Optional[dict] = None
_ws_listener_task: Optional[asyncio.Task] = None

# ── MCP server definition ─────────────────────────────────────────────────────
mcp = FastMCP(
    "openparty",
    instructions="""
OpenParty connects multiple AI agents into a shared Room for real-time discussion.

Workflow after joining a room:
1. Call join_room() → you are now in the Room
2. Call wait_for_turn() → blocks until it is your turn to speak
3. Call send_message(content) → send your reply to the Room
4. Repeat steps 2-3 until you decide to leave
5. Call leave_room() when done

Alternatively, use send_and_wait(content) which combines steps 3+2 into one call.

Tips:
- Read the conversation history in wait_for_turn() response before composing your reply
- Keep replies concise (2-4 sentences) so the conversation flows naturally
- You are an active participant — engage with what others just said
""",
)


# ── WebSocket helpers ─────────────────────────────────────────────────────────


def _ws_open() -> bool:
    if _ws is None:
        return False
    try:
        return _ws.close_code is None
    except AttributeError:
        try:
            return not _ws.closed
        except AttributeError:
            return False


async def _ws_listener(ws):
    """Background task: pump incoming WebSocket messages into buffers."""
    global _pending_messages, _your_turn_event, _your_turn_payload
    try:
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")
            log.debug(f"WS ← {t}")

            if t == "your_turn":
                _your_turn_payload = msg
                if _your_turn_event:
                    _your_turn_event.set()
            else:
                _pending_messages.append(msg)

    except websockets.exceptions.ConnectionClosed:
        log.info("WS connection closed")
    except Exception as e:
        log.error(f"WS listener error: {e}")


async def _connect(server_url: str) -> bool:
    global _ws, _ws_listener_task, _server_url
    _server_url = server_url
    if _ws_open():
        return True
    try:
        _ws = await websockets.connect(server_url)
        _ws_listener_task = asyncio.create_task(_ws_listener(_ws))
        log.info(f"Connected to {server_url}")
        return True
    except Exception as e:
        log.error(f"Cannot connect to OpenParty server at {server_url}: {e}")
        return False


async def _wait_for_message_type(msg_type: str, timeout: float = 5.0) -> Optional[dict]:
    """Poll _pending_messages for a specific type, up to timeout seconds."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for i, m in enumerate(_pending_messages):
            if m.get("type") == msg_type:
                return _pending_messages.pop(i)
        await asyncio.sleep(0.05)
    return None


# ── MCP Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def join_room(
    room_id: str,
    name: str,
    model: str = "claude",
    server_url: str = "ws://localhost:8765",
) -> str:
    """
    Join an OpenParty Room and wait for other participants.

    Args:
        room_id:    Room identifier, e.g. "code-review-001"
        name:       Your display name in the room, e.g. "Architect"
        model:      Your model name shown to other participants
        server_url: WebSocket server URL (default: ws://localhost:8765)

    Returns:
        Confirmation with room info and current participants.
    """
    global _ws, _agent_id, _room_id, _agent_name
    global _pending_messages, _your_turn_event

    _your_turn_event = asyncio.Event()
    _pending_messages = []

    if not await _connect(server_url):
        return (
            f"❌ Cannot connect to OpenParty server at {server_url}\n"
            f"Make sure the server is running: python server.py"
        )

    _agent_id = str(uuid.uuid4())[:8]
    _room_id = room_id
    _agent_name = name

    await _ws.send(
        json.dumps(
            {
                "type": "join",
                "room_id": room_id,
                "agent_id": _agent_id,
                "name": name,
                "model": model,
            }
        )
    )

    joined = await _wait_for_message_type("joined", timeout=5.0)
    if joined:
        agents = joined.get("agents_in_room", [])
        names = [a["name"] for a in agents]
        others = [n for n in names if n != name]
        status = (
            f"✅ Joined room '{room_id}' as '{name}'\n"
            f"Participants so far: {', '.join(names)}\n"
        )
        if others:
            status += f"Other agents present: {', '.join(others)}\n"
            status += "Call wait_for_turn() to know when to speak."
        else:
            status += "You are the first to arrive. Call wait_for_turn() — it will wait for others to join."
        return status

    return (
        f"⚠️  Joined room '{room_id}' as '{name}' but no confirmation received yet.\n"
        f"Call wait_for_turn() to proceed."
    )


@mcp.tool()
async def wait_for_turn(timeout_seconds: float = 120.0) -> str:
    """
    Wait until it is your turn to speak in the Room.

    Blocks until the server sends your_turn signal.
    Read the conversation history in the response before composing your reply.

    Args:
        timeout_seconds: Max seconds to wait (default 120 — rooms can be slow)

    Returns:
        Your turn context: topic, history of recent messages, and instructions.
    """
    global _your_turn_event, _your_turn_payload

    if not _ws_open():
        return "❌ Not connected. Call join_room() first."

    if _your_turn_event is None:
        _your_turn_event = asyncio.Event()

    # Already signalled?
    if _your_turn_event.is_set():
        payload = _your_turn_payload
        _your_turn_event.clear()
        return _format_your_turn(payload)

    # Wait for signal
    try:
        await asyncio.wait_for(_your_turn_event.wait(), timeout=timeout_seconds)
        payload = _your_turn_payload
        _your_turn_event.clear()
        return _format_your_turn(payload)
    except asyncio.TimeoutError:
        return (
            f"⏳ Still waiting after {timeout_seconds}s. "
            "The other participant may be thinking. Call wait_for_turn() again."
        )


def _format_your_turn(payload: dict) -> str:
    if not payload:
        return "🎤 It's your turn! Call send_message() to respond."

    history = payload.get("history", [])
    context = payload.get("context", {})
    prompt = payload.get("prompt", "")
    topic = context.get("topic", "")
    turn_num = context.get("total_turns", 0) + 1
    participants = context.get("participants", [])

    lines = ["🎤 YOUR TURN TO SPEAK"]
    lines.append(f"Turn #{turn_num}")
    if participants:
        lines.append(f"Participants: {', '.join(p['name'] for p in participants)}")
    if topic or prompt:
        lines.append(f"Topic: {prompt or topic}")
    lines.append("")

    if history:
        lines.append("── Recent conversation ──")
        for entry in history[-6:]:
            speaker = entry.get("name", "?")
            content = entry.get("content", "")
            lines.append(f"{speaker}: {content}")
        lines.append("")

    lines.append(
        "Read the conversation above, then call send_message(content) with your reply."
    )
    lines.append(
        "Keep it concise — 2-4 sentences. Engage directly with what was just said."
    )
    return "\n".join(lines)


@mcp.tool()
async def send_message(content: str) -> str:
    """
    Send your reply to the Room.

    Call this after wait_for_turn() signals it is your turn.
    After sending, call wait_for_turn() again to wait for the next turn.

    Args:
        content: Your message (2-4 sentences recommended)

    Returns:
        Confirmation that the message was sent.
    """
    if not _ws_open():
        return "❌ Not connected. Call join_room() first."
    if not content.strip():
        return "❌ Message cannot be empty."

    await _ws.send(json.dumps({"type": "message", "content": content}))
    preview = content[:80] + ("..." if len(content) > 80 else "")
    return (
        f"✅ Message sent to room '{_room_id}':\n\"{preview}\"\n\n"
        f"Now call wait_for_turn() to wait for your next turn."
    )


@mcp.tool()
async def send_and_wait(content: str, timeout_seconds: float = 120.0) -> str:
    """
    Send your message AND immediately wait for the next turn.
    Convenience tool that combines send_message() + wait_for_turn().

    Args:
        content:         Your message to send
        timeout_seconds: How long to wait for the next turn (default 120s)

    Returns:
        The next your_turn context when it arrives.
    """
    send_result = await send_message(content)
    if "❌" in send_result:
        return send_result
    wait_result = await wait_for_turn(timeout_seconds=timeout_seconds)
    return f"{send_result}\n\n{wait_result}"


@mcp.tool()
async def get_history(max_messages: int = 10) -> str:
    """
    Get the recent conversation history from the Room.

    Args:
        max_messages: Number of recent messages to return (default 10)

    Returns:
        Formatted conversation history.
    """
    messages = []

    if _your_turn_payload:
        messages = _your_turn_payload.get("history", [])[-max_messages:]

    recent = [m for m in _pending_messages if m.get("type") == "message"]
    if recent and not messages:
        messages = recent[-max_messages:]

    if not messages:
        return "📭 No messages yet. The room may be empty or just started."

    lines = [f"📜 Room '{_room_id}' — last {len(messages)} messages", "─" * 40]
    for m in messages:
        lines.append(f"{m.get('name', '?')}: {m.get('content', '')}")
    return "\n".join(lines)


@mcp.tool()
async def get_room_status() -> str:
    """
    Get your current connection status and room info.

    Returns:
        Current status: connected/disconnected, room ID, agent ID.
    """
    if not _ws_open():
        return "🔌 Not connected. Call join_room() to join a room."

    your_turn = _your_turn_event is not None and _your_turn_event.is_set()
    lines = [
        "✅ Connected to OpenParty",
        f"Room:      {_room_id or '?'}",
        f"Your name: {_agent_name or '?'}",
        f"Agent ID:  {_agent_id or '?'}",
        f"Server:    {_server_url}",
        f"Your turn: {'YES 🎤 — call send_message()' if your_turn else 'No — call wait_for_turn()'}",
    ]
    return "\n".join(lines)


@mcp.tool()
async def leave_room() -> str:
    """
    Leave the current Room and disconnect.

    Returns:
        Confirmation message.
    """
    global _ws, _ws_listener_task, _agent_id, _room_id, _agent_name
    global _pending_messages, _your_turn_event

    if not _ws_open():
        return "ℹ️  Not connected to any room."

    room = _room_id or "unknown"
    try:
        await _ws.send(json.dumps({"type": "leave"}))
        await asyncio.sleep(0.2)
    except Exception:
        pass

    try:
        if _ws_listener_task:
            _ws_listener_task.cancel()
        await _ws.close()
    except Exception:
        pass

    _ws = None
    _ws_listener_task = None
    _agent_id = None
    _room_id = None
    _agent_name = None
    _pending_messages = []
    _your_turn_event = None

    return f"👋 Left room '{room}'. Disconnected from OpenParty."


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("OpenParty MCP Server v2 starting (stdio transport)...")
    mcp.run(transport="stdio")
