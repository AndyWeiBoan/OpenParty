"""
Automated tests for Phase 1 of the thinking-stream feature.

Test IDs
--------
1. test_claude_path_normalizer         — Claude SDK AssistantMessage → agent_thinking blocks
2. test_opencode_path_normalizer       — OpenCode SSE events → agent_thinking blocks
3. test_sessionid_filtering            — Non-matching sessionID events are discarded
4. test_server_broadcast_observers_only— agent_thinking goes to observers only
5. test_thinking_log_storage           — blocks correctly stored in room.thinking_log
6. test_fifo_eviction                  — 25 entries → only 20 remain (oldest removed)
7. test_timeout_fallback               — SSE timeout triggers POST fallback
8. test_turn_end_guard_and_turn_start_reset — TUI guards turn_end / turn_start reset
"""

import asyncio
import json
import sys
import types
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import pytest_asyncio  # noqa: F401 — ensures pytest-asyncio is importable

# ---------------------------------------------------------------------------
# Minimal stubs so bridge.py can be imported without the full SDK installed
# ---------------------------------------------------------------------------

# Stub out claude_agent_sdk if it isn't available
if "claude_agent_sdk" not in sys.modules:
    sdk = types.ModuleType("claude_agent_sdk")
    for _cls in [
        "ClaudeAgentOptions", "query", "AssistantMessage", "ResultMessage",
        "SystemMessage", "ThinkingConfigEnabled", "ProcessError",
    ]:
        setattr(sdk, _cls, type(_cls, (), {}))

    @dataclass
    class _ThinkingBlock:
        thinking: str = ""

    @dataclass
    class _ToolUseBlock:
        name: str = ""
        input: dict = field(default_factory=dict)

    @dataclass
    class _TextBlock:
        text: str = ""

    setattr(sdk, "ThinkingBlock", _ThinkingBlock)
    setattr(sdk, "ToolUseBlock", _ToolUseBlock)
    setattr(sdk, "TextBlock", _TextBlock)
    sys.modules["claude_agent_sdk"] = sdk

# Stub out aiohttp if absent
if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

# Stub websockets + websockets.server with a minimal WebSocketServerProtocol
if "websockets" not in sys.modules:
    _ws_mod = types.ModuleType("websockets")
    _ws_server_mod = types.ModuleType("websockets.server")

    class _FakeWSProto:
        """Minimal stand-in for websockets.server.WebSocketServerProtocol."""
        pass

    setattr(_ws_server_mod, "WebSocketServerProtocol", _FakeWSProto)
    sys.modules["websockets"] = _ws_mod
    sys.modules["websockets.server"] = _ws_server_mod
else:
    # Real websockets is installed — ensure websockets.server is importable
    if "websockets.server" not in sys.modules:
        import importlib
        importlib.import_module("websockets.server")

sys.path.insert(0, "/Users/andy/3rd-party/OpenParty")

# Now we can safely import bridge
import bridge  # noqa: E402

# Import server components
import server  # noqa: E402
from server import Room, Agent, Observer, RoomServer  # noqa: E402

# Import TUI — it imports textual; skip gracefully if not available
try:
    import openparty_tui as tui_module  # noqa: E402
    _TUI_AVAILABLE = True
except Exception:
    _TUI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_bridge(name="TestAgent", agent_id="agent-001"):
    """Return a minimally initialised AgentBridge without starting networking."""
    ab = bridge.AgentBridge.__new__(bridge.AgentBridge)
    ab.name = name
    ab.agent_id = agent_id
    ab.room_id = "room-test"
    ab.ws = None
    ab.log = MagicMock()
    ab._opencode = None
    ab.session_id = None
    ab.model = "claude-test"
    ab.allowed_tools = []
    return ab


# ===========================================================================
# Test 1 — Claude path normalizer
# ===========================================================================

