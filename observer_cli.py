"""
OpenParty Observer CLI — live view with fixed bottom input bar.

Usage:
    python observer_cli.py --room my-room
    python observer_cli.py --room my-room --owner --name Andy
"""

import asyncio
import curses
import json
import argparse
import locale
import re
import signal
import subprocess
import sys
import unicodedata
from datetime import datetime
from typing import Optional

import aiohttp
import websockets


OPENCODE_URL = "http://127.0.0.1:4096"
OPENPARTY_SERVER = "ws://localhost:8765"

COMMANDS = [
    ("/leave",     "離開房間"),
    ("/add-agent", "新增 AI agent 加入房間"),
    ("/kick",      "踢除房間成員"),
    ("/kick-all",  "踢除所有 AI agent"),
    ("/broadcast", "同時向所有 agent 發話，並行回答"),
]

# Commands that require a trailing argument — selecting from completion
# should only fill the input (with a trailing space), not execute immediately.
COMMANDS_WITH_ARGS = {"/broadcast"}

AGENT_COLORS_CURSES = [
    curses.COLOR_CYAN,
    curses.COLOR_YELLOW,
    curses.COLOR_GREEN,
    curses.COLOR_MAGENTA,
    curses.COLOR_BLUE,
]

# Color pair IDs
PAIR_DIM       = 1
PAIR_BOLD      = 2
PAIR_CYAN      = 3
PAIR_YELLOW    = 4
PAIR_GREEN     = 5
PAIR_MAGENTA   = 6
PAIR_BLUE      = 7
PAIR_RED       = 8
PAIR_INPUT_BAR = 9
PAIR_AGENT_0   = 10
PAIR_AGENT_1   = 11
PAIR_AGENT_2   = 12
PAIR_AGENT_3   = 13
PAIR_AGENT_4   = 14
PAIR_OWNER     = 15

agent_color_map: dict[str, int] = {}  # name → curses pair ID
message_queue: asyncio.Queue = asyncio.Queue()


def display_width(s: str) -> int:
    """計算字串的顯示寬度（CJK 字元佔 2 格）。"""
    width = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ('W', 'F') else 1
    return width


def truncate_to_display_width(s: str, max_width: int) -> str:
    """保留字串尾端、符合 max_width 顯示寬度（用於 input bar 顯示游標端）。"""
    width = 0
    result = []
    for ch in reversed(s):
        eaw = unicodedata.east_asian_width(ch)
        ch_w = 2 if eaw in ('W', 'F') else 1
        if width + ch_w > max_width:
            break
        width += ch_w
        result.append(ch)
    return ''.join(reversed(result))


def truncate_head(s: str, max_width: int) -> str:
    """保留字串開頭、符合 max_width 顯示寬度（用於 chat 行截斷）。"""
    width = 0
    result = []
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        ch_w = 2 if eaw in ('W', 'F') else 1
        if width + ch_w > max_width:
            break
        width += ch_w
        result.append(ch)
    return ''.join(result)


def unicode_wrap(text: str, width: int) -> list[str]:
    """以 Unicode 顯示寬度換行，支援 CJK 字元（佔 2 格），保留縮排。"""
    if not text:
        return [""]
    # 保留前置空白作為每行縮排
    stripped = text.lstrip(" ")
    indent = text[: len(text) - len(stripped)]
    indent_w = display_width(indent)
    effective_w = max(width - indent_w, 10)  # effective wrap width for content

    words = stripped.split(" ")
    lines: list[str] = []
    cur_words: list[str] = []
    cur_w = 0

    for word in words:
        word_w = display_width(word)
        if not cur_words:
            cur_words = [word]
            cur_w = word_w
        elif cur_w + 1 + word_w <= effective_w:
            cur_words.append(word)
            cur_w += 1 + word_w
        else:
            lines.append(indent + " ".join(cur_words))
            # If a single word is wider than effective_w, break it by chars
            if word_w > effective_w:
                parts = []
                part = ""
                part_w = 0
                for ch in word:
                    ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                    if part_w + ch_w > effective_w:
                        parts.append(indent + part)
                        part = ch
                        part_w = ch_w
                    else:
                        part += ch
                        part_w += ch_w
                if part:
                    parts.append(indent + part)
                # Last part becomes current line
                *head, tail = parts
                lines.extend(head)
                cur_words = [tail.lstrip()]
                cur_w = display_width(cur_words[0])
            else:
                cur_words = [word]
                cur_w = word_w

    if cur_words:
        lines.append(indent + " ".join(cur_words))
    return lines or [""]


def color_pair_for(name: str) -> int:
    if name not in agent_color_map:
        idx = len(agent_color_map) % 5
        agent_color_map[name] = PAIR_AGENT_0 + idx
    return agent_color_map[name]


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── Curses UI ──────────────────────────────────────────────────────────────────

