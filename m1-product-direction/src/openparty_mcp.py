"""
OpenParty MCP Server — PoC 實作
================================
讓 Claude Code / Crush / 任何支援 MCP 的 AI 工具能加入 OpenParty Room。

這是最小化驗證：
- 只實作 join_room / send_message / get_history / observe_room 四個工具
- 使用 stdio transport（MCP 標準，不需要額外設定 port）
- 不修改現有的 server.py

使用方式（在 Claude Code 或 Crush 的 MCP 設定中加入）：
  {
    "mcpServers": {
      "openparty": {
        "type": "stdio",
        "command": "python",
        "args": ["/path/to/openparty_mcp.py"]
      }
    }
  }

或：
  claude mcp add --transport stdio openparty -- python /path/to/openparty_mcp.py
"""

import asyncio
import json
import sys
import uuid
import logging
from typing import Optional

import websockets
from mcp.server.fastmcp import FastMCP

# 只能寫到 stderr，不能寫 stdout（否則會污染 stdio transport 的 JSON-RPC）
logging.basicConfig(
    level=logging.DEBUG, stream=sys.stderr, format="%(asctime)s [MCP] %(message)s"
)
log = logging.getLogger(__name__)

# 全域狀態：目前 MCP session 中的連線
_ws = None  # websockets.ClientConnection instance
_agent_id: Optional[str] = None
_room_id: Optional[str] = None
_server_url: str = "ws://localhost:8765"
_pending_messages: list[dict] = []  # 暫存收到的 Room 訊息
_your_turn_event: Optional[asyncio.Event] = None
_your_turn_payload: Optional[dict] = None
_ws_task: Optional[asyncio.Task] = None
_ws_lock = asyncio.Lock()

mcp = FastMCP(
    "openparty",
    instructions="""
    OpenParty lets multiple AI agents from different tools join the same Room and discuss a topic together.
    
    Available tools:
    - join_room: Join an OpenParty Room with a given ID and agent name
    - send_message: Send a message to the Room you've joined
    - get_history: Get the conversation history of the Room
    - observe_room: Join as an observer (read-only, no turn-taking)
    - leave_room: Leave the current Room
    
    Typical usage:
    1. join_room(room_id="debate-001", name="Claude")
    2. Wait for your_turn signal, then send_message(content="Hello everyone!")
    3. Use get_history() to see what others have said
    4. leave_room() when done
    """,
)


def _ws_is_open(ws) -> bool:
    """Check if websocket connection is open (compatible with websockets 11-14+)."""
    if ws is None:
        return False
    # websockets 14+ uses close_code; older versions use closed attribute
    try:
        return ws.close_code is None
    except AttributeError:
        try:
            return not ws.closed
        except AttributeError:
            return False


async def _ws_listener(ws):
    """背景任務：持續監聽 WebSocket，暫存訊息。"""
    global _pending_messages, _your_turn_event, _your_turn_payload
    try:
        async for raw in ws:
            msg = json.loads(raw)
            msg_type = msg.get("type")
            log.debug(f"WS recv: {msg_type}")

            if msg_type == "your_turn":
                _your_turn_payload = msg
                if _your_turn_event:
                    _your_turn_event.set()
            else:
                _pending_messages.append(msg)
    except websockets.exceptions.ConnectionClosed:
        log.debug("WS connection closed")
    except Exception as e:
        log.error(f"WS listener error: {e}")


async def _ensure_connected(server_url: str = None) -> bool:
    """確保 WebSocket 連線存在。"""
    global _ws, _ws_task, _server_url
    if server_url:
        _server_url = server_url
    if _ws_is_open(_ws):
        return True
    try:
        _ws = await websockets.connect(_server_url)
        _ws_task = asyncio.create_task(_ws_listener(_ws))
        log.debug(f"Connected to {_server_url}")
        return True
    except Exception as e:
        log.error(f"Cannot connect to OpenParty server: {e}")
        return False


@mcp.tool()
async def join_room(
    room_id: str,
    name: str,
    model: str = "claude",
    server_url: str = "ws://localhost:8765",
) -> str:
    """
    Join an OpenParty Room.

    Args:
        room_id: The room identifier (e.g., "debate-001")
        name: Your display name in the room
        model: Your model name (e.g., "claude-sonnet")
        server_url: WebSocket server URL (default: ws://localhost:8765)

    Returns:
        Status message with room info and participants
    """
    global _ws, _agent_id, _room_id, _pending_messages, _your_turn_event

    _your_turn_event = asyncio.Event()
    _pending_messages = []

    if not await _ensure_connected(server_url):
        return f"❌ Cannot connect to OpenParty server at {server_url}. Make sure the server is running."

    _agent_id = str(uuid.uuid4())[:8]
    _room_id = room_id

    # 發送 join 訊息
    join_msg = {
        "type": "join",
        "room_id": room_id,
        "agent_id": _agent_id,
        "name": name,
        "model": model,
    }
    await _ws.send(json.dumps(join_msg))

    # 等待 joined 確認（最多 5 秒）
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < 5:
        # 從 pending 找 joined 訊息
        for i, msg in enumerate(_pending_messages):
            if msg.get("type") == "joined":
                _pending_messages.pop(i)
                agents = msg.get("agents_in_room", [])
                agent_names = [a["name"] for a in agents]
                return (
                    f"✅ Joined Room '{room_id}' as '{name}' (agent_id: {_agent_id})\n"
                    f"Participants: {', '.join(agent_names)}\n"
                    f"Tip: Use get_history() to see the conversation, "
                    f"or wait for your turn and use send_message() to speak."
                )
        await asyncio.sleep(0.1)

    return f"⚠️ Joined room '{room_id}' but didn't receive confirmation. The room may be waiting for more agents."


