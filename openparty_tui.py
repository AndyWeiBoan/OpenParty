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
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, RichLog, Static, TextArea
from textual.timer import Timer


# ── Constants ──────────────────────────────────────────────────────────────────

OPENCODE_URL = "http://127.0.0.1:4096"
OPENPARTY_SERVER = "ws://localhost:8765"

COMMANDS = [
    ("/leave", "離開房間"),
    ("/add-agent", "新增 AI agent 加入房間"),
    ("/kick", "踢除房間成員"),
    ("/kick-all", "踢除所有 AI agent"),
    ("/broadcast", "同時向所有 agent 發話，並行回答"),
]

COMMANDS_WITH_ARGS = {"/broadcast"}

AGENT_STYLES = ["cyan", "yellow", "green", "magenta", "blue"]

_agent_color_map: dict[str, str] = {}

# ── Style constants ────────────────────────────────────────────────────────────

OWNER_STYLE = Style(color="black", bgcolor="white", bold=True)
OWNER_BODY = Style(color="black", bgcolor="white")
DIM_STYLE = Style(color="gray70")
BOLD_STYLE = Style(bold=True, color="white")
MAGENTA_STYLE = Style(color="magenta")
GREEN_STYLE = Style(color="green")
RED_STYLE = Style(color="red")
YELLOW_STYLE = Style(color="yellow")
CYAN_STYLE = Style(color="cyan")


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
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


_INLINE_RE = re.compile(r"(\*\*(.+?)\*\*|\*(.+?)\*|#[\w][\w\-]*|@[\w][\w\-]*)")


