"""
OpenParty TUI — Textual-based reimplementation of observer_cli.py.

Usage:
    python openparty_tui.py --room my-room
    python openparty_tui.py --room my-room --owner --name Andy
"""

import asyncio
import json
import argparse
import re
from datetime import datetime

import aiohttp
import websockets
from rich.markup import escape as rich_escape
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, RichLog, Static



# ── Constants ──────────────────────────────────────────────────────────────────

OPENCODE_URL = "http://127.0.0.1:4096"
OPENPARTY_SERVER = "ws://localhost:8765"

COMMANDS = [
    ("/leave",     "離開房間"),
    ("/add-agent", "新增 AI agent 加入房間"),
    ("/kick",      "踢除房間成員"),
    ("/kick-all",  "踢除所有 AI agent"),
    ("/broadcast", "同時向所有 agent 發話，並行回答"),
]

COMMANDS_WITH_ARGS = {"/broadcast"}

AGENT_STYLES = ["cyan", "yellow", "green", "magenta", "blue"]

_agent_color_map: dict[str, str] = {}

# ── Style constants ────────────────────────────────────────────────────────────

OWNER_STYLE   = Style(color="black", bgcolor="white", bold=True)
OWNER_BODY    = Style(color="black", bgcolor="white")
DIM_STYLE     = Style(color="bright_black")
BOLD_STYLE    = Style(bold=True, color="white")
MAGENTA_STYLE = Style(color="magenta")
GREEN_STYLE   = Style(color="green")
RED_STYLE     = Style(color="red")
YELLOW_STYLE  = Style(color="yellow")
CYAN_STYLE    = Style(color="cyan")


# ── Helper functions ───────────────────────────────────────────────────────────

def _agent_style(name: str) -> Style:
    """Return a round-robin Rich Style for this agent name, memoized."""
    if name not in _agent_color_map:
        idx = len(_agent_color_map) % len(AGENT_STYLES)
        _agent_color_map[name] = AGENT_STYLES[idx]
    return Style(color=_agent_color_map[name])


def _model_label(model: str) -> str:
    """Return a concise, user-friendly model label from the raw model string."""
    if not model or model in ("human", "unknown", ""):
        return ""
    parts = model.split("/")
    return parts[-1]


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


_INLINE_RE = re.compile(r'(\*\*(.+?)\*\*|\*(.+?)\*|#[\w][\w\-]*|@[\w][\w\-]*)')


def _parse_to_rich(text: str, base_style: Style) -> Text:
    """Convert **bold**, *italic*, #name (cyan bold), @name (yellow bold) to Rich Text."""
    result = Text(style=base_style)
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            result.append(text[last:m.start()], style=base_style)
        full = m.group(0)
        if full.startswith("**"):
            inner = m.group(2)
            result.append(inner, style=Style(bold=True) + base_style)
        elif full.startswith("*"):
            inner = m.group(3)
            result.append(inner, style=Style(italic=True) + base_style)
        elif full.startswith("#"):
            result.append(full, style=Style(color="cyan", bold=True))
        else:  # @mention
            result.append(full, style=Style(color="yellow", bold=True))
        last = m.end()
    if last < len(text):
        result.append(text[last:], style=base_style)
    return result


# ── Custom Messages ────────────────────────────────────────────────────────────

class ServerMessage(Message):
    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data


# ── Widgets ────────────────────────────────────────────────────────────────────