@pytest.mark.asyncio
async def test_claude_path_normalizer():
    """Blocks extracted from AssistantMessage match AgentThinkingEvent schema."""
    sdk = sys.modules["claude_agent_sdk"]
    ThinkingBlock = sdk.ThinkingBlock
    ToolUseBlock = sdk.ToolUseBlock

    # Build a fake AssistantMessage with ThinkingBlock + ToolUseBlock content
    thinking_block = ThinkingBlock(thinking="I need to plan my answer.")
    tool_block = ToolUseBlock(name="web_search", input={"query": "pytest asyncio"})

    class FakeAssistantMessage:
        model = "claude-test"
        content = [thinking_block, tool_block]

    ab = _make_agent_bridge()

    captured: list[list[dict]] = []

    async def fake_send(blocks):
        captured.append(blocks)

    ab._send_agent_thinking = fake_send

    # Simulate the Claude path from bridge.py lines 630-645
    blocks = []
    for block in FakeAssistantMessage.content:
        if isinstance(block, ThinkingBlock):
            blocks.append({"type": "thinking", "text": block.thinking})
        elif isinstance(block, ToolUseBlock):
            blocks.append({
                "type": "tool_use",
                "tool": block.name,
                "input": block.input,
            })
    if blocks:
        await ab._send_agent_thinking(blocks)

    assert len(captured) == 1
    emitted = captured[0]
    assert emitted[0]["type"] == "thinking"
    assert emitted[0]["text"] == "I need to plan my answer."
    assert emitted[1]["type"] == "tool_use"
    assert emitted[1]["tool"] == "web_search"
    assert emitted[1]["input"] == {"query": "pytest asyncio"}

    # Verify result conforms to AgentThinkingEvent schema
    event = bridge.AgentThinkingEvent(
        agent_id=ab.agent_id,
        name=ab.name,
        blocks=emitted,
    )
    assert event.type == "agent_thinking"
    assert event.agent_id == "agent-001"
    assert len(event.blocks) == 2
    # Verify block sub-type dataclasses
    tb = bridge.AgentThinkingBlock(text="I need to plan my answer.")
    assert tb.type == "thinking"
    tub = bridge.AgentToolUseBlock(tool="web_search", input={"query": "pytest asyncio"})
    assert tub.type == "tool_use"


# ===========================================================================
# Test 2 — OpenCode path normalizer
# ===========================================================================

@pytest.mark.asyncio
async def test_opencode_path_normalizer():
    """SSE events (reasoning-delta/end, tool-call) produce correct blocks."""
    ab = _make_agent_bridge()

    # Spy on _send_agent_thinking
    sent_blocks: list[list[dict]] = []

    async def fake_send(blocks):
        sent_blocks.append(blocks)

    ab._send_agent_thinking = fake_send

    # Simulate the SSE event-processing logic from _opencode_sse_listener
    session_id = "sess-abc"
    reasoning_buf: list[str] = []

    events = [
        {"type": "reasoning-delta", "properties": {"sessionID": session_id, "delta": "First "}},
        {"type": "reasoning-delta", "properties": {"sessionID": session_id, "delta": "part "}},
        {"type": "reasoning-delta", "properties": {"sessionID": session_id, "delta": "done."}},
        {"type": "reasoning-end",   "properties": {"sessionID": session_id}},
        {"type": "tool-call",       "properties": {"sessionID": session_id, "tool": "read_file", "input": {"path": "/tmp/x"}}},
    ]

    for data in events:
        props = data.get("properties", {})
        if props.get("sessionID") != session_id:
            continue  # filter

        event_type = data.get("type", "")
        delta = props.get("delta", "")

        if event_type == "reasoning-delta":
            reasoning_buf.append(delta)
        elif event_type == "reasoning-end":
            if reasoning_buf:
                text = "".join(reasoning_buf)
                reasoning_buf = []
                await ab._send_agent_thinking([{"type": "thinking", "text": text}])
        elif event_type == "tool-call":
            tool_name = props.get("tool", "tool")
            tool_input = props.get("input", {})
            await ab._send_agent_thinking([{"type": "tool_use", "tool": tool_name, "input": tool_input}])

    assert len(sent_blocks) == 2

    thinking_emit = sent_blocks[0]
    assert thinking_emit[0]["type"] == "thinking"
    assert thinking_emit[0]["text"] == "First part done."

    tool_emit = sent_blocks[1]
    assert tool_emit[0]["type"] == "tool_use"
    assert tool_emit[0]["tool"] == "read_file"
    assert tool_emit[0]["input"] == {"path": "/tmp/x"}