def _parse_to_rich(text: str, base_style: Style) -> Text:
    """Convert **bold**, *italic*, #name (cyan bold), @name (yellow bold) to Rich Text."""
    result = Text(style=base_style)
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            result.append(text[last : m.start()], style=base_style)
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
        Binding("pageup", "scroll_page_up", "Page Up", show=False),
        Binding("pagedown", "scroll_page_down", "Page Down", show=False),
        Binding("end", "scroll_to_end", "End", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(
            auto_scroll=False, highlight=False, markup=False, wrap=True, **kwargs
        )

    def write_msg(self, content: "Text | str") -> None:
        at_bottom = self.scroll_y >= self.max_scroll_y
        self.write(content, scroll_end=at_bottom)

    def action_scroll_page_up(self) -> None:
        self.scroll_relative(y=-(self.size.height // 2), animate=False)

    def action_scroll_page_down(self) -> None:
        self.scroll_relative(y=(self.size.height // 2), animate=False)

    def action_scroll_to_end(self) -> None:
        self.scroll_end(animate=False)


class RoomHeader(Static):
    """Sticky header pinned at top showing room info and per-agent status."""

    DEFAULT_CSS = """
    RoomHeader {
        height: auto;
        width: 100%;
        background: #0a1a0a;
        color: #ffaa00;
        border-bottom: solid #ffaa00;
        padding: 0 1;
        text-style: bold;
    }
    """

    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._room_id: str = ""
        self._topic: str = ""
        self._agents: list = []
        self._agent_status: dict[str, str] = {}  # agent_name → status summary
        self._thinking: set[str] = set()
        self._frame: int = 0
        self._timer: Timer | None = None

    def update_info(
        self,
        room_id: str,
        topic: str,
        agents: list,
    ) -> None:
        self._room_id = room_id
        self._topic = topic
        self._agents = agents
        self._refresh_display()

    def start_thinking(self, agent_name: str, summary: str = "") -> None:
        """Mark agent as thinking and start spinner."""
        self._thinking.add(agent_name)
        self._agent_status[agent_name] = summary or "thinking..."
        if not self._timer:
            self._timer = self.set_interval(0.1, self._tick)
        self._refresh_display()

    def stop_thinking(self, agent_name: str) -> None:
        """Mark agent as standby and stop spinner if no agents are thinking."""
        self._thinking.discard(agent_name)
        self._agent_status.pop(agent_name, None)
        if not self._thinking and self._timer:
            self._timer.stop()
            self._timer = None
        self._refresh_display()

    def update_block(self, agent_name: str, blocks: list[dict]) -> None:
        """Update per-agent status summary from latest block in agent_thinking event."""
        for block in reversed(blocks):
            btype = block.get("type", "")
            if btype == "thinking":
                self._agent_status[agent_name] = "thinking..."
                break
            elif btype == "tool_use":
                tool = block.get("tool", "?")
                inp = block.get("input", {})
                first_val = str(next(iter(inp.values()), "")) if inp else ""
                self._agent_status[agent_name] = f"{tool}({first_val[:18]})"
                break
            elif btype == "text":
                # Text block means the agent is producing its final response
                self._agent_status[agent_name] = "responding..."
                break
            else:
                # Unknown block type — use generic label rather than leaving stale status
                self._agent_status[agent_name] = "thinking..."
                break
        self._refresh_display()

    def _tick(self) -> None:
        self._frame += 1
        self._refresh_display()

    def _refresh_display(self) -> None:
        spin = self.SPINNER[self._frame % len(self.SPINNER)]
        lines = [f"OpenParty — Room: {self._room_id}   Topic: {self._topic}", "Agents:"]
        if self._agents:
            for a in self._agents:
                name = a["name"]
                if name in self._thinking:
                    summary = self._agent_status.get(name, "") or "thinking..."
                    lines.append(f"   {spin} {name}: {summary}")
                else:
                    lines.append(f"   ● {name}: standby")
        else:
            lines.append("   (waiting...)")
        self.update("\n".join(lines))


class AgentSidebar(Static):
    """Right-side panel showing identity and per-agent thinking state.

    Styled like OpenCode's right sidebar: fixed width, dark background,
    always visible. Shows owner/observer identity at top, then live
    thinking status for each active agent below.
    """

    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    DEFAULT_CSS = """
    AgentSidebar {
        width: 28;
        height: 100%;
        background: #111111;
        color: #aaaaaa;
        border-left: solid #333333;
        padding: 1 1;
    }
    """

    def __init__(self, owner: bool, name: str, **kwargs):
        super().__init__(**kwargs)
        self._owner = owner
        self._name = name
        self._frame = 0
        self._thinking_agents: dict[str, str] = {}  # name → last block summary
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self._refresh_display()

    def start_thinking(self, agent_name: str, summary: str = "") -> None:
        self._thinking_agents[agent_name] = summary
        if not self._timer:
            self._timer = self.set_interval(0.1, self._tick)

    def stop_thinking(self, agent_name: str) -> None:
        self._thinking_agents.pop(agent_name, None)
        if not self._thinking_agents and self._timer:
            self._timer.stop()
            self._timer = None
        self._refresh_display()

    def _tick(self) -> None:
        self._frame += 1
        self._refresh_display()

    def _refresh_display(self) -> None:
        lines: list[str] = []
        # Identity header
        if self._owner:
            lines.append(f"[bold white]{self._name}[/bold white]")
            lines.append("[dim]owner[/dim]")
        else:
            lines.append(f"[bold white]{self._name}[/bold white]")
            lines.append("[dim]observer[/dim]")
        lines.append("")
        # Thinking agents
        if self._thinking_agents:
            lines.append("[dim]thinking[/dim]")
            spin = self.SPINNER_FRAMES[self._frame % len(self.SPINNER_FRAMES)]
            for agent, summary in self._thinking_agents.items():
                if summary:
                    lines.append(f"{spin} [yellow]{agent}[/yellow]")
                    lines.append(f"  [dim]{summary[:22]}[/dim]")
                else:
                    lines.append(f"{spin} [yellow]{agent}[/yellow]")
        else:
            lines.append("[dim]idle[/dim]")
        self.update("\n".join(lines))


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
                    lines.append(
                        f"[bold cyan]▶  {escaped_value:<22} [dim]{escaped_desc}[/dim][/]"
                    )
            else:
                if self.completing_type == "mention":
                    lines.append(f"[dim]{escaped_value}[/dim]")
                else:
                    lines.append(f"[dim]   {escaped_value:<22} {escaped_desc}[/dim]")
        self.update("\n".join(lines))


class StatusBar(Static):
    """One-line status bar showing round status (idle / thinking)."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: #ffaa00;
        color: #0a1a0a;
    }
    """

    def __init__(self, owner: bool, display_name: str, **kwargs):
        super().__init__("", **kwargs)
        self._display_name = display_name
        self._round: int = 0
        self._is_thinking: bool = False

    def on_mount(self) -> None:
        self._refresh_display()

    def set_round(self, round_num: int) -> None:
        """Update the current round number."""
        self._round = round_num
        self._refresh_display()

    def set_thinking(self, thinking: bool) -> None:
        """Switch between thinking and idle state."""
        self._is_thinking = thinking
        if thinking:
            self.styles.background = "#0a1a0a"
            self.styles.color = "#ffaa00"
        else:
            self.styles.background = "#ffaa00"
            self.styles.color = "#0a1a0a"
        self._refresh_display()

    def _refresh_display(self) -> None:
        round_str = f"Round {self._round}" if self._round > 0 else "idle"
        state = "thinking" if self._is_thinking else "idle"
        self.update(f" {round_str} / {state}  — {self._display_name}")


class MessageInput(TextArea):
    """Multi-line text input: Shift+Enter inserts newline, Enter submits."""

    class Submitted(Message):
        """Emitted when the user presses Enter (without Shift) to submit."""

        def __init__(self, input: "MessageInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    # Expose a `.value` property so call-sites that used Input.value still work.
    @property
    def value(self) -> str:  # type: ignore[override]
        return self.text

    @value.setter
    def value(self, new: str) -> None:
        self.clear()
        self.insert(new)

    # Mimic Input.cursor_position setter (move cursor to a character offset).
    @property
    def cursor_position(self) -> int:  # type: ignore[override]
        row, col = self.cursor_location
        # approximate: count chars up to row,col
        lines = self.text.split("\n")
        pos = sum(len(lines[i]) + 1 for i in range(row)) + col
        return pos

    @cursor_position.setter
    def cursor_position(self, pos: int) -> None:
        text = self.text
        row = 0
        col = 0
        count = 0
        for i, ch in enumerate(text):
            if count >= pos:
                break
            if ch == "\n":
                row += 1
                col = 0
            else:
                col += 1
            count += 1
        self.move_cursor((row, col))

    def on_key(self, event: events.Key) -> None:
        app: OpenPartyApp = self.app  # type: ignore[assignment]

        # ── Shift+Enter → newline (let TextArea handle it naturally) ─────
        if event.key == "shift+enter":
            return  # don't intercept; TextArea inserts newline

        # ── Enter (no Shift) → submit ─────────────────────────────────────
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if getattr(app, "_completing", False):
                app._completion_enter()
            else:
                text = self.text
                self.post_message(self.Submitted(self, text))
            return

        # ── Completion navigation ─────────────────────────────────────────
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
        Binding("up", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("enter", "pick", "Select", show=False),
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
                    item
                    for item in self.all_items
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
        title.update(
            f" 選擇 Agent [{len(self.filtered)}/{len(self.all_items)}]  ↑↓ Enter Esc "
        )

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
        self._thinking: set[str] = set()
        self._turn_complete: set[str] = (
            set()
        )  # agents whose turn has ended; guard for late agent_thinking
        self._topic: str = ""
        self._last_round: int = 0  # track round changes for divider rendering

        # Completion state
        self._completing: bool = False
        self._completing_type: str = "command"
        self._completion_items: list[tuple[str, str]] = []

    @property
    def display_name(self) -> str:
        return f"[owner] {self.owner_name}" if self.owner else self.owner_name

    def compose(self) -> ComposeResult:
        yield RoomHeader(id="room-header")
        yield ChatLog(id="chat")
        yield CompletionList()
        yield StatusBar(self.owner, self.display_name, id="round-status-bar")
        if self.owner:
            yield MessageInput(id="input")

    def on_mount(self) -> None:
        if self.owner:
            self.query_one("#input", MessageInput).focus()
        asyncio.create_task(self._run_ws())

    def _refresh_header(self) -> None:
        """Update the room header with current agent list and topic."""
        self.query_one("#room-header", RoomHeader).update_info(
            self.room_id,
            self._topic,
            self.agents,
        )

    # ── WebSocket connection ───────────────────────────────────────────────────

    async def _run_ws(self) -> None:
        try:
            async with websockets.connect(self.server_url) as ws:
                self.ws = ws
                await ws.send(
                    json.dumps(
                        {
                            "type": "join",
                            "role": "observer",
                            "room_id": self.room_id,
                            "name": self.display_name,
                            "owner": self.owner,
                        }
                    )
                )
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

        # Insert round divider when round number changes
        msg_round = entry.get("round", 0)
        if msg_round > self._last_round:
            width = self.size.width or 80
            round_label = f" Round {msg_round} "
            side = max(2, (width - len(round_label)) // 2)
            divider = "─" * side + round_label + "─" * side
            if self._last_round > 0:
                self._chat(Text(divider, style=Style(color="#45505A", bold=True)))
            self._last_round = msg_round

        self._chat(Text(""))

        model_label = _model_label(model)
        ts = now()
        header_str = (
            f"  {ts}  {name}  ({model_label})" if model_label else f"  {ts}  {name}"
        )

        if is_private:
            if private_to:
                whisper_tag = f" 【私訊 → {', '.join(private_to)}】"
            else:
                whisper_tag = " 【私訊】"
            header_text = Text(
                header_str + whisper_tag, style=Style(color="magenta", bold=True)
            )
            self._chat(header_text)
            for line in content.split("\n"):
                self._chat(_parse_to_rich(f"    {line}", MAGENTA_STYLE))
        elif is_owner_msg:
            self._chat(Text(header_str, style=OWNER_STYLE))
            for line in content.split("\n"):
                self._chat(_parse_to_rich(f"    {line}", OWNER_BODY))
        else:
            agent_st = _agent_style(name)
            header_text = Text(
                header_str, style=Style.combine([agent_st, Style(bold=True)])
            )
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
            self._topic = topic
            self._refresh_header()
            if self.owner:
                self._chat(
                    Text(
                        "  You are the room owner. Send a message to set the topic and start.",
                        style=GREEN_STYLE,
                    )
                )
            history = msg.get("history", [])
            if history:
                self._chat(
                    Text(f"  [replaying {len(history)} messages]", style=DIM_STYLE)
                )
                for entry in history:
                    self._print_message(entry)

        elif t == "agent_joined":
            agent_st = _agent_style(msg["name"])
            self._chat(
                Text(
                    f"{now()}  ++ {msg['name']} ({msg['model']}) joined", style=agent_st
                )
            )
            self.agents.append(
                {
                    "agent_id": msg.get("agent_id", msg["name"]),
                    "name": msg["name"],
                    "model": msg.get("model", ""),
                    "engine": msg.get("engine", ""),
                }
            )
            self._topic = msg.get("topic", "") or self._topic
            self._refresh_header()

        elif t == "agent_left":
            self._chat(
                Text(
                    f"{now()}  -- {msg['name']} left  ({msg.get('agents_remaining', '?')} remaining)",
                    style=RED_STYLE,
                )
            )
            self.agents = [a for a in self.agents if a["name"] != msg["name"]]
            self._thinking.discard(msg["name"])
            self._refresh_header()

        elif t == "model_updated":
            agent_name = msg.get("name", "")
            new_model = msg.get("model", "")
            for a in self.agents:
                if a["name"] == agent_name:
                    a["model"] = new_model
                    break
            label = _model_label(new_model)
            self._chat(
                Text(f"{now()}  [model] {agent_name} → {label}", style=DIM_STYLE)
            )

        elif t == "system_message":
            self._chat(Text(f"{now()}  *** {msg.get('text', '')}", style=YELLOW_STYLE))

        elif t == "waiting_for_owner":
            self._chat(
                Text(f"{now()}  [server] {msg.get('message', '')}", style=DIM_STYLE)
            )

        elif t == "turn_start":
            agent_name = msg["name"]
            self._thinking.add(agent_name)
            self._turn_complete.discard(agent_name)  # reset guard for this agent
            header = self.query_one("#room-header", RoomHeader)
            # start_thinking() calls _refresh_display() internally; no need for
            # a separate update_info() call here (which would double-render).
            header.start_thinking(agent_name)
            self.query_one("#round-status-bar", StatusBar).set_thinking(True)

        elif t == "turn_end":
            latency = msg.get("latency_ms", 0)
            self._chat(Text(f"  ({latency}ms)", style=DIM_STYLE))
            agent_name = msg.get("name", "")
            if agent_name:
                self._thinking.discard(agent_name)
                self._turn_complete.add(agent_name)
                self.query_one("#room-header", RoomHeader).stop_thinking(agent_name)
            if not self._thinking:
                self.query_one("#round-status-bar", StatusBar).set_thinking(False)

        elif t == "agent_thinking":
            agent_name = msg.get("name", "")
            # Guard: if turn already ended for this agent, discard for UI
            if agent_name in self._turn_complete:
                return
            blocks = msg.get("blocks", [])
            self.query_one("#room-header", RoomHeader).update_block(agent_name, blocks)

        elif t == "message":
            self._print_message(msg)

        elif t == "spawn_result":
            name = msg.get("name", "?")
            model = msg.get("model", "?")
            if msg.get("success"):
                self._chat(
                    Text(
                        f"{now()}  [server] 已啟動 {name} ({model})", style=GREEN_STYLE
                    )
                )
            else:
                self._chat(Text(f"{now()}  [server] 啟動 {name} 失敗", style=RED_STYLE))

        elif t == "room_state":
            pass  # silently ignore

        else:
            self._chat(Text(f"{now()}  [{t}] {json.dumps(msg)[:80]}", style=DIM_STYLE))

    # ── Input events ───────────────────────────────────────────────────────────

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "input" and self.owner:
            # For completion, use only the last line (where the cursor is)
            buf = event.text_area.text
            last_line = buf.split("\n")[-1] if buf else ""
            self._update_completion(last_line)

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        if event.input.id != "input" or not self.owner:
            return
        text = event.value.strip()
        inp = self.query_one("#input", MessageInput)
        inp.value = ""
        cl = self.query_one(CompletionList)
        cl.hide()
        self._completing = False
        if text:
            self.run_worker(self._handle_command(text), exclusive=False)

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
                self.run_worker(self._handle_command(selected.strip()), exclusive=False)

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
                self._chat(
                    Text(
                        f"{now()}  [system] 沒有可用的 engine（server 未偵測到 opencode 或 claude）",
                        style=RED_STYLE,
                    )
                )
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
            self._chat(
                Text(
                    f"{now()}  [spawning] {chosen['display']} as '{agent_name}'...",
                    style=GREEN_STYLE,
                )
            )
            if self.ws is not None:
                await self.ws.send(
                    json.dumps(
                        {
                            "type": "spawn_agent",
                            "name": agent_name,
                            "model": chosen["full_id"],
                            "engine": chosen.get("engine", "opencode"),
                        }
                    )
                )
            return

        if text == "/kick-all":
            if not self.agents:
                self._chat(
                    Text(f"{now()}  [system] 目前沒有成員可踢除", style=YELLOW_STYLE)
                )
            else:
                if self.ws is not None:
                    for agent in list(self.agents):
                        await self.ws.send(
                            json.dumps(
                                {
                                    "type": "kick_agent",
                                    "agent_name": agent["name"],
                                }
                            )
                        )
            return

        if text == "/kick":
            if not self.agents:
                self._chat(
                    Text(f"{now()}  [system] 目前沒有成員可踢除", style=YELLOW_STYLE)
                )
                return
            kick_agents = [
                {"name": a["name"], "model": a.get("model", "?")} for a in self.agents
            ]
            chosen = await self.push_screen_wait(KickPickerScreen(kick_agents))
            if chosen is None:
                return
            if self.ws is not None:
                await self.ws.send(
                    json.dumps(
                        {
                            "type": "kick_agent",
                            "agent_name": chosen["name"],
                        }
                    )
                )
            return

        if text.startswith("/broadcast"):
            content = text[len("/broadcast") :].strip()
            if not content:
                self._chat(
                    Text(
                        f"{now()}  [system] 用法：/broadcast <訊息>", style=YELLOW_STYLE
                    )
                )
            elif not self.agents:
                self._chat(
                    Text(
                        f"{now()}  [system] 沒有 agent 可以廣播，請先 /add-agent",
                        style=YELLOW_STYLE,
                    )
                )
            else:
                if self.ws is not None:
                    await self.ws.send(
                        json.dumps({"type": "broadcast", "content": content})
                    )
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
                    result.append(
                        {
                            "display": f"{pid} - {mname}",
                            "full_id": f"{pid}/{mid}",
                            "engine": "opencode",
                            "base_name": base_name[:12],
                        }
                    )
        except Exception:
            pass

    # Claude engine (claude_agent_sdk bundled binary)
    if "claude" in available_engines:
        for model_id, label, base in [
            ("claude-opus-4-6", "Opus 4.6  · Most capable", "claude-opus"),
            ("claude-sonnet-4-6", "Sonnet 4.6 · Best for everyday", "claude-sonnet"),
            ("claude-haiku-4-5", "Haiku 4.5  · Fastest", "claude-haiku"),
        ]:
            result.append(
                {
                    "display": f"claude - {label}",
                    "full_id": f"claude/{model_id}",
                    "engine": "claude",
                    "base_name": base,
                }
            )

    return result


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenParty TUI — Textual-based room client"
    )
    parser.add_argument("--room", default="debate-001", help="Room ID to join")
    parser.add_argument(
        "--server", default=OPENPARTY_SERVER, help="Server WebSocket URL"
    )
    parser.add_argument("--name", default="Human", help="Your display name")
    parser.add_argument(
        "--owner", action="store_true", help="Join as room owner (can speak)"
    )
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