class ChatUI:
    def __init__(self, stdscr, owner: bool, room_id: str, name: str):
        self.stdscr = stdscr
        self.owner = owner
        self.room_id = room_id
        self.name = name
        self.input_buf = ""
        self.input_cursor = 0  # character index into input_buf
        self.lines: list[tuple[str, int]] = []  # (text, color_pair)
        self.available_engines: list[str] = []  # filled from server "joined" msg

        # Command / mention completion state
        self.completing = False
        self.completing_type: str = "command"  # "command" | "mention"
        self.completion_items: list[tuple[str, str]] = []  # [(value, desc)]
        self.completion_idx = 0
        self.completion_win: Optional[curses.window] = None

        # Generic picker state ("" = closed, "model" = /add-agent, "kick" = /kick)
        self.picker_mode: str = ""
        self.picker_items: list[dict] = []      # currently visible (filtered) items
        self.picker_all_items: list[dict] = []  # full unfiltered list
        self.picker_search: str = ""            # search query (model mode only)
        self.picker_idx = 0
        self.model_win: Optional[curses.window] = None

        # Agents currently in the room: [{agent_id, name, model}]
        self.agents: list[dict] = []

        # Scroll state (0 = bottom / latest, positive = scrolled up)
        self.scroll_offset = 0

        # Selection state (line indices into self.lines; None = no selection)
        self.sel_start: int | None = None
        self.sel_end: int | None = None
        self._mouse_selecting = False

        # Multi-line input tracking
        self._last_input_h = 1

        self._setup_colors()
        self._setup_windows()

    def _setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(PAIR_DIM,       curses.COLOR_WHITE,   -1)
        curses.init_pair(PAIR_BOLD,      curses.COLOR_WHITE,   -1)
        curses.init_pair(PAIR_CYAN,      curses.COLOR_CYAN,    -1)
        curses.init_pair(PAIR_YELLOW,    curses.COLOR_YELLOW,  -1)
        curses.init_pair(PAIR_GREEN,     curses.COLOR_GREEN,   -1)
        curses.init_pair(PAIR_MAGENTA,   curses.COLOR_MAGENTA, -1)
        curses.init_pair(PAIR_BLUE,      curses.COLOR_BLUE,    -1)
        curses.init_pair(PAIR_RED,       curses.COLOR_RED,     -1)
        curses.init_pair(PAIR_INPUT_BAR, curses.COLOR_BLACK,   curses.COLOR_WHITE)
        curses.init_pair(PAIR_AGENT_0,   curses.COLOR_CYAN,    -1)
        curses.init_pair(PAIR_AGENT_1,   curses.COLOR_YELLOW,  -1)
        curses.init_pair(PAIR_AGENT_2,   curses.COLOR_GREEN,   -1)
        curses.init_pair(PAIR_AGENT_3,   curses.COLOR_MAGENTA, -1)
        curses.init_pair(PAIR_AGENT_4,   curses.COLOR_BLUE,    -1)
        curses.init_pair(PAIR_OWNER,     curses.COLOR_BLACK,   curses.COLOR_WHITE)

    def _setup_windows(self):
        h, w = self.stdscr.getmaxyx()
        self.chat_win = curses.newwin(h - 2, w, 0, 0)
        # scrollok disabled — we manage scrolling manually
        self.sep_win = curses.newwin(1, w, h - 2, 0)
        self.input_win = curses.newwin(1, w, h - 1, 0)
        self.chat_h = h - 2
        self.chat_w = w
        self.term_h = h
        # Enable mouse: wheel scroll + click.
        # macOS ncurses sends 0x08000000 for scroll-down (not the standard BUTTON5_PRESSED).
        _btn5 = getattr(curses, "BUTTON5_PRESSED", 0x08000000)
        self._mouse_btn5 = _btn5
        curses.mousemask(
            curses.BUTTON4_PRESSED    # wheel up
            | _btn5                   # wheel down (0x08000000 on macOS)
            | curses.BUTTON1_PRESSED  # selection start
            | curses.BUTTON1_RELEASED # selection end
            | curses.BUTTON1_CLICKED  # left click (scrollbar jump)
        )
        try:
            curses.curs_set(1)
        except curses.error:
            pass

    def _filter_picker(self):
        """Recompute picker_items from picker_all_items based on picker_search."""
        q = self.picker_search.lower()
        if q:
            self.picker_items = [
                item for item in self.picker_all_items
                if q in item.get("display", "").lower()
            ]
        else:
            self.picker_items = list(self.picker_all_items)
        self.picker_idx = 0

    def resize(self):
        """Resize existing sub-windows in-place after terminal resize or focus restore."""
        try:
            curses.update_lines_cols()
        except AttributeError:
            pass
        h, w = self.stdscr.getmaxyx()
        chat_h = max(1, h - 2)
        chat_w = max(1, w)
        # Discard stale popup windows
        self.completion_win = None
        self.model_win = None
        # Resize existing windows in-place (no destroy/create gap)
        try:
            self.chat_win.resize(chat_h, chat_w)
            self.sep_win.resize(1, chat_w)
            self.sep_win.mvwin(max(0, h - 2), 0)
            self.input_win.resize(1, chat_w)
            self.input_win.mvwin(max(0, h - 1), 0)
        except curses.error:
            # Fallback: recreate if resize/mvwin fails (e.g. terminal too small)
            self.chat_win  = curses.newwin(chat_h, chat_w, 0, 0)
            self.sep_win   = curses.newwin(1, chat_w, max(0, h - 2), 0)
            self.input_win = curses.newwin(1, chat_w, max(0, h - 1), 0)
        self.chat_h = chat_h
        self.chat_w = chat_w
        self.term_h = h
        self._last_input_h = 1
        self.render()

    def _redraw_sep(self):
        self.sep_win.erase()
        self.sep_win.bkgd(' ', curses.color_pair(PAIR_INPUT_BAR))
        if self.owner:
            hint = f" [{self.name}] Type message and Enter to send. /leave to exit."
        else:
            hint = f" [Observer: {self.name}] Read-only mode."
        self.sep_win.addstr(0, 0, hint[:self.chat_w - 1], curses.color_pair(PAIR_INPUT_BAR))
        self.sep_win.noutrefresh()

    def _update_input_layout(self):
        """Resize input_win and chat_win when number of input lines changes."""
        new_h = max(1, min(5, self.input_buf.count('\n') + 1))
        if new_h == self._last_input_h:
            return
        self._last_input_h = new_h
        h = self.term_h
        w = self.chat_w
        new_chat_h = max(1, h - 1 - new_h)
        try:
            self.chat_win.resize(new_chat_h, w)
            self.sep_win.mvwin(max(0, h - 1 - new_h), 0)
            self.input_win.resize(new_h, w)
            self.input_win.mvwin(max(0, h - new_h), 0)
        except curses.error:
            pass
        self.chat_h = new_chat_h

    def _redraw_input(self):
        self._update_input_layout()
        self.input_win.erase()
        prompt_w = 2
        content_w = max(1, self.chat_w - 1 - prompt_w)

        input_lines = self.input_buf.split('\n')
        before_cursor = self.input_buf[:self.input_cursor]
        cur_line_idx = before_cursor.count('\n')
        cur_line_before = before_cursor.split('\n')[-1]

        # Which lines to show (scroll so cursor is always visible)
        input_h = self._last_input_h
        start_line = max(0, cur_line_idx - input_h + 1)

        for row in range(input_h):
            line_idx = start_line + row
            if line_idx >= len(input_lines):
                break
            line_text = input_lines[line_idx]
            prompt = "> " if line_idx == 0 else "  "
            before_w = display_width(line_text)
            if before_w <= content_w:
                visible = truncate_head(line_text, content_w)
            else:
                visible = truncate_to_display_width(line_text, content_w)
            try:
                self.input_win.addstr(row, 0, prompt + visible)
            except curses.error:
                pass

        # Position cursor
        cursor_row = cur_line_idx - start_line
        cursor_x = prompt_w + display_width(cur_line_before)
        try:
            self.input_win.move(cursor_row, min(cursor_x, self.chat_w - 1))
        except curses.error:
            pass
        self.input_win.noutrefresh()

    def add_line(self, text: str, pair: int = PAIR_DIM):
        """Add a line to the chat window, word-wrapping if needed."""
        wrap_w = self.chat_w - 2  # leave 1 col for scrollbar
        for line in unicode_wrap(text, wrap_w):
            self.lines.append((line, pair))
        # Only auto-scroll to bottom if user is already at bottom
        if self.scroll_offset == 0:
            self._redraw_chat()
        # else: keep position — user is reading history

    def _redraw_chat(self):
        """Render the chat area based on current scroll_offset."""
        self.chat_win.erase()
        total = len(self.lines)
        visible = self.chat_h

        # Clamp offset
        max_offset = max(0, total - visible)
        self.scroll_offset = min(self.scroll_offset, max_offset)

        start = max(0, total - visible - self.scroll_offset)
        sel_lo = min(self.sel_start, self.sel_end) if self.sel_start is not None and self.sel_end is not None else None
        sel_hi = max(self.sel_start, self.sel_end) if self.sel_start is not None and self.sel_end is not None else None
        for row in range(visible):
            idx = start + row
            if idx >= total:
                break
            text, pair = self.lines[idx]
            extra = curses.A_REVERSE if sel_lo is not None and sel_lo <= idx <= sel_hi else 0
            _render_line_with_mentions(self.chat_win, row, text, pair, extra)

        # Scrollbar on rightmost column
        if total > visible:
            thumb_size = max(1, int(visible * visible / total))
            thumb_pos = int((start / max(1, total - visible)) * (visible - thumb_size))
            for row in range(visible):
                if thumb_pos <= row < thumb_pos + thumb_size:
                    ch, attr = '\u2588', curses.color_pair(PAIR_CYAN)  # █
                else:
                    ch, attr = '\u2591', curses.color_pair(PAIR_DIM)   # ░
                try:
                    self.chat_win.addstr(row, self.chat_w - 1, ch, attr)
                except curses.error:
                    pass
        elif self.scroll_offset > 0:
            # Scrolled up indicator even when not enough lines for full bar
            try:
                self.chat_win.addstr(0, self.chat_w - 1, '\u2191', curses.color_pair(PAIR_CYAN))
            except curses.error:
                pass

        self.chat_win.noutrefresh()

    def _restore_base_windows(self):
        """Force sep and input windows to repaint over any cleared popup area."""
        try:
            self.sep_win.touchwin()
            self.sep_win.noutrefresh()
            self.input_win.touchwin()
            self.input_win.noutrefresh()
        except curses.error:
            pass

    def _redraw_completion(self):
        """Draw command completion popup just above the separator."""
        if self.completion_win:
            try:
                self.completion_win.erase()
                self.completion_win.noutrefresh()
            except curses.error:
                pass
            self.completion_win = None
            self._restore_base_windows()

        if not self.completing or not self.completion_items:
            return

        popup_h = len(self.completion_items) + 2
        popup_w = min(self.chat_w - 4, 60)
        popup_y = max(0, self.term_h - 2 - popup_h)
        popup_x = 2

        try:
            win = curses.newwin(popup_h, popup_w, popup_y, popup_x)
            win.bkgd(' ', curses.color_pair(PAIR_INPUT_BAR))
            win.border()
            for i, (value, desc) in enumerate(self.completion_items):
                if self.completing_type == "mention":
                    label = f" {value}"[:popup_w - 2]
                else:
                    label = f" {value:<20} {desc}"[:popup_w - 2]
                pair = curses.color_pair(PAIR_CYAN) | curses.A_BOLD if i == self.completion_idx \
                       else curses.color_pair(PAIR_INPUT_BAR)
                try:
                    win.addstr(i + 1, 1, label, pair)
                except curses.error:
                    pass
            win.noutrefresh()
            self.completion_win = win
        except curses.error:
            pass

    def _redraw_model_picker(self):
        """Draw picker popup for /add-agent and /kick."""
        if self.model_win:
            try:
                self.model_win.erase()
                self.model_win.noutrefresh()
            except curses.error:
                pass
            self.model_win = None
            self._restore_base_windows()

        if not self.picker_mode:
            return
        # model mode needs at least picker_all_items; kick mode needs picker_items
        if self.picker_mode == "model" and not self.picker_all_items:
            return
        if self.picker_mode != "model" and not self.picker_items:
            return

        has_search = self.picker_mode == "model"
        items = self.picker_items

        visible = min(10, len(items))
        # +1 for search box row (model mode only)
        extra = 1 if has_search else 0
        popup_h = max(visible, 1) + 3 + extra
        popup_w = min(self.chat_w - 4, 66)
        popup_y = max(0, self.term_h - 2 - popup_h)
        popup_x = 2

        # scroll window so selected item is visible
        start = max(0, self.picker_idx - visible + 1)
        shown = items[start:start + visible]

        if self.picker_mode == "kick":
            title = " 選擇踢除成員 (↑↓ Enter Esc) "
        else:
            match_info = f"{len(items)}/{len(self.picker_all_items)}"
            title = f" 選擇 Agent [{match_info}]  ↑↓ Enter Esc "
        title = title[:popup_w - 2]

        item_row_start = 1 + extra  # row where list items begin

        try:
            win = curses.newwin(popup_h, popup_w, popup_y, popup_x)
            win.bkgd(' ', curses.color_pair(PAIR_INPUT_BAR))
            win.border()
            try:
                win.addstr(0, 1, title, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            except curses.error:
                pass

            if has_search:
                # Search box at row 1
                cursor_marker = "\u258f" if len(self.picker_search) < popup_w - 6 else ""
                search_line = f" / {self.picker_search}{cursor_marker}"[:popup_w - 2]
                try:
                    win.addstr(1, 1, search_line, curses.color_pair(PAIR_INPUT_BAR) | curses.A_BOLD)
                except curses.error:
                    pass

            if not items:
                try:
                    win.addstr(item_row_start, 1, " (no results)", curses.color_pair(PAIR_DIM))
                except curses.error:
                    pass
            else:
                for row, item in enumerate(shown):
                    label = f" {item['display']}"[:popup_w - 2]
                    idx = start + row
                    pair = curses.color_pair(PAIR_CYAN) | curses.A_BOLD if idx == self.picker_idx \
                           else curses.color_pair(PAIR_INPUT_BAR)
                    try:
                        win.addstr(item_row_start + row, 1, label, pair)
                    except curses.error:
                        pass

            win.noutrefresh()
            self.model_win = win
        except curses.error:
            pass

    def _screen_row_to_line_idx(self, y: int) -> int | None:
        """Convert a chat-window row to a self.lines index. None if out of range."""
        total = len(self.lines)
        visible = self.chat_h
        start = max(0, total - visible - self.scroll_offset)
        idx = start + y
        return idx if 0 <= idx < total else None

    def _get_selected_text(self) -> str:
        if self.sel_start is None or self.sel_end is None:
            return ""
        lo = min(self.sel_start, self.sel_end)
        hi = max(self.sel_start, self.sel_end)
        # Strip inline markdown markers from copied text
        md_re = re.compile(r'\*\*(.+?)\*\*|\*(.+?)\*', re.DOTALL)
        lines = []
        for idx in range(lo, hi + 1):
            if idx < len(self.lines):
                raw, _ = self.lines[idx]
                clean = md_re.sub(lambda m: m.group(1) or m.group(2), raw)
                lines.append(clean)
        return "\n".join(lines)

    def copy_selection(self) -> bool:
        """Copy selected text to clipboard via pbcopy. Returns True on success."""
        text = self._get_selected_text()
        if not text:
            return False
        try:
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(text.encode("utf-8"))
            return True
        except Exception:
            return False

    def clear_selection(self):
        self.sel_start = None
        self.sel_end = None
        self._mouse_selecting = False

    def render(self):
        self._redraw_chat()
        self._redraw_sep()
        self._redraw_input()
        self._redraw_completion()
        self._redraw_model_picker()
        curses.doupdate()

    def handle_event(self, msg: dict) -> str | None:
        """Process a server message, return text to send (if owner typed /leave)."""
        t = msg.get("type")

        if t == "joined":
            self.available_engines = msg.get("available_engines", [])
            state = msg.get("room_state", {})
            topic = state.get("topic", "(waiting for owner to set topic)")
            participants = state.get("participants", [])
            self.agents = list(participants)  # init agent list
            self.add_line("=" * 60, PAIR_BOLD)
            self.add_line(f"  OpenParty — Room: {self.room_id}", PAIR_BOLD)
            self.add_line(f"  Topic: {topic}", PAIR_DIM)
            if participants:
                self.add_line(f"  Agents: {', '.join(p['name'] for p in participants)}", PAIR_DIM)
            else:
                self.add_line("  Agents: (waiting...)", PAIR_DIM)
            self.add_line("=" * 60, PAIR_BOLD)
            if self.owner:
                self.add_line("  You are the room owner. Send a message to set the topic and start.", PAIR_GREEN)
            history = msg.get("history", [])
            if history:
                self.add_line(f"  [replaying {len(history)} messages]", PAIR_DIM)
                for entry in history:
                    self._print_message(entry)

        elif t == "agent_joined":
            pair = color_pair_for(msg["name"])
            self.add_line(f"{now()}  ++ {msg['name']} ({msg['model']}) joined", pair)
            self.agents.append({
                "agent_id": msg.get("agent_id", msg["name"]),
                "name": msg["name"],
                "model": msg.get("model", ""),
            })

        elif t == "agent_left":
            self.add_line(
                f"{now()}  -- {msg['name']} left  ({msg.get('agents_remaining', '?')} remaining)",
                PAIR_RED,
            )
            self.agents = [a for a in self.agents if a["name"] != msg["name"]]

        elif t == "model_updated":
            agent_name = msg.get("name", "")
            new_model = msg.get("model", "")
            for a in self.agents:
                if a["name"] == agent_name:
                    a["model"] = new_model
                    break
            label = _model_label(new_model)
            self.add_line(f"{now()}  [model] {agent_name} → {label}", PAIR_DIM)

        elif t == "system_message":
            self.add_line(f"{now()}  *** {msg.get('text', '')}", PAIR_YELLOW)

        elif t == "waiting_for_owner":
            self.add_line(f"{now()}  [server] {msg.get('message', '')}", PAIR_DIM)

        elif t == "turn_start":
            pair = color_pair_for(msg["name"])
            self.add_line(f"{now()}  » {msg['name']} is thinking...", pair)

        elif t == "turn_end":
            latency = msg.get("latency_ms", 0)
            self.add_line(f"  ({latency}ms)", PAIR_DIM)

        elif t == "message":
            self._print_message(msg)

        elif t == "spawn_result":
            name = msg.get("name", "?")
            model = msg.get("model", "?")
            if msg.get("success"):
                self.add_line(f"{now()}  [server] 已啟動 {name} ({model})", PAIR_GREEN)
            else:
                self.add_line(f"{now()}  [server] 啟動 {name} 失敗", PAIR_RED)

        elif t == "room_state":
            pass  # silently ignore

        else:
            self.add_line(f"{now()}  [{t}] {json.dumps(msg)[:80]}", PAIR_DIM)

        self.render()

    def _print_message(self, entry: dict):
        name = entry.get("name", "?")
        model = entry.get("model", "")
        content = entry.get("content", "")
        is_private = entry.get("is_private", False)
        private_to = entry.get("private_to", [])
        is_owner_msg = name.startswith("[owner]")
        pair = curses.color_pair(PAIR_OWNER) if is_owner_msg else color_pair_for(name)
        self.add_line("", PAIR_DIM)
        model_label = _model_label(model)
        header = f"  {name}  ({model_label})" if model_label else f"  {name}"
        if is_private:
            if private_to:
                whisper_tag = f" 【私訊 → {', '.join(private_to)}】"
            else:
                whisper_tag = " 【私訊】"
            self.add_line(header + whisper_tag, curses.color_pair(PAIR_MAGENTA) | curses.A_BOLD)
            for line in content.split("\n"):
                self.add_line(f"    {line}", curses.color_pair(PAIR_MAGENTA))
        elif is_owner_msg:
            self.add_line(header, curses.color_pair(PAIR_OWNER) | curses.A_BOLD)
            for line in content.split("\n"):
                self.add_line(f"    {line}", curses.color_pair(PAIR_OWNER))
        else:
            self.add_line(header, pair | curses.A_BOLD)
            for line in content.split("\n"):
                self.add_line(f"    {line}", PAIR_DIM)


MENTION_RE = re.compile(r"(@[\w][\w\-]*)")


_INLINE_RE = re.compile(r'(\*\*(.+?)\*\*|\*(.+?)\*|@[\w][\w\-]*)', re.DOTALL)


def _parse_inline(text: str, base_pair: int, extra_attr: int = 0) -> list[tuple[str, int]]:
    """Parse **bold**, *italic*, and @mentions into (text, attr) segments."""
    segments: list[tuple[str, int]] = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            segments.append((text[last:m.start()], base_pair | extra_attr))
        full = m.group(0)
        if full.startswith("**"):
            segments.append((m.group(2), base_pair | curses.A_BOLD | extra_attr))
        elif full.startswith("*"):
            segments.append((m.group(3), base_pair | curses.A_DIM | extra_attr))
        else:  # @mention
            segments.append((full, curses.color_pair(PAIR_CYAN) | curses.A_BOLD | extra_attr))
        last = m.end()
    if last < len(text):
        segments.append((text[last:], base_pair | extra_attr))
    return segments


def _render_line_with_mentions(win, y: int, text: str, base_pair: int, extra_attr: int = 0):
    """Render a line of text with **bold**, *italic*, and @mention highlighting."""
    segments = _parse_inline(text, curses.color_pair(base_pair), extra_attr)
    x = 0
    max_x = win.getmaxyx()[1] - 1
    for seg_text, attr in segments:
        if not seg_text or x >= max_x:
            break
        safe = truncate_head(seg_text, max_x - x)
        if safe:
            try:
                win.addstr(y, x, safe, attr)
            except curses.error:
                break
            x += display_width(safe)


def _model_label(model: str) -> str:
    """Return a concise, user-friendly model label from the raw model string."""
    if not model or model in ("human", "unknown", ""):
        return ""
    # "opencode/openai/gpt-4o"  → "gpt-4o"
    # "opencode/minimax/minimax-m2.5" → "minimax-m2.5"
    # "claude-sonnet"            → "claude-sonnet"
    # "claude"                   → "claude"
    parts = model.split("/")
    if parts[0] == "opencode" and len(parts) >= 2:
        return parts[-1]   # last segment is the model id
    return parts[-1]       # for claude-sonnet, model, etc.


# ── Async loops ────────────────────────────────────────────────────────────────

async def recv_loop(ws, ui: ChatUI):
    """Receive WebSocket messages and push to queue."""
    async for raw in ws:
        msg = json.loads(raw)
        await message_queue.put(msg)


async def fetch_models(available_engines: list[str]) -> list[dict]:
    """Build agent list based on what the server has available."""
    result = []

    # ── OpenCode models (from opencode serve HTTP API) ────────────────────────
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

    # ── Claude engine (claude_agent_sdk bundled binary) ───────────────────────
    if "claude" in available_engines:
        for model_id, label, base in [
            ("claude-opus-4-6",   "Opus 4.6  · Most capable",     "claude-opus"),
            ("claude-sonnet-4-6", "Sonnet 4.6 · Best for everyday", "claude-sonnet"),
            ("claude-haiku-4-5",  "Haiku 4.5  · Fastest",          "claude-haiku"),
        ]:
            result.append({
                "display": f"claude - {label}",
                "full_id": f"claude/{model_id}",
                "engine": "claude",
                "base_name": base,
            })

    return result


async def spawn_agent(ws, name: str, model_id: str, engine: str = "opencode"):
    """Ask the server to spawn a bridge.py agent process."""
    await ws.send(json.dumps({
        "type": "spawn_agent",
        "name": name,
        "model": model_id,
        "engine": engine,
    }))


def _update_completion(ui: ChatUI):
    """Recompute completion_items based on current input_buf."""
    buf = ui.input_buf

    # ── @mention completion: triggered when buffer ends with @partial ─────────
    at_match = re.search(r"@([\w\-]*)$", buf)
    if at_match and not ui.picker_mode:
        partial = at_match.group(1).lower()
        matches = [
            (f"@{a['name']}", a.get("model", ""))
            for a in ui.agents
            if a["name"].lower().startswith(partial)
        ]
        # Also match observer's own name if ui.name is available and not the owner
        if matches:
            ui.completing = True
            ui.completing_type = "mention"
            ui.completion_items = matches
            ui.completion_idx = min(ui.completion_idx, max(0, len(matches) - 1))
            return

    # ── /command completion ────────────────────────────────────────────────────
    if buf.startswith("/") and not ui.picker_mode:
        matches = [(cmd, desc) for cmd, desc in COMMANDS if cmd.startswith(buf)]
        if matches and buf != matches[0][0]:  # don't show if already an exact match
            ui.completing = True
            ui.completing_type = "command"
            ui.completion_items = matches
            ui.completion_idx = min(ui.completion_idx, max(0, len(matches) - 1))
            return

    ui.completing = False
    ui.completing_type = "command"
    ui.completion_items = []
    ui.completion_idx = 0


async def ui_loop(stdscr, ws, ui: ChatUI, owner: bool):
    """Handle keyboard input (non-blocking) and process message queue."""
    stdscr.nodelay(True)
    curses.cbreak()
    stdscr.keypad(True)
    # Read input from input_win instead of stdscr so that the implicit
    # wrefresh(win) inside get_wch() refreshes input_win (cursor stays in
    # the input bar) rather than stdscr (which would flash the cursor to
    # stdscr's (0,0) position — i.e. the chat area — on every poll).
    ui.input_win.nodelay(True)
    ui.input_win.keypad(True)

    # SIGWINCH fires on terminal resize and, on some macOS terminals, on
    # focus restore — both cases should trigger a full window rebuild.
    _resize_needed = False

    def _on_sigwinch():
        nonlocal _resize_needed
        _resize_needed = True

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGWINCH, _on_sigwinch)
    except (OSError, NotImplementedError):
        pass  # Windows or environments without SIGWINCH

    ui.render()

    while True:
        # Handle deferred resize / focus-restore repaint
        if _resize_needed:
            _resize_needed = False
            ui.resize()
            ui.input_win.nodelay(True)
            ui.input_win.keypad(True)

        # Process all pending server messages
        while not message_queue.empty():
            msg = await message_queue.get()
            ui.handle_event(msg)

        # Check keyboard input — get_wch() supports Unicode (including CJK).
        # Use input_win (not stdscr) so the implicit wrefresh inside get_wch
        # keeps the cursor in the input bar rather than flashing it to chat.
        ch = None
        try:
            ch = ui.input_win.get_wch()
        except curses.error:
            pass

        if ch is not None:
            # ── Terminal resize ──────────────────────────────────────────────
            if ch == curses.KEY_RESIZE:
                _resize_needed = False  # prevent double resize from SIGWINCH
                ui.resize()
                continue

            # ── Scroll keys (always active) ─────────────────────────────────
            if ch == curses.KEY_PPAGE:  # Page Up
                ui.scroll_offset += ui.chat_h // 2
                ui._redraw_chat()
                curses.doupdate()
                continue
            elif ch == curses.KEY_NPAGE:  # Page Down
                ui.scroll_offset = max(0, ui.scroll_offset - ui.chat_h // 2)
                ui._redraw_chat()
                curses.doupdate()
                continue
            elif ch == curses.KEY_END:  # End → jump to bottom
                ui.scroll_offset = 0
                ui._redraw_chat()
                curses.doupdate()
                continue

            elif ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                total = len(ui.lines)
                max_offset = max(0, total - ui.chat_h)
                # ── Mouse wheel ───────────────────────────────────────
                if bstate & curses.BUTTON4_PRESSED:   # wheel up → scroll back
                    ui.scroll_offset = min(max_offset, ui.scroll_offset + 3)
                    ui._redraw_chat()
                    curses.doupdate()
                    continue
                elif ui._mouse_btn5 and bstate & ui._mouse_btn5:  # wheel down → scroll fwd
                    ui.scroll_offset = max(0, ui.scroll_offset - 3)
                    ui._redraw_chat()
                    curses.doupdate()
                    continue
                # ── Text selection ────────────────────────────────────
                elif bstate & curses.BUTTON1_PRESSED:
                    # Always clear existing selection first
                    ui.clear_selection()
                    if 0 <= my < ui.chat_h:
                        idx = ui._screen_row_to_line_idx(my)
                        if idx is not None:
                            ui.sel_start = idx
                            ui.sel_end = idx
                            ui._mouse_selecting = True
                    ui._redraw_chat()
                    curses.doupdate()
                    continue
                elif bstate & curses.BUTTON1_RELEASED:
                    if ui._mouse_selecting:
                        ui._mouse_selecting = False
                        if 0 <= my < ui.chat_h:
                            idx = ui._screen_row_to_line_idx(my)
                            if idx is not None:
                                ui.sel_end = idx
                        # If start == end (single click, no drag) → clear selection
                        if ui.sel_start == ui.sel_end:
                            ui.clear_selection()
                        ui._redraw_chat()
                        curses.doupdate()
                    continue
                # ── Click: clear selection, optionally jump scrollbar ────────
                elif bstate & curses.BUTTON1_CLICKED:
                    if ui.sel_start is not None:
                        ui.clear_selection()
                        ui._redraw_chat()
                        curses.doupdate()
                    scrollbar_x = ui.chat_w - 1
                    if mx == scrollbar_x and 0 <= my < ui.chat_h and max_offset > 0:
                        ratio = my / max(1, ui.chat_h - 1)
                        ui.scroll_offset = int(max_offset * (1.0 - ratio))
                        ui._redraw_chat()
                        curses.doupdate()
                    continue

        # ── Ctrl+C: copy selection (works for owner and observer) ──────────
        if ch == '\x03' and ui.sel_start is not None:
            ui.copy_selection()
            ui.clear_selection()
            ui._redraw_chat()
            curses.doupdate()
            continue

        # ── Any keyboard input clears selection and returns focus to input ──
        if ch is not None and ch != curses.KEY_MOUSE and ui.sel_start is not None:
            if ch not in (curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_END, curses.KEY_RESIZE):
                ui.clear_selection()
                ui._redraw_chat()
                curses.doupdate()

        if ch is not None and owner:
            # ── Picker mode (add-agent / kick) ──────────────────────────────
            if ui.picker_mode:
                if ch in (curses.KEY_UP,):
                    ui.picker_idx = max(0, ui.picker_idx - 1)
                    ui.render()
                elif ch in (curses.KEY_DOWN,):
                    ui.picker_idx = min(max(0, len(ui.picker_items) - 1), ui.picker_idx + 1)
                    ui.render()
                elif ch in (curses.KEY_ENTER, '\n', '\r'):
                    if not ui.picker_items:
                        pass  # no results — ignore Enter
                    else:
                        item = ui.picker_items[ui.picker_idx]
                        mode = ui.picker_mode
                        ui.picker_mode = ""
                        ui.picker_items = []
                        ui.picker_all_items = []
                        ui.picker_search = ""
                        ui.picker_idx = 0
                        if mode == "model":
                            base = item.get("base_name", item["full_id"].split("/")[-1])[:12]
                            existing_names = {a["name"] for a in ui.agents}
                            if base not in existing_names:
                                agent_name = base
                            else:
                                n = 2
                                while f"{base}-{n}" in existing_names:
                                    n += 1
                                agent_name = f"{base}-{n}"
                            ui.add_line(
                                f"{now()}  [spawning] {item['display']} as '{agent_name}'...",
                                PAIR_GREEN,
                            )
                            ui.render()
                            await spawn_agent(ws, agent_name, item["full_id"], item.get("engine", "opencode"))
                        elif mode == "kick":
                            await ws.send(json.dumps({
                                "type": "kick_agent",
                                "agent_name": item["agent_name"],
                            }))
                            ui.render()
                elif ch in ('\x1b',) or ch == 27:  # Esc
                    ui.picker_mode = ""
                    ui.picker_items = []
                    ui.picker_all_items = []
                    ui.picker_search = ""
                    ui.picker_idx = 0
                    ui.render()
                elif ui.picker_mode == "model" and ch in (curses.KEY_BACKSPACE, '\x7f', '\x08', 127, 8):
                    ui.picker_search = ui.picker_search[:-1]
                    ui._filter_picker()
                    ui.render()
                elif ui.picker_mode == "model" and isinstance(ch, str) and ord(ch) >= 32:
                    ui.picker_search += ch
                    ui._filter_picker()
                    ui.render()
                continue

            # ── Completion navigation ───────────────────────────────────────
            if ui.completing:
                if ch in (curses.KEY_UP,):
                    ui.completion_idx = max(0, ui.completion_idx - 1)
                    ui.render()
                    continue
                elif ch in (curses.KEY_DOWN,):
                    ui.completion_idx = min(
                        len(ui.completion_items) - 1, ui.completion_idx + 1
                    )
                    ui.render()
                    continue
                elif ch in ('\t', curses.KEY_ENTER, '\n', '\r'):
                    selected = ui.completion_items[ui.completion_idx][0]
                    if ui.completing_type == "mention":
                        # Replace only the trailing @partial in input_buf
                        ui.input_buf = re.sub(r"@[\w\-]*$", selected + " ", ui.input_buf)
                        ui.input_cursor = len(ui.input_buf)
                        ui.completing = False
                        ui.completion_items = []
                        _update_completion(ui)
                        ui.render()
                        continue  # don't send — just fill
                    else:
                        # Command: fill input; Enter also falls through to execute
                        ui.input_buf = selected
                        ui.input_cursor = len(ui.input_buf)
                        ui.completing = False
                        ui.completion_items = []
                        # Commands that need arguments: fill with trailing space, don't execute
                        if ch in ('\t',) or selected in COMMANDS_WITH_ARGS:
                            if selected in COMMANDS_WITH_ARGS:
                                ui.input_buf += " "
                                ui.input_cursor = len(ui.input_buf)
                            ui.render()
                            continue
                        # Enter falls through to normal Enter handler below
                elif ch in ('\x1b',) or ch == 27:  # Esc → dismiss
                    ui.completing = False
                    ui.completion_items = []
                    ui.render()
                    continue

            # ── Shift+Enter (Alt+Enter): insert newline ─────────────────────
            if ch == '\x1b':
                # Peek immediately for Alt+Enter (Esc followed by Enter)
                try:
                    next_ch = stdscr.get_wch()
                    if next_ch in ('\r', '\n'):
                        ui.input_buf = (
                            ui.input_buf[:ui.input_cursor] + '\n' +
                            ui.input_buf[ui.input_cursor:]
                        )
                        ui.input_cursor += 1
                        ui.render()
                    # else: stray escape, ignore both characters
                except curses.error:
                    pass  # lone Esc, ignore
                continue

            # ── Normal input ────────────────────────────────────────────────
            if ch in (curses.KEY_ENTER, '\n', '\r'):
                text = ui.input_buf.strip()
                ui.input_buf = ""
                ui.input_cursor = 0
                ui.completing = False
                ui.completion_items = []

                if text == "/leave":
                    await ws.close()
                    return
                elif text == "/add-agent":
                    ui.add_line(f"{now()}  [system] 正在讀取可用模型...", PAIR_DIM)
                    ui.render()
                    models = await fetch_models(ui.available_engines)
                    if not models:
                        ui.add_line(
                            f"{now()}  [system] 沒有可用的 engine（server 未偵測到 opencode 或 claude）",
                            PAIR_RED,
                        )
                    else:
                        ui.picker_all_items = list(models)
                        ui.picker_items = list(models)
                        ui.picker_search = ""
                        ui.picker_idx = 0
                        ui.picker_mode = "model"
                    ui.render()
                elif text == "/kick-all":
                    if not ui.agents:
                        ui.add_line(f"{now()}  [system] 目前沒有成員可踢除", PAIR_YELLOW)
                    else:
                        for agent in list(ui.agents):
                            await ws.send(json.dumps({
                                "type": "kick_agent",
                                "agent_name": agent["name"],
                            }))
                elif text == "/kick":
                    if not ui.agents:
                        ui.add_line(f"{now()}  [system] 目前沒有成員可踢除", PAIR_YELLOW)
                    else:
                        ui.picker_items = [
                            {"display": f"{a['name']}  ({a.get('model', '?')})", "agent_name": a["name"]}
                            for a in ui.agents
                        ]
                        ui.picker_idx = 0
                        ui.picker_mode = "kick"
                    ui.render()
                elif text.startswith("/broadcast"):
                    content = text[len("/broadcast"):].strip()
                    if not content:
                        ui.add_line(f"{now()}  [system] 用法：/broadcast <訊息>", PAIR_YELLOW)
                    elif not ui.agents:
                        ui.add_line(f"{now()}  [system] 沒有 agent 可以廣播，請先 /add-agent", PAIR_YELLOW)
                    else:
                        await ws.send(json.dumps({"type": "broadcast", "content": content}))
                    ui.render()
                elif text:
                    await ws.send(json.dumps({"type": "message", "content": text}))
                ui.render()

            elif ch == curses.KEY_LEFT:
                ui.input_cursor = max(0, ui.input_cursor - 1)
                ui.render()

            elif ch == curses.KEY_RIGHT:
                ui.input_cursor = min(len(ui.input_buf), ui.input_cursor + 1)
                ui.render()

            elif ch == curses.KEY_HOME or ch == '\x01':  # Home / Ctrl+A
                ui.input_cursor = 0
                ui.render()

            elif ch == '\x05':  # Ctrl+E → end of line
                ui.input_cursor = len(ui.input_buf)
                ui.render()

            elif ch == curses.KEY_DC:  # Delete key → delete char at cursor
                if ui.input_cursor < len(ui.input_buf):
                    ui.input_buf = ui.input_buf[:ui.input_cursor] + ui.input_buf[ui.input_cursor + 1:]
                    _update_completion(ui)
                    ui.render()

            elif ch in (curses.KEY_BACKSPACE, '\x7f', '\x08', 127, 8):
                if ui.input_cursor > 0:
                    ui.input_buf = ui.input_buf[:ui.input_cursor - 1] + ui.input_buf[ui.input_cursor:]
                    ui.input_cursor -= 1
                    _update_completion(ui)
                    ui.render()

            elif isinstance(ch, str) and ord(ch) >= 32:
                ui.input_buf = ui.input_buf[:ui.input_cursor] + ch + ui.input_buf[ui.input_cursor:]
                ui.input_cursor += 1
                _update_completion(ui)
                ui.render()

        await asyncio.sleep(0.05)


# ── Main ───────────────────────────────────────────────────────────────────────

async def observe(stdscr, room_id: str, server_url: str, name: str, owner: bool):
    display_name = f"[owner] {name}" if owner else name
    ui = ChatUI(stdscr, owner=owner, room_id=room_id, name=display_name)

    try:
        async with websockets.connect(server_url) as ws:
            await ws.send(json.dumps({
                "type": "join",
                "role": "observer",
                "room_id": room_id,
                "name": display_name,
                "owner": owner,
            }))

            await asyncio.gather(
                recv_loop(ws, ui),
                ui_loop(stdscr, ws, ui, owner),
            )

    except websockets.exceptions.ConnectionClosed:
        ui.add_line("\n[Observer] Connection closed.", PAIR_RED)
        ui.render()
        await asyncio.sleep(1)
    except ConnectionRefusedError:
        curses.endwin()
        print(f"[Observer] Cannot connect to {server_url}. Is the server running?")
        sys.exit(1)


def main():
    locale.setlocale(locale.LC_ALL, '')
    parser = argparse.ArgumentParser(description="OpenParty Observer — watch a Room live")
    parser.add_argument("--room", default="debate-001", help="Room ID to observe")
    parser.add_argument("--server", default="ws://localhost:8765", help="Server URL")
    parser.add_argument("--name", default="Human", help="Your display name")
    parser.add_argument("--owner", action="store_true", help="Join as room owner (can speak)")
    args = parser.parse_args()

    def run(stdscr):
        asyncio.run(observe(stdscr, args.room, args.server, args.name, args.owner))

    curses.wrapper(run)


if __name__ == "__main__":
    main()