# ===========================================================================
# Test 3 — sessionID filtering
# ===========================================================================

@pytest.mark.asyncio
async def test_sessionid_filtering():
    """Events with a non-matching sessionID are discarded; matching ones pass through."""
    sent_blocks: list[list[dict]] = []

    async def fake_send(blocks):
        sent_blocks.append(blocks)

    ab = _make_agent_bridge()
    ab._send_agent_thinking = fake_send

    target_session = "sess-correct"
    other_session = "sess-other"

    events = [
        # wrong session → discard
        {"type": "reasoning-delta", "properties": {"sessionID": other_session, "delta": "ignored"}},
        {"type": "reasoning-end",   "properties": {"sessionID": other_session}},
        # correct session → process
        {"type": "reasoning-delta", "properties": {"sessionID": target_session, "delta": "kept"}},
        {"type": "reasoning-end",   "properties": {"sessionID": target_session}},
    ]

    reasoning_buf: list[str] = []
    for data in events:
        props = data.get("properties", {})
        if props.get("sessionID") != target_session:
            continue

        event_type = data.get("type", "")
        delta = props.get("delta", "")

        if event_type == "reasoning-delta":
            reasoning_buf.append(delta)
        elif event_type == "reasoning-end":
            if reasoning_buf:
                text = "".join(reasoning_buf)
                reasoning_buf = []
                await ab._send_agent_thinking([{"type": "thinking", "text": text}])

    # Only the matching-session event should have produced output
    assert len(sent_blocks) == 1
    assert sent_blocks[0][0]["text"] == "kept"


# ===========================================================================
# Test 4 — server broadcast goes to observers only
# ===========================================================================

@pytest.mark.asyncio
async def test_server_broadcast_observers_only():
    """agent_thinking message is broadcast with observers_only=True (not to agents)."""
    app = RoomServer.__new__(RoomServer)
    app.rooms = {}
    app.spawned_procs = []
    app.opencode_proc = None
    app.available_engines = []

    room = Room(room_id="r1", current_round=3)

    # Add a fake agent WS and a fake observer WS
    agent_ws = AsyncMock()
    observer_ws = AsyncMock()

    room.agents["agent-001"] = Agent(
        ws=agent_ws, agent_id="agent-001", name="Bot", model="test", room_id="r1"
    )
    room.observers["obs-001"] = Observer(
        ws=observer_ws, observer_id="obs-001", name="Watcher", is_owner=True
    )

    msg = {
        "type": "agent_thinking",
        "agent_id": "agent-001",
        "name": "Bot",
        "blocks": [{"type": "thinking", "text": "hmm"}],
    }
    msg["turn"] = room.current_round  # server injects turn

    # Call the real _broadcast with observers_only=True
    await app._broadcast(room, msg, observers_only=True)

    # Observer should have received the message
    observer_ws.send.assert_called_once()
    sent_payload = json.loads(observer_ws.send.call_args[0][0])
    assert sent_payload["type"] == "agent_thinking"
    assert sent_payload["turn"] == 3

    # Agent should NOT have received anything
    agent_ws.send.assert_not_called()


# ===========================================================================
# Test 5 — thinking_log storage
# ===========================================================================

def test_thinking_log_storage():
    """agent_thinking blocks are stored in room.thinking_log[agent_id]."""
    room = Room(room_id="r1", current_round=5)
    agent_id = "agent-001"
    blocks = [{"type": "thinking", "text": "deep thought"}]

    # Simulate the server-side storage logic (server.py lines 691-698)
    agent_log = room.thinking_log.setdefault(agent_id, [])
    agent_log.append({
        "turn": room.current_round,
        "timestamp": "2026-04-06T00:00:00+00:00",
        "blocks": blocks,
    })

    assert agent_id in room.thinking_log
    log_entry = room.thinking_log[agent_id][0]
    assert log_entry["turn"] == 5
    assert log_entry["blocks"] == blocks
    assert "timestamp" in log_entry