@mcp.tool()
async def send_message(content: str) -> str:
    """
    Send a message to the OpenParty Room you've joined.

    Args:
        content: The message content to send

    Returns:
        Confirmation that the message was sent
    """
    global _ws, _room_id

    if not _ws_is_open(_ws):
        return "❌ Not connected to any Room. Use join_room() first."

    if not content.strip():
        return "❌ Message content cannot be empty."

    msg = {"type": "message", "content": content}
    await _ws.send(json.dumps(msg))

    return f"✅ Message sent to Room '{_room_id}':\n\"{content[:100]}{'...' if len(content) > 100 else ''}\""


@mcp.tool()
async def get_history(max_messages: int = 10) -> str:
    """
    Get the recent conversation history from the Room.

    Args:
        max_messages: Maximum number of recent messages to return (default: 10)

    Returns:
        Formatted conversation history
    """
    global _pending_messages, _your_turn_payload

    # 收集所有訊息類型
    all_messages = []

    # 先看 your_turn payload 裡的 history（最完整的）
    if _your_turn_payload:
        history = _your_turn_payload.get("history", [])
        all_messages = history[-max_messages:]

    # 也加入 pending messages 裡的 message 類型
    recent_msgs = [m for m in _pending_messages if m.get("type") == "message"]
    all_messages.extend(recent_msgs[-max_messages:])

    if not all_messages:
        return "📭 No messages in history yet. The room may be empty or just started."

    lines = [f"📜 Room History (last {len(all_messages)} messages):"]
    lines.append("─" * 40)
    for entry in all_messages[-max_messages:]:
        name = entry.get("name", "Unknown")
        content = entry.get("content", "")
        lines.append(f"**{name}**: {content}")

    return "\n".join(lines)


@mcp.tool()
async def check_your_turn(timeout_seconds: float = 2.0) -> str:
    """
    Check if it's your turn to speak in the Room.

    Args:
        timeout_seconds: How long to wait for a turn signal (default: 2 seconds)

    Returns:
        Turn status and conversation context if it's your turn
    """
    global _your_turn_event, _your_turn_payload

    if not _ws_is_open(_ws):
        return "❌ Not connected to any Room. Use join_room() first."

    if _your_turn_event is None:
        _your_turn_event = asyncio.Event()

    # 檢查是否已有 your_turn 訊號
    if _your_turn_event.is_set():
        payload = _your_turn_payload
        _your_turn_event.clear()  # 重置，等待下次

        history = payload.get("history", [])
        context = payload.get("context", {})
        prompt = payload.get("prompt", "")

        lines = ["🎤 IT'S YOUR TURN!"]
        if prompt:
            lines.append(f"Topic: {prompt}")
        lines.append(f"Room: {context.get('topic', 'No topic')}")
        lines.append(f"Turn: {context.get('total_turns', 0) + 1}")
        lines.append("")
        lines.append("Recent conversation:")
        for msg in history[-5:]:
            lines.append(f"  {msg.get('name', '?')}: {msg.get('content', '')[:80]}...")
        lines.append("")
        lines.append("Use send_message() to respond!")
        return "\n".join(lines)

    # 等待新的 your_turn
    try:
        await asyncio.wait_for(_your_turn_event.wait(), timeout=timeout_seconds)
        payload = _your_turn_payload
        _your_turn_event.clear()

        history = payload.get("history", [])
        context = payload.get("context", {})
        prompt = payload.get("prompt", "")

        lines = ["🎤 IT'S YOUR TURN!"]
        if prompt:
            lines.append(f"Topic: {prompt}")
        lines.append(f"Room turn #{context.get('total_turns', 0) + 1}")
        lines.append("")
        for msg in history[-5:]:
            lines.append(f"  {msg.get('name', '?')}: {msg.get('content', '')[:80]}...")
        lines.append("")
        lines.append("Use send_message() to respond!")
        return "\n".join(lines)

    except asyncio.TimeoutError:
        return f"⏳ Not your turn yet (waited {timeout_seconds}s). Try again later or use get_history() to see what's happening."


@mcp.tool()
async def leave_room() -> str:
    """
    Leave the current OpenParty Room and disconnect.

    Returns:
        Confirmation message
    """
    global _ws, _ws_task, _agent_id, _room_id, _pending_messages, _your_turn_event

    if not _ws_is_open(_ws):
        return "ℹ️ Not connected to any Room."

    room = _room_id or "unknown"

    try:
        await _ws.send(json.dumps({"type": "leave"}))
        await asyncio.sleep(0.2)  # 讓伺服器處理 leave
    except Exception:
        pass

    try:
        if _ws_task:
            _ws_task.cancel()
        await _ws.close()
    except Exception:
        pass

    _ws = None
    _ws_task = None
    _agent_id = None
    _room_id = None
    _pending_messages = []
    _your_turn_event = None

    return f"👋 Left Room '{room}'. Disconnected."


@mcp.tool()
async def get_room_status() -> str:
    """
    Get the current connection status and room info.

    Returns:
        Current status including room ID, agent ID, and message count
    """
    if not _ws_is_open(_ws):
        return "🔌 Status: Not connected\nUse join_room() to join an OpenParty Room."

    pending_count = len(_pending_messages)
    your_turn = _your_turn_event is not None and _your_turn_event.is_set()

    lines = [
        f"✅ Status: Connected",
        f"Room: {_room_id or 'unknown'}",
        f"Agent ID: {_agent_id or 'unknown'}",
        f"Server: {_server_url}",
        f"Pending messages: {pending_count}",
        f"Your turn: {'YES 🎤' if your_turn else 'No'}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    log.info("OpenParty MCP Server starting (stdio transport)...")
    mcp.run(transport="stdio")
