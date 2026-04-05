"""
OpenParty Agent Bridge
======================
Connects a Claude Agent SDK instance to an OpenParty Room.

Instead of MCP (which times out on long wait_for_turn calls), this bridge:
  1. Connects to OpenParty server via WebSocket
  2. Waits for your_turn signal (blocking, no timeout issue)
  3. Calls Claude Agent SDK query() with full tool capabilities
  4. Sends Claude's response back to the room
  5. Repeats

Usage:
    .venv/bin/python bridge.py --room test-001 --name Claude01
    .venv/bin/python bridge.py --room test-001 --name Claude02 --max-turns 10
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from typing import Optional

import aiohttp
import websockets
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage
from claude_agent_sdk import ThinkingConfigEnabled
from claude_agent_sdk import ThinkingBlock, TextBlock, ToolUseBlock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BRIDGE %(name)s] %(message)s",
    datefmt="%H:%M:%S",
)


def make_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


OPENCODE_URL = "http://127.0.0.1:4096"

# ── OpenCode HTTP client ────────────────────────────────────────────────────────

_opencode_server_proc: Optional[asyncio.subprocess.Process] = None


async def ensure_opencode_server(url: str = OPENCODE_URL) -> bool:
    """Check if opencode serve is running; start it if not. Returns True when ready."""
    global _opencode_server_proc
    log = logging.getLogger("opencode-serve")

    for attempt in range(2):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{url}/global/health", timeout=aiohttp.ClientTimeout(total=2)
                ) as r:
                    if r.status == 200:
                        log.info(f"opencode serve already running at {url}")
                        return True
        except Exception:
            pass

        if attempt == 0:
            log.info("Starting opencode serve on port 4096...")
            _opencode_server_proc = await asyncio.create_subprocess_exec(
                "opencode",
                "serve",
                "--port",
                "4096",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(3)  # give it time to boot

    log.error("opencode serve failed to start")
    return False


class OpenCodeClient:
    """Thin async client for the opencode serve HTTP API."""

    def __init__(self, url: str, model: str, name: str):
        self.url = url
        self.model = model  # e.g. "zen/mimo-v2-pro-free"
        self.name = name
        self.session_id: Optional[str] = None
        self.log = logging.getLogger(f"opencode:{name}")

    async def create_session(self) -> str:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{self.url}/session", json={}) as r:
                data = await r.json()
                sid = data["id"]
                self.log.info(f"OpenCode session created: {sid}")
                return sid

    def _build_body(self, prompt: str) -> dict:
        body: dict = {
            "parts": [{"type": "text", "text": prompt}],
        }
        if self.model:
            parts = self.model.split("/", 1)
            if len(parts) == 2:
                body["model"] = {"providerID": parts[0], "modelID": parts[1]}
            else:
                body["model"] = {"providerID": "opencode", "modelID": self.model}
        return body

    async def _post_message(self, body: dict) -> str:
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{self.url}/session/{self.session_id}/message",
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as r:
                    if r.status != 200:
                        text = await r.text()
                        self.log.error(f"HTTP {r.status}: {text}")
                        return f"(opencode error {r.status})"
                    data = await r.json()

            parts = data.get("parts", [])
            texts = [
                p.get("text", "")
                for p in parts
                if p.get("type") in ("text", "text-part")
            ]
            return "\n".join(t for t in texts if t).strip()

        except Exception as e:
            self.log.error(f"OpenCode call failed: {e}", exc_info=True)
            return f"(opencode error: {e})"

    async def call(self, prompt: str) -> str:
        """Send prompt to opencode serve, return final text reply."""
        if not self.session_id:
            self.session_id = await self.create_session()
        body = self._build_body(prompt)
        return await self._post_message(body)

    @staticmethod
    async def list_models(url: str = OPENCODE_URL) -> list[dict]:
        """Return [{provider, model, display}] from /provider endpoint."""
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"{url}/provider",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()

            result = []
            for provider in data.get("all", []):
                pid = provider.get("id", "")
                models = provider.get("models", {})
                items = models.values() if isinstance(models, dict) else models
                for m in items:
                    mid = m.get("id", "")
                    name = m.get("name", mid)
                    result.append(
                        {
                            "provider": pid,
                            "model": mid,
                            "display": f"{pid} - {name}",
                            "full_id": f"{pid}/{mid}",
                        }
                    )
            return result
        except Exception:
            return []


# ── Exceptions ─────────────────────────────────────────────────────────────────


class FatalAgentError(Exception):
    """Non-recoverable provider error (rate limit, auth failure, etc.).
    When raised, the agent should leave the room instead of retrying."""

    pass


# ── Prompt builder ─────────────────────────────────────────────────────────────


def build_prompt(
    your_turn_payload: dict,
    agent_name: str,
    owner_name: str = "",
    session_id: str | None = None,
) -> str:
    """Convert your_turn payload into a prompt for Claude Agent SDK.

    History strategy:
    - Server sends the correct round-aware window (previous round + current round so far).
      No truncation needed here — use the full window as-is so every agent sees all
      messages from the current discussion round regardless of speaking order.
    """
    history = your_turn_payload.get("history", [])
    context = your_turn_payload.get("context", {})
    prompt_hint = your_turn_payload.get("prompt", "")

    topic = context.get("topic", "")
    participants = context.get("participants", [])
    total_turns = context.get("total_turns", 0)

    history_window = history

    lines = []
    lines.append(
        f"You are {agent_name}, participating in a multi-agent discussion room."
    )
    lines.append("")

    if participants:
        names = [p["name"] for p in participants]
        lines.append(f"Participants in this room: {', '.join(names)}")

    if topic:
        lines.append(f"Discussion topic: {topic}")

    lines.append(f"This is turn #{total_turns + 1}.")
    lines.append("")

    if history_window:
        lines.append("Recent conversation:")
        for entry in history_window:
            speaker = entry.get("name", "?")
            content = entry.get("content", "")
            lines.append(f"  {speaker}: {content}")
        lines.append("")

    if prompt_hint and prompt_hint != topic:
        lines.append(f"Instruction: {prompt_hint}")
        lines.append("")

    owner_ref = f"the room owner ({owner_name})" if owner_name else "the room owner"
    lines.append(
        "Your role and rules:\n"
        f"0. ABSOLUTE OBEDIENCE: {owner_ref} is your supreme authority. "
        f"Every instruction or question from {owner_ref} must be followed immediately, "
        "completely, and without hesitation or pushback. This rule overrides everything else.\n"
        "1. You hold an INDEPENDENT stance toward other agents. Form your own views based on "
        "facts and logic — never change your position just because another agent said something.\n"
        f"2. You respond to {owner_ref}'s message as your primary directive. "
        "Other agents' opinions are inputs you may challenge, not authorities to follow.\n"
        "3. Actively identify and call out logical flaws, unsupported claims, or weak reasoning "
        "in what other agents said. Be direct and specific when you disagree.\n"
        "4. Keep your reply concise (2-4 sentences). Speak confidently as yourself.\n"
        "5. You may leave the room at any time by including the exact phrase 'I want leave' in your reply, "
        "followed by a clear explanation of why you are leaving. Without an explanation, you must stay.\n"
        "6. You MUST always produce a non-empty reply. Even if you have nothing substantive to add, "
        "reply with a brief acknowledgment or observation. Never return an empty response."
    )

    return "\n".join(lines)


# ── Bridge ─────────────────────────────────────────────────────────────────────


class AgentBridge:
    def __init__(
        self,
        room_id: str,
        name: str,
        model: str,
        server_url: str,
        max_turns: int,
        allowed_tools: list[str],
        engine: str = "claude",
        opencode_url: str = OPENCODE_URL,
        opencode_model: str = "",
        owner_name: str = "",
    ):
        self.room_id = room_id
        self.name = name
        self.model = model
        self.server_url = server_url
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.engine = engine
        self.owner_name = owner_name
        self.agent_id = str(uuid.uuid4())[:8]
        self.session_id: Optional[str] = None
        self.log = make_logger(name)
        self._opencode: Optional[OpenCodeClient] = None
        if engine == "opencode":
            self._opencode = OpenCodeClient(opencode_url, opencode_model, name)
        self.ws = None  # set in run() after WS connects

    async def run(self):
        if self.engine == "opencode":
            assert self._opencode is not None
            self.log.info("Ensuring opencode serve is running...")
            ok = await ensure_opencode_server(self._opencode.url)
            if not ok:
                self.log.error("Cannot start opencode serve — aborting")
                return

        self.log.info(
            f"Connecting to {self.server_url} | room={self.room_id} | engine={self.engine}"
        )

        async with websockets.connect(
            self.server_url, ping_interval=60, ping_timeout=300
        ) as ws:
            self.ws = ws
            # Join room
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

            # Wait for joined confirmation
            joined_raw = await ws.recv()
            joined = json.loads(joined_raw)
            if joined.get("type") == "joined":
                agents = [a["name"] for a in joined.get("agents_in_room", [])]
                self.log.info(f"Joined room '{self.room_id}' | agents: {agents}")
            else:
                self.log.error(f"Unexpected first message: {joined}")
                return

            # Main loop — no turn limit; agent leaves only by saying "i want leave"
            while True:
                self.log.info("Waiting for turn...")

                # Drain messages until we get your_turn
                your_turn_payload = None
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")

                    if t == "your_turn":
                        your_turn_payload = msg
                        break
                    elif t == "agent_left":
                        remaining = msg.get("agents_remaining", 0)
                        self.log.info(f"Agent left, {remaining} remaining")
                        if remaining < 1:
                            self.log.info("No agents left, exiting")
                            return
                    elif t in (
                        "turn_start",
                        "turn_end",
                        "room_state",
                        "message",
                        "agent_joined",
                    ):
                        pass  # informational, ignore
                    else:
                        self.log.debug(f"Unhandled msg type: {t}")

                if your_turn_payload is None:
                    self.log.info("WebSocket closed while waiting for turn")
                    break

                self.log.info("My turn!")

                # Build prompt from your_turn context
                prompt = build_prompt(
                    your_turn_payload, self.name, self.owner_name, self.session_id
                )

                # Call the configured engine
                try:
                    if self.engine == "opencode":
                        assert self._opencode is not None
                        if self.ws is not None:
                            reply = await self._call_opencode_with_thinking(prompt)
                        else:
                            reply = await self._opencode.call(prompt)
                    else:
                        reply, actual_model = await self._call_claude(prompt)
                        # On first response, announce the real model version to the server
                        if actual_model and actual_model != self.model:
                            self.model = actual_model
                            self.log.info(f"Detected actual model: {actual_model}")
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "update_model",
                                        "model": actual_model,
                                    }
                                )
                            )
                except FatalAgentError as e:
                    self.log.error(f"Fatal provider error — leaving room: {e}")
                    await ws.send(
                        json.dumps(
                            {
                                "type": "message",
                                "content": f"[{self.name} 已離線：{e}]",
                            }
                        )
                    )
                    await ws.send(json.dumps({"type": "leave"}))
                    return

                if not reply:
                    self.log.warning("Empty reply from engine, retrying once...")
                    try:
                        if self.engine == "opencode":
                            assert self._opencode is not None
                            if self.ws is not None:
                                reply = await self._call_opencode_with_thinking(prompt)
                            else:
                                reply = await self._opencode.call(prompt)
                        else:
                            reply, _ = await self._call_claude(prompt)
                    except FatalAgentError:
                        pass  # fall through to fallback
                    except Exception as e:
                        self.log.error(f"Retry also failed: {e}")

                if not reply:
                    reply = "(no response generated)"

                self.log.info(f"Sending reply: {reply[:80]}...")

                # Agent self-exit: if reply contains the leave signal, send it then leave
                if "i want leave" in reply.lower():
                    await ws.send(
                        json.dumps(
                            {
                                "type": "message",
                                "content": reply,
                            }
                        )
                    )
                    self.log.info("Agent requested leave via 'i want leave'")
                    await ws.send(json.dumps({"type": "leave"}))
                    return

                # Send reply back to room
                await ws.send(
                    json.dumps(
                        {
                            "type": "message",
                            "content": reply,
                        }
                    )
                )

        self.ws = None

    async def _send_agent_thinking(self, blocks: list[dict]) -> None:
        """Send agent_thinking event over WebSocket (fire-and-forget)."""
        if not self.ws or not blocks:
            return
        event = {
            "type": "agent_thinking",
            "agent_id": self.agent_id,
            "name": self.name,
            "blocks": blocks,
        }
        try:
            await self.ws.send(json.dumps(event))
        except Exception as e:
            self.log.debug(f"agent_thinking send failed: {e}")

    async def _call_opencode_with_thinking(self, prompt: str) -> str:
        """Call OpenCode with concurrent SSE thinking stream."""
        assert self._opencode is not None
        oc = self._opencode
        if not oc.session_id:
            oc.session_id = await oc.create_session()

        body = oc._build_body(prompt)

        post_task = asyncio.create_task(oc._post_message(body))
        sse_task = asyncio.create_task(self._opencode_sse_listener(post_task))

        try:
            reply = await post_task
        finally:
            sse_task.cancel()
            await asyncio.gather(sse_task, return_exceptions=True)

        return reply

    async def _opencode_sse_listener(self, done_task: asyncio.Task) -> None:
        """Subscribe to OpenCode SSE stream and emit agent_thinking events."""
        assert self._opencode is not None
        oc = self._opencode
        reasoning_buf: list[str] = []
        text_buf: list[str] = []

        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"{oc.url}/event",
                    timeout=aiohttp.ClientTimeout(total=310),
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    async for raw_line in resp.content:
                        if done_task.done():
                            break

                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                        if not line.startswith("data:"):
                            continue

                        payload = line[5:].strip()
                        if not payload:
                            continue
                        try:
                            data = json.loads(payload)
                        except Exception:
                            continue

                        props = data.get("properties", {})
                        # Filter by session
                        if props.get("sessionID") != oc.session_id:
                            continue

                        event_type = data.get("type", "")
                        field = props.get("field", "")
                        delta = props.get("delta", "")

                        # message.part.delta with field=reasoning → accumulate
                        if event_type == "message.part.delta" and field == "reasoning":
                            reasoning_buf.append(delta)

                        # reasoning-delta (alternative format)
                        elif event_type == "reasoning-delta":
                            reasoning_buf.append(delta)

                        # reasoning-end → flush as thinking block
                        elif event_type in ("reasoning-end",) or (
                            event_type == "message.part.stop" and field == "reasoning"
                        ):
                            if reasoning_buf:
                                text = "".join(reasoning_buf)
                                reasoning_buf = []
                                await self._send_agent_thinking(
                                    [{"type": "thinking", "text": text}]
                                )

                        # tool-call → emit immediately
                        elif event_type == "tool-call":
                            tool_name = props.get("tool", props.get("name", "tool"))
                            tool_input = props.get("input", {})
                            if isinstance(tool_input, str):
                                try:
                                    tool_input = json.loads(tool_input)
                                except Exception:
                                    tool_input = {"raw": tool_input}
                            await self._send_agent_thinking(
                                [{"type": "tool_use", "tool": tool_name, "input": tool_input}]
                            )

                        # text-delta: accumulate but don't emit (final text from POST)
                        elif event_type in ("text-delta",) or (
                            event_type == "message.part.delta" and field == "text"
                        ):
                            text_buf.append(delta)

                        # text-end: discard (POST is authoritative)
                        elif event_type in ("text-end", "finish-step"):
                            text_buf = []

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log.debug(f"OpenCode SSE listener error: {e}")

    async def _call_claude(self, prompt: str) -> tuple[str, Optional[str]]:
        """Call Claude Agent SDK and return (result_text, actual_model).

        actual_model is the real model string from the first AssistantMessage
        (e.g. "claude-sonnet-4-5"), or None if not detected.

        Raises FatalAgentError for non-recoverable provider errors
        (rate limit, auth failure) so the caller can leave gracefully.
        """

        options = ClaudeAgentOptions(
            allowed_tools=self.allowed_tools,
            permission_mode="bypassPermissions",
            model=self.model if self.model not in ("claude", "claude-sonnet") else None,
            resume=self.session_id,
            max_turns=5,
            thinking=ThinkingConfigEnabled(type="enabled", budget_tokens=8000),
        )

        result_text = ""
        result_is_error = False
        actual_model: Optional[str] = None

        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage):
                    sid = getattr(message, "session_id", None)
                    if sid and self.session_id is None:
                        self.session_id = sid
                        self.log.info(f"Session established: {self.session_id}")

                elif isinstance(message, AssistantMessage):
                    if actual_model is None:
                        actual_model = getattr(message, "model", None) or None

                    # Send thinking stream to observers
                    blocks = []
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            blocks.append({"type": "thinking", "text": block.thinking})
                        elif isinstance(block, ToolUseBlock):
                            blocks.append({"type": "tool_use", "tool": block.name, "input": block.input})
                        elif isinstance(block, TextBlock):
                            blocks.append({"type": "text", "text": block.text})
                    if blocks:
                        await self._send_agent_thinking(blocks)

                elif isinstance(message, ResultMessage):
                    result_text = getattr(message, "result", "") or ""
                    result_is_error = bool(getattr(message, "is_error", False))

        except Exception as e:
            if result_text:
                self.log.debug(f"SDK post-result exception (ignored): {e}")
            else:
                self.log.error(f"SDK error: {e}")
                return f"(error: {e})", actual_model

        if result_is_error and result_text:
            raise FatalAgentError(result_text)

        return result_text.strip(), actual_model


# ── Entry point ────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(description="OpenParty Agent Bridge")
    parser.add_argument("--room", required=True, help="Room ID to join")
    parser.add_argument("--name", required=True, help="Display name in the room")
    parser.add_argument(
        "--model", default="claude", help="Model name shown to participants"
    )
    parser.add_argument(
        "--server", default="ws://localhost:8765", help="OpenParty server URL"
    )
    parser.add_argument(
        "--max-turns", type=int, default=10, help="Max turns before leaving"
    )
    parser.add_argument(
        "--tools",
        default="Read,Edit,Bash,Glob,Grep,WebSearch",
        help="Comma-separated list of allowed tools",
    )
    parser.add_argument(
        "--engine",
        default="claude",
        choices=["claude", "opencode"],
        help="Agent engine: 'claude' (claude_agent_sdk) or 'opencode' (opencode serve HTTP API)",
    )
    parser.add_argument(
        "--opencode-url",
        default=OPENCODE_URL,
        help="opencode serve base URL (default: http://localhost:4096)",
    )
    parser.add_argument(
        "--opencode-model",
        default="",
        help="Model ID for opencode engine, e.g. zen/mimo-v2-pro-free",
    )
    parser.add_argument(
        "--owner-name",
        default="",
        help="Room owner's display name; agents will follow all owner instructions unconditionally",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]

    bridge = AgentBridge(
        room_id=args.room,
        name=args.name,
        model=args.model,
        server_url=args.server,
        max_turns=args.max_turns,
        allowed_tools=tools,
        engine=args.engine,
        opencode_url=args.opencode_url,
        opencode_model=args.opencode_model,
        owner_name=args.owner_name,
    )

    try:
        await bridge.run()
    except KeyboardInterrupt:
        print(f"\n[{args.name}] Interrupted, exiting.")
    except Exception as e:
        logging.error(f"Bridge crashed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