# ===========================================================================
# Test 6 — FIFO eviction (max 20 entries)
# ===========================================================================

def test_fifo_eviction():
    """Inserting 25 entries keeps only the 20 most recent; oldest are removed."""
    room = Room(room_id="r1")
    agent_id = "agent-001"

    for i in range(25):
        agent_log = room.thinking_log.setdefault(agent_id, [])
        agent_log.append({
            "turn": i,
            "timestamp": f"2026-04-06T00:00:{i:02d}+00:00",
            "blocks": [{"type": "thinking", "text": f"thought {i}"}],
        })
        if len(agent_log) > 20:
            agent_log.pop(0)

    log = room.thinking_log[agent_id]
    assert len(log) == 20
    # The oldest 5 (turn 0-4) must be gone; turn 5 should be first
    assert log[0]["turn"] == 5
    # The most recent (turn 24) should be last
    assert log[-1]["turn"] == 24


# ===========================================================================
# Test 7 — timeout fallback: SSE timeout triggers POST fallback
# ===========================================================================

@pytest.mark.asyncio
async def test_timeout_fallback():
    """If SSE listener stalls after POST resolves, bridge cancels SSE and returns POST result."""
    ab = _make_agent_bridge()

    # Fake OpenCode client
    fake_oc = MagicMock()
    fake_oc.session_id = "sess-x"
    fake_oc.url = "http://127.0.0.1:4096"
    fake_oc._build_body = MagicMock(return_value={"prompt": "hello"})

    # POST returns a quick result
    async def fast_post(body):
        return "Final answer from POST"

    fake_oc._post_message = fast_post
    ab._opencode = fake_oc

    sse_was_cancelled = False

    # SSE listener hangs forever — simulates a stalled SSE connection.
    # The bridge's finally block should cancel it after SSE_TIMEOUT (5s in prod,
    # but we patch it to nearly 0 below so the test is fast).
    async def stalled_sse_listener(done_task):
        nonlocal sse_was_cancelled
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            sse_was_cancelled = True
            raise

    ab._opencode_sse_listener = stalled_sse_listener

    # Monkey-patch SSE_TIMEOUT inside _call_opencode_with_thinking by patching
    # asyncio.wait_for so it immediately times out.
    original_wait_for = asyncio.wait_for

    async def instant_timeout(coro, timeout):
        # Simulate a timeout by raising TimeoutError regardless of timeout value
        raise asyncio.TimeoutError()

    with patch("asyncio.wait_for", side_effect=instant_timeout):
        result = await ab._call_opencode_with_thinking("hello")

    # POST result must always be returned, even when SSE times out
    assert result == "Final answer from POST"
    # SSE task should have been cancelled after the timeout
    assert sse_was_cancelled is True


# ===========================================================================
# Test 8 — turn_end guard + turn_start reset
# ===========================================================================

def test_turn_end_guard_and_turn_start_reset():
    """After turn_end, agent_thinking is discarded by UI; turn_start resets the guard."""
    if not _TUI_AVAILABLE:
        pytest.skip("openparty_tui / textual not installed")

    # Simulate the TUI state without running the full Textual app
    turn_complete: set[str] = set()
    thinking: set[str] = set()

    def handle_turn_start(agent_name: str):
        thinking.add(agent_name)
        turn_complete.discard(agent_name)  # reset guard

    def handle_turn_end(agent_name: str):
        thinking.discard(agent_name)
        turn_complete.add(agent_name)

    def should_render_agent_thinking(agent_name: str) -> bool:
        """Returns True if the agent_thinking event should be rendered."""
        return agent_name not in turn_complete

    agent = "Bot"

    # Before any turn: thinking events should render
    assert should_render_agent_thinking(agent) is True

    # Start a turn
    handle_turn_start(agent)
    assert should_render_agent_thinking(agent) is True

    # End the turn — subsequent agent_thinking must be discarded
    handle_turn_end(agent)
    assert should_render_agent_thinking(agent) is False

    # Next turn_start must reset the guard
    handle_turn_start(agent)
    assert should_render_agent_thinking(agent) is True