class ChatLog(RichLog):
    """Scrollable chat log with page-up/down/end key bindings."""

    BINDINGS = [
        Binding("pageup",   "scroll_page_up",   "Page Up",   show=False),
        Binding("pagedown", "scroll_page_down",  "Page Down", show=False),
        Binding("end",      "scroll_to_end",     "End",       show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(auto_scroll=False, highlight=False, markup=False, wrap=True, **kwargs)

    def write_msg(self, content: "Text | str") -> None:
        at_bottom = self.scroll_y >= self.max_scroll_y
        self.write(content, scroll_end=at_bottom)

    def action_scroll_page_up(self) -> None:
        self.scroll_relative(y=-(self.size.height // 2), animate=False)

    def action_scroll_page_down(self) -> None:
        self.scroll_relative(y=(self.size.height // 2), animate=False)

    def action_scroll_to_end(self) -> None:
        self.scroll_end(animate=False)


class SepBar(Static):
    """Status bar showing owner name or observer read-only notice."""

    DEFAULT_CSS = """
    SepBar {
        height: 1;
        background: white;
        color: black;
        padding: 0;
    }
    """

    def __init__(self, owner: bool, name: str, **kwargs):
        super().__init__(**kwargs)
        self._owner = owner
        self._name = name

    def on_mount(self) -> None:
        if self._owner:
            self.update(f" [{self._name}] Type message + Enter to send. /leave to exit.")
        else:
            self.update(f" [Observer: {self._name}] Read-only mode.")


class CompletionList(Static):
    """Autocomplete popup that appears above the input bar."""

    DEFAULT_CSS = """
    CompletionList {
        height: auto;
        max-height: 18;
        background: $panel;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.items: list[tuple[str, str]] = []
        self.selected_idx: int = 0
        self.completing_type: str = "command"
        self.styles.display = "none"  # hidden until show_items() is called

    def show_items(self, items: list[tuple[str, str]], completing_type: str) -> None:
        self.items = items
        self.selected_idx = 0
        self.completing_type = completing_type
        self.styles.display = "block"
        self._refresh()

    def hide(self) -> None:
        self.items = []
        self.styles.display = "none"

    def move_up(self) -> None:
        if self.items:
            self.selected_idx = max(0, self.selected_idx - 1)
            self._refresh()

    def move_down(self) -> None:
        if self.items:
            self.selected_idx = min(len(self.items) - 1, self.selected_idx + 1)
            self._refresh()

    def get_selected(self) -> "str | None":
        if not self.items:
            return None
        return self.items[self.selected_idx][0]

    def _refresh(self) -> None:
        if not self.items:
            self.update("")
            return
        lines = []
        for i, (value, desc) in enumerate(self.items):
            escaped_value = rich_escape(value)
            escaped_desc = rich_escape(desc)
            if i == self.selected_idx:
                if self.completing_type == "mention":
                    lines.append(f"[bold cyan]▶ {escaped_value}[/]")
                else:
                    lines.append(f"[bold cyan]▶  {escaped_value:<22} [dim]{escaped_desc}[/dim][/]")
            else:
                if self.completing_type == "mention":
                    lines.append(f"[dim]{escaped_value}[/dim]")
                else:
                    lines.append(f"[dim]   {escaped_value:<22} {escaped_desc}[/dim]")
        self.update("\n".join(lines))


class MessageInput(Input):
    """Text input that delegates Tab/Up/Down/Escape to the completion list."""

    async def action_submit(self) -> None:  # type: ignore[override]
        app: OpenPartyApp = self.app  # type: ignore[assignment]
        if getattr(app, "_completing", False):
            app._completion_enter()
        else:
            await super().action_submit()

    def on_key(self, event: events.Key) -> None:
        app: OpenPartyApp = self.app  # type: ignore[assignment]
        if not getattr(app, "_completing", False):
            return
        if event.key == "up":
            app.query_one(CompletionList).move_up()
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            app.query_one(CompletionList).move_down()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            app.query_one(CompletionList).hide()
            app._completing = False  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()
        elif event.key == "tab":
            app._completion_tab()  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()


# ── Modal Screens ──────────────────────────────────────────────────────────────

class _PickerScreen(ModalScreen):
    """Base class for modal picker dialogs."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel", show=False),
        Binding("up",     "move_up",      "Up",     show=False),
        Binding("down",   "move_down",    "Down",   show=False),
        Binding("enter",  "pick",         "Select", show=False),
    ]

    DEFAULT_CSS = """
    _PickerScreen {
        align: center middle;
    }

    #picker-box {
        width: 72;
        max-height: 28;
        background: $panel;
        border: solid $accent;
    }

    #picker-title {
        background: $accent;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #picker-search {
        height: 3;
        border: tall $panel-lighten-1;
        background: $panel-darken-1;
    }

    #picker-list {
        height: auto;
        max-height: 18;
    }
    """

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_move_up(self) -> None:
        self.query_one("#picker-list", ListView).action_cursor_up()

    def action_move_down(self) -> None:
        self.query_one("#picker-list", ListView).action_cursor_down()

    def on_list_view_selected(self, _event: ListView.Selected) -> None:  # type: ignore[override]  # pyright: ignore[reportUnusedParameter]
        self._do_pick()

    def action_pick(self) -> None:
        self._do_pick()

    def _do_pick(self):
        raise NotImplementedError


class ModelPickerScreen(_PickerScreen):
    """Modal for choosing a model when spawning a new agent."""

    def __init__(self, items: list[dict], **kwargs):
        super().__init__(**kwargs)
        self.all_items: list[dict] = items
        self.filtered: list[dict] = list(items)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(id="picker-title")
            yield Input(placeholder="/ 搜尋...", id="picker-search")
            yield ListView(id="picker-list")

    def on_mount(self) -> None:
        self._refresh_list()
        self.query_one("#picker-search", Input).focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:  # type: ignore[override, reportUnusedParameter]
        self._do_pick()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "picker-search":
            query = event.value.lower()
            if query:
                self.filtered = [
                    item for item in self.all_items
                    if query in item.get("display", "").lower()
                ]
            else:
                self.filtered = list(self.all_items)
            self._refresh_list()

    def _refresh_list(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        lv.clear()
        for item in self.filtered:
            lv.append(ListItem(Label(rich_escape(item["display"]))))
        title = self.query_one("#picker-title", Static)
        title.update(f" 選擇 Agent [{len(self.filtered)}/{len(self.all_items)}]  ↑↓ Enter Esc ")

    def _do_pick(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        idx = lv.index if lv.index is not None else 0  # default to first item
        if self.filtered and 0 <= idx < len(self.filtered):
            self.dismiss(self.filtered[idx])
        else:
            self.dismiss(None)


class KickPickerScreen(_PickerScreen):
    """Modal for choosing an agent to kick."""

    def __init__(self, agents: list[dict], **kwargs):
        super().__init__(**kwargs)
        self.agents = agents

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(" 選擇踢除成員 (↑↓ Enter Esc) ", id="picker-title")
            yield ListView(id="picker-list")

    def on_mount(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        for a in self.agents:
            lv.append(ListItem(Label(f"{a['name']}  ({a.get('model', '?')})")))
        lv.focus()

    def _do_pick(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        idx = lv.index
        if idx is not None and 0 <= idx < len(self.agents):
            self.dismiss(self.agents[idx])
        else:
            self.dismiss(None)


# ── Main Application ───────────────────────────────────────────────────────────

class OpenPartyApp(App):
    CSS_PATH = "openparty.tcss"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        room_id: str,
        server_url: str,
        name: str,
        owner: bool,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.room_id = room_id
        self.server_url = server_url
        self.owner_name = name  # `name` is a read-only property on App
        self.owner = owner
        self.ws = None
        self.agents: list[dict] = []
        self.available_engines: list[str] = []

        # Completion state
        self._completing: bool = False
        self._completing_type: str = "command"
        self._completion_items: list[tuple[str, str]] = []

    @property
    def display_name(self) -> str:
        return f"[owner] {self.owner_name}" if self.owner else self.owner_name

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat")
        yield CompletionList()

        yield SepBar(self.owner, self.display_name)
        if self.owner:
            yield MessageInput(placeholder="> ", id="input")

    def on_mount(self) -> None:
        if self.owner:
            self.query_one("#input", MessageInput).focus()
        asyncio.create_task(self._run_ws())

    # ── WebSocket connection ───────────────────────────────────────────────────

    async def _run_ws(self) -> None:
        try:
            async with websockets.connect(self.server_url) as ws:
                self.ws = ws
                await ws.send(json.dumps({
                    "type": "join",
                    "role": "observer",
                    "room_id": self.room_id,
                    "name": self.display_name,
                    "owner": self.owner,
                }))
                async for raw in ws:
                    self.post_message(ServerMessage(json.loads(raw)))
        except ConnectionRefusedError:
            self._chat(
                Text(
                    f"[error] Cannot connect to {self.server_url}. Is the server running?",
                    style=RED_STYLE,
                )
            )
        except websockets.exceptions.ConnectionClosed:
            self._chat(Text("[Observer] Connection closed.", style=RED_STYLE))
        except Exception as exc:
            self._chat(Text(f"[error] {exc}", style=RED_STYLE))
        finally:
            self.ws = None

    # ── Chat helpers ───────────────────────────────────────────────────────────

    def _chat(self, content: "Text | str") -> None:
        self.query_one("#chat", ChatLog).write_msg(content)

    def _print_message(self, entry: dict) -> None:
        name = entry.get("name", "?")
        model = entry.get("model", "")
        content = entry.get("content", "")
        is_private = entry.get("is_private", False)
        private_to = entry.get("private_to", [])
        is_owner_msg = name.startswith("[owner]")

        self._chat(Text(""))

        model_label = _model_label(model)
        header_str = f"  {name}  ({model_label})" if model_label else f"  {name}"

        if is_private:
            if private_to:
                whisper_tag = f" 【私訊 → {', '.join(private_to)}】"
            else:
                whisper_tag = " 【私訊】"
            header_text = Text(header_str + whisper_tag, style=Style(color="magenta", bold=True))
            self._chat(header_text)
            for line in content.split("\n"):
                self._chat(_parse_to_rich(f"    {line}", MAGENTA_STYLE))
        elif is_owner_msg:
            self._chat(Text(header_str, style=OWNER_STYLE))
            for line in content.split("\n"):
                self._chat(_parse_to_rich(f"    {line}", OWNER_BODY))
        else:
            agent_st = _agent_style(name)
            header_text = Text(header_str, style=Style.combine([agent_st, Style(bold=True)]))
            self._chat(header_text)
            for line in content.split("\n"):
                self._chat(_parse_to_rich(f"    {line}", DIM_STYLE))

    # ── Server message handler ─────────────────────────────────────────────────

    def on_server_message(self, event: ServerMessage) -> None:
        msg = event.data
        t = msg.get("type")

        if t == "joined":
            self.available_engines = msg.get("available_engines", [])
            state = msg.get("room_state", {})
            topic = state.get("topic", "(waiting for owner to set topic)")
            participants = state.get("participants", [])
            self.agents = list(participants)

            self._chat(Text("=" * 60, style=BOLD_STYLE))
            self._chat(Text(f"  OpenParty — Room: {self.room_id}", style=BOLD_STYLE))
            self._chat(Text(f"  Topic: {topic}", style=DIM_STYLE))
            if participants:
                self._chat(Text(f"  Agents: {', '.join(p['name'] for p in participants)}", style=DIM_STYLE))
            else:
                self._chat(Text("  Agents: (waiting...)", style=DIM_STYLE))
            self._chat(Text("=" * 60, style=BOLD_STYLE))
            if self.owner:
                self._chat(Text("  You are the room owner. Send a message to set the topic and start.", style=GREEN_STYLE))
            history = msg.get("history", [])
            if history:
                self._chat(Text(f"  [replaying {len(history)} messages]", style=DIM_STYLE))
                for entry in history:
                    self._print_message(entry)

        elif t == "agent_joined":
            agent_st = _agent_style(msg["name"])
            self._chat(Text(f"{now()}  ++ {msg['name']} ({msg['model']}) joined", style=agent_st))
            self.agents.append({
                "agent_id": msg.get("agent_id", msg["name"]),
                "name": msg["name"],
                "model": msg.get("model", ""),
            })

        elif t == "agent_left":
            self._chat(Text(
                f"{now()}  -- {msg['name']} left  ({msg.get('agents_remaining', '?')} remaining)",
                style=RED_STYLE,
            ))
            self.agents = [a for a in self.agents if a["name"] != msg["name"]]

        elif t == "model_updated":
            agent_name = msg.get("name", "")
            new_model = msg.get("model", "")
            for a in self.agents:
                if a["name"] == agent_name:
                    a["model"] = new_model
                    break
            label = _model_label(new_model)
            self._chat(Text(f"{now()}  [model] {agent_name} → {label}", style=DIM_STYLE))

        elif t == "system_message":
            self._chat(Text(f"{now()}  *** {msg.get('text', '')}", style=YELLOW_STYLE))

        elif t == "waiting_for_owner":
            self._chat(Text(f"{now()}  [server] {msg.get('message', '')}", style=DIM_STYLE))

        elif t == "turn_start":
            agent_st = _agent_style(msg["name"])
            self._chat(Text(f"{now()}  » {msg['name']} is thinking...", style=agent_st))

        elif t == "turn_end":
            latency = msg.get("latency_ms", 0)
            self._chat(Text(f"  ({latency}ms)", style=DIM_STYLE))

        elif t == "message":
            self._print_message(msg)

        elif t == "spawn_result":
            name = msg.get("name", "?")
            model = msg.get("model", "?")
            if msg.get("success"):
                self._chat(Text(f"{now()}  [server] 已啟動 {name} ({model})", style=GREEN_STYLE))
            else:
                self._chat(Text(f"{now()}  [server] 啟動 {name} 失敗", style=RED_STYLE))

        elif t == "room_state":
            pass  # silently ignore

        else:
            self._chat(Text(f"{now()}  [{t}] {json.dumps(msg)[:80]}", style=DIM_STYLE))

    # ── Input events ───────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "input" and self.owner:
            self._update_completion(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "input" or not self.owner:
            return
        text = event.value.strip()
        inp = self.query_one("#input", MessageInput)
        inp.value = ""
        cl = self.query_one(CompletionList)
        cl.hide()
        self._completing = False
        if text:
            asyncio.create_task(self._handle_command(text))

    # ── Completion logic ───────────────────────────────────────────────────────

    def _update_completion(self, buf: str) -> None:
        cl = self.query_one(CompletionList)

        # @/# mention completion
        at_match = re.search(r"([#@])([\w\-]*)$", buf)
        if at_match:
            sigil = at_match.group(1)
            partial = at_match.group(2).lower()
            matches = [
                (f"{sigil}{a['name']}", a.get("model", ""))
                for a in self.agents
                if a["name"].lower().startswith(partial)
            ]
            if matches:
                self._completing = True
                self._completing_type = "mention"
                self._completion_items = matches
                cl.show_items(matches, "mention")
                return

        # /command completion
        if buf.startswith("/"):
            matches = [(cmd, desc) for cmd, desc in COMMANDS if cmd.startswith(buf)]
            if matches and buf != matches[0][0]:
                self._completing = True
                self._completing_type = "command"
                self._completion_items = matches
                cl.show_items(matches, "command")
                return

        self._completing = False
        cl.hide()

    def _completion_enter(self) -> None:
        cl = self.query_one(CompletionList)
        selected = cl.get_selected()
        if selected is None:
            return
        inp = self.query_one("#input", MessageInput)
        cl.hide()
        self._completing = False

        if self._completing_type == "mention":
            inp.value = re.sub(r"[#@][\w\-]*$", selected + " ", inp.value)
            inp.cursor_position = len(inp.value)
        else:
            # Command
            if selected in COMMANDS_WITH_ARGS:
                inp.value = selected + " "
                inp.cursor_position = len(inp.value)
            else:
                inp.value = ""
                asyncio.create_task(self._handle_command(selected.strip()))

    def _completion_tab(self) -> None:
        cl = self.query_one(CompletionList)
        selected = cl.get_selected()
        if selected is None:
            return
        inp = self.query_one("#input", MessageInput)
        cl.hide()
        self._completing = False

        if self._completing_type == "mention":
            inp.value = re.sub(r"[#@][\w\-]*$", selected + " ", inp.value)
            inp.cursor_position = len(inp.value)
        else:
            # Always fill (never execute on tab)
            new_val = selected if selected not in COMMANDS_WITH_ARGS else selected + " "
            inp.value = new_val + " " if not new_val.endswith(" ") else new_val
            inp.cursor_position = len(inp.value)

    # ── Command handler ────────────────────────────────────────────────────────

    async def _handle_command(self, text: str) -> None:
        if not text:
            return

        if text == "/leave":
            if self.ws is not None:
                await self.ws.close()
            self.exit()
            return

        if text == "/add-agent":
            self._chat(Text(f"{now()}  [system] 正在讀取可用模型...", style=DIM_STYLE))
            models = await _fetch_models(self.available_engines)
            if not models:
                self._chat(Text(
                    f"{now()}  [system] 沒有可用的 engine（server 未偵測到 opencode 或 claude）",
                    style=RED_STYLE,
                ))
                return
            chosen = await self.push_screen_wait(ModelPickerScreen(models))
            if chosen is None:
                return
            base = chosen.get("base_name", chosen["full_id"].split("/")[-1])[:12]
            existing_names = {a["name"] for a in self.agents}
            if base not in existing_names:
                agent_name = base
            else:
                n = 2
                while f"{base}-{n}" in existing_names:
                    n += 1
                agent_name = f"{base}-{n}"
            self._chat(Text(
                f"{now()}  [spawning] {chosen['display']} as '{agent_name}'...",
                style=GREEN_STYLE,
            ))
            if self.ws is not None:
                await self.ws.send(json.dumps({
                    "type": "spawn_agent",
                    "name": agent_name,
                    "model": chosen["full_id"],
                    "engine": chosen.get("engine", "opencode"),
                }))
            return

        if text == "/kick-all":
            if not self.agents:
                self._chat(Text(f"{now()}  [system] 目前沒有成員可踢除", style=YELLOW_STYLE))
            else:
                if self.ws is not None:
                    for agent in list(self.agents):
                        await self.ws.send(json.dumps({
                            "type": "kick_agent",
                            "agent_name": agent["name"],
                        }))
            return

        if text == "/kick":
            if not self.agents:
                self._chat(Text(f"{now()}  [system] 目前沒有成員可踢除", style=YELLOW_STYLE))
                return
            kick_agents = [
                {"display": f"{a['name']}  ({a.get('model', '?')})", "agent_name": a["name"]}
                for a in self.agents
            ]
            chosen = await self.push_screen_wait(KickPickerScreen(kick_agents))
            if chosen is None:
                return
            if self.ws is not None:
                await self.ws.send(json.dumps({
                    "type": "kick_agent",
                    "agent_name": chosen["agent_name"],
                }))
            return

        if text.startswith("/broadcast"):
            content = text[len("/broadcast"):].strip()
            if not content:
                self._chat(Text(f"{now()}  [system] 用法：/broadcast <訊息>", style=YELLOW_STYLE))
            elif not self.agents:
                self._chat(Text(
                    f"{now()}  [system] 沒有 agent 可以廣播，請先 /add-agent",
                    style=YELLOW_STYLE,
                ))
            else:
                if self.ws is not None:
                    await self.ws.send(json.dumps({"type": "broadcast", "content": content}))
            return

        # Normal message
        if self.ws is not None:
            await self.ws.send(json.dumps({"type": "message", "content": text}))


# ── Model fetching (top-level async) ──────────────────────────────────────────

async def _fetch_models(available_engines: list[str]) -> list[dict]:
    """Build agent list based on what the server has available."""
    result = []

    # OpenCode models (from opencode serve HTTP API)
    if "opencode" in available_engines:
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"{OPENCODE_URL}/provider",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as r:
                    data = await r.json() if r.status == 200 else {}
            connected = set(data.get("connected", []))
            for provider in data.get("all", []):
                pid = provider.get("id", "")
                if pid not in connected:
                    continue
                models = provider.get("models", {})
                items = models.values() if isinstance(models, dict) else models
                for m in items:
                    mid = m.get("id", "")
                    mname = m.get("name", mid)
                    base_name = mid.split("/")[-1].split(":")[0] if mid else "agent"
                    result.append({
                        "display": f"{pid} - {mname}",
                        "full_id": f"{pid}/{mid}",
                        "engine": "opencode",
                        "base_name": base_name[:12],
                    })
        except Exception:
            pass

    # Claude engine (claude_agent_sdk bundled binary)
    if "claude" in available_engines:
        for model_id, label, base in [
            ("claude-opus-4-6",   "Opus 4.6  · Most capable",      "claude-opus"),
            ("claude-sonnet-4-6", "Sonnet 4.6 · Best for everyday", "claude-sonnet"),
            ("claude-haiku-4-5",  "Haiku 4.5  · Fastest",           "claude-haiku"),
        ]:
            result.append({
                "display": f"claude - {label}",
                "full_id": f"claude/{model_id}",
                "engine": "claude",
                "base_name": base,
            })

    return result


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="OpenParty TUI — Textual-based room client")
    parser.add_argument("--room",   default="debate-001",       help="Room ID to join")
    parser.add_argument("--server", default=OPENPARTY_SERVER,   help="Server WebSocket URL")
    parser.add_argument("--name",   default="Human",            help="Your display name")
    parser.add_argument("--owner",  action="store_true",        help="Join as room owner (can speak)")
    args = parser.parse_args()

    app = OpenPartyApp(
        room_id=args.room,
        server_url=args.server,
        name=args.name,
        owner=args.owner,
    )
    app.run()


if __name__ == "__main__":
    main()
