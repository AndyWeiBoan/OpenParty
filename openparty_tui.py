"""
OpenParty TUI — 基於 Textual 框架的多 Agent 討論室客戶端
==========================================================

這是 observer_cli.py 的 TUI（Terminal User Interface）重新實作，
使用 Textual 框架提供豐富的互動式終端介面，讓使用者（房間擁有者）
能以可視化的方式管理討論室並與 AI Agent 互動。

主要功能：
  - 連線到 OpenParty WebSocket 伺服器，加入指定討論室
  - 以 TUI 形式即時顯示聊天訊息、Agent 狀態與思考過程
  - 房間擁有者可傳送訊息、新增 Agent、踢除成員、廣播指令
  - 支援 @檔案 附件（自動讀取並附帶檔案內容）
  - 支援剪貼簿圖片貼上（自動壓縮並傳送給 Agent）
  - 支援輸入自動補全（/命令、$@提及、@檔案路徑）
  - 即時顯示 Agent 思考狀態（spinner + 工具呼叫摘要）

使用方式：
    python openparty_tui.py --room my-room
    python openparty_tui.py --room my-room --owner --name Andy

架構說明：
  OpenPartyApp（Textual App 主類別）
    ├── RoomHeader        : 頂部固定標題列，顯示房間資訊與各 Agent 狀態
    ├── ChatLog           : 可捲動的聊天訊息區域
    ├── CompletionList    : 自動補全彈出視窗
    ├── StatusBar         : 底部狀態列（round 編號、思考/閒置狀態）
    └── MessageInput      : 多行文字輸入框（僅擁有者可見）
"""

import asyncio
import json
import argparse
import fnmatch
import os
import platform
import re
import shutil
import subprocess
import uuid as uuid_mod
from datetime import datetime
from pathlib import Path

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


# ── 常數定義 ───────────────────────────────────────────────────────────────────

# opencode serve 本機 HTTP API 的預設 URL
OPENCODE_URL = "http://127.0.0.1:4096"

# OpenParty WebSocket 伺服器的預設 URL
OPENPARTY_SERVER = "ws://localhost:8765"

# 房間擁有者可用的斜線命令清單
# 每個條目為 (命令字串, 說明文字)，用於自動補全提示
COMMANDS = [
    ("/leave", "離開房間"),
    ("/add-agent", "新增 AI agent 加入房間"),
    ("/kick", "踢除房間成員"),
    ("/kick-all", "踢除所有 AI agent"),
    ("/broadcast", "同時向所有 agent 發話，並行回答"),
]

# 需要額外引數的命令（選完後不立即執行，而是在輸入框填入命令等待補充引數）
COMMANDS_WITH_ARGS = {"/broadcast"}

# Agent 名稱顏色輪轉表：同一房間中每個 Agent 依加入順序分配不同顏色
AGENT_STYLES = ["cyan", "yellow", "green", "magenta", "blue"]

# 全域 Agent 名稱 → 顏色 映射表（memoized，避免同一 Agent 每次渲染使用不同顏色）
_agent_color_map: dict[str, str] = {}

# ── 樣式常數 ───────────────────────────────────────────────────────────────────
# 使用 Rich Style 定義各類訊息的視覺樣式，統一在此集中管理，方便修改主題

OWNER_STYLE = Style(
    color="black", bgcolor="white", bold=True
)  # 擁有者訊息標題：黑底白字粗體
OWNER_BODY = Style(color="black", bgcolor="white")  # 擁有者訊息正文：黑底白字
DIM_STYLE = Style(color="gray70")  # 系統/提示訊息：灰色淡化
BOLD_STYLE = Style(bold=True, color="white")  # 重要訊息：白色粗體
MAGENTA_STYLE = Style(color="magenta")  # 私訊：洋紅色
GREEN_STYLE = Style(color="green")  # 成功/加入通知：綠色
RED_STYLE = Style(color="red")  # 錯誤/離開通知：紅色
YELLOW_STYLE = Style(color="yellow")  # 警告/系統訊息：黃色
CYAN_STYLE = Style(color="cyan")  # 一般提示：青色


# ── 輔助函式 ───────────────────────────────────────────────────────────────────


def _agent_style(name: str) -> Style:
    """根據 Agent 名稱回傳對應的 Rich Style（顏色輪轉，已 memoize）。

    使用 round-robin 方式從 AGENT_STYLES 分配顏色，確保同一房間內
    不同 Agent 的訊息以不同顏色顯示，方便使用者辨識。
    一旦分配後就固定下來（memoize），不會因為 Agent 離開再加入而變色。

    Args:
        name: Agent 的顯示名稱

    Returns:
        對應該 Agent 的 Rich Style 物件
    """
    if name not in _agent_color_map:
        idx = len(_agent_color_map) % len(AGENT_STYLES)
        _agent_color_map[name] = AGENT_STYLES[idx]
    return Style(color=_agent_color_map[name])


def _model_label(model: str) -> str:
    """將原始 model 字串轉換為簡潔的人類可讀標籤。

    例如：
      "claude/claude-sonnet-4-6" → "claude-sonnet-4-6"
      "zen/mimo-v2-pro-free"     → "mimo-v2-pro-free"
      "human" / "" / "unknown"  → ""（不顯示）

    取 "/" 最後一段，去掉 provider 前綴，讓介面更簡潔。

    Args:
        model: 原始 model 識別字串

    Returns:
        簡潔標籤字串；若為 human/unknown/空字串則回傳空字串
    """
    if not model or model in ("human", "unknown", ""):
        return ""
    parts = model.split("/")
    return parts[-1]


def now() -> str:
    """回傳當前時間的格式化字串，精確到毫秒。

    格式：YYYY-MM-DD HH:MM:SS.mmm
    例如：2025-04-07 12:34:56.789

    用於在聊天訊息標題中顯示訊息時間戳。

    Returns:
        格式化後的時間字串
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# 正則表達式：匹配訊息中的 Markdown inline 格式標記
# 支援以下模式：
#   **粗體**      → 粗體文字
#   *斜體*        → 斜體文字
#   #名稱         → Agent/頻道提及（cyan 粗體）
#   $名稱         → 直接指令提及（yellow 粗體）
_INLINE_RE = re.compile(r"(\*\*(.+?)\*\*|\*(.+?)\*|#[\w][\w\-]*|\$[\w][\w\-]*)")


def _parse_to_rich(text: str, base_style: Style) -> Text:
    """將文字中的簡易 Markdown 標記轉換為 Rich Text 物件。

    支援的格式：
      **粗體**  → 套用 bold 樣式（合併 base_style）
      *斜體*    → 套用 italic 樣式（合併 base_style）
      #名稱     → 套用 cyan + bold 樣式（通常代表 Agent 或頻道提及）
      $名稱     → 套用 yellow + bold 樣式（通常代表直接指令提及）

    未匹配的文字段落使用 base_style。

    Args:
        text       : 原始文字字串（可能包含 Markdown 標記）
        base_style : 預設樣式，用於未匹配的文字段落

    Returns:
        Rich Text 物件，可直接傳給 RichLog.write() 或 ChatLog.write_msg()
    """
    result = Text(style=base_style)
    last = 0
    for m in _INLINE_RE.finditer(text):
        # 插入匹配前的普通文字（使用 base_style）
        if m.start() > last:
            result.append(text[last : m.start()], style=base_style)
        full = m.group(0)
        if full.startswith("**"):
            # **粗體** → 取 group(2)（去掉 ** 的內容）
            inner = m.group(2)
            result.append(inner, style=Style(bold=True) + base_style)
        elif full.startswith("*"):
            # *斜體* → 取 group(3)（去掉 * 的內容）
            inner = m.group(3)
            result.append(inner, style=Style(italic=True) + base_style)
        elif full.startswith("#"):
            # #名稱 → 頻道/Agent 提及，使用 cyan 粗體
            result.append(full, style=Style(color="cyan", bold=True))
        else:
            # $名稱 → 直接指令提及，使用 yellow 粗體
            result.append(full, style=Style(color="yellow", bold=True))
        last = m.end()
    # 插入最後一段普通文字
    if last < len(text):
        result.append(text[last:], style=base_style)
    return result


# ── 圖片剪貼簿輔助函式 ─────────────────────────────────────────────────────────

# Anthropic Vision API 的單張圖片大小上限（5 MB）
# 超過此限制的圖片會被拒絕，需在傳送前壓縮
IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB Anthropic API limit

# 支援的圖片格式 magic bytes 列表
# 格式：(magic bytes 前綴, MIME type)
# 用於在不依賴副檔名的情況下辨識圖片格式（更可靠）
_IMAGE_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),  # PNG 格式
    (b"\xff\xd8\xff", "image/jpeg"),  # JPEG 格式
    (b"GIF87a", "image/gif"),  # GIF 格式（舊版）
    (b"GIF89a", "image/gif"),  # GIF 格式（新版，支援動畫）
    (b"RIFF", "image/webp"),  # RIFF....WEBP — 需額外驗證第 8-12 byte 為 "WEBP"
]


def _verify_mime_from_bytes(data: bytes) -> str | None:
    """透過 magic bytes 辨識圖片 MIME type，不依賴副檔名。

    逐一比對 _IMAGE_MAGIC 列表中的 magic bytes 前綴：
      - PNG/JPEG/GIF：直接比對前綴即可確認格式
      - WebP：需先比對 RIFF 前綴，再確認第 8-12 byte 為 "WEBP"
              （因為 RIFF 格式也用於其他格式如 WAV，必須額外驗證）

    Args:
        data: 圖片的原始 bytes 資料

    Returns:
        識別到的 MIME type 字串（如 "image/png"）；無法識別時回傳 None
    """
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                # RIFF 前綴但非 WebP，繼續比對下一個
                continue
            return mime
    return None


def _grab_clipboard_image():
    """嘗試從作業系統剪貼簿取得圖片。

    跨平台實作：
      macOS  : 使用 PIL.ImageGrab.grabclipboard()，直接讀取系統剪貼簿
      Linux  : 依序嘗試 xclip（X11）和 wl-paste（Wayland），
               讀取 PNG 格式的剪貼簿資料後用 PIL 解析
      Windows: 目前不支援（回傳 None）

    延遲匯入 PIL（Pillow）：
      PIL 不在最小依賴集中，只有在嘗試讀取圖片時才匯入。
      若 Pillow 未安裝則靜默回傳 None，不影響 TUI 的其他功能。

    Returns:
        PIL.Image.Image 物件（成功）；或 None（無圖片或 Pillow 未安裝）
    """
    try:
        from PIL import Image, ImageGrab  # type: ignore[import-untyped]
    except ImportError:
        # Pillow 未安裝，圖片貼上功能不可用
        return None

    system = platform.system()

    if system == "Darwin":
        # macOS：PIL.ImageGrab.grabclipboard() 直接存取 macOS 剪貼簿
        try:
            img = ImageGrab.grabclipboard()
            if isinstance(img, Image.Image):
                return img
        except Exception:
            pass
        return None

    if system == "Linux":
        # Linux：先嘗試 X11 的 xclip，再嘗試 Wayland 的 wl-paste
        for cmd in [
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            ["wl-paste", "--type", "image/png"],
        ]:
            # 先確認工具是否存在於 PATH 中，避免 FileNotFoundError
            if not shutil.which(cmd[0]):
                continue
            try:
                data = subprocess.check_output(cmd, timeout=2)
                if data:
                    from io import BytesIO

                    return Image.open(BytesIO(data))
            except Exception:
                pass
        return None

    # Windows 或其他平台：目前不支援
    return None


def _save_clipboard_image(img, save_dir: str, name: str) -> tuple[str, str]:
    """將 PIL 圖片調整大小、壓縮後儲存到指定目錄。

    壓縮策略：
      1. 縮放：將最長邊限制在 1568px（Anthropic Vision API 的最佳解析度）
               超過此尺寸再傳送不會提升模型的理解能力，卻會佔用更多 token
      2. 格式選擇（依透明度）：
         - 有 alpha 通道（RGBA、LA 或含 transparency 的 P 模式）→ WebP（lossless-ish，保留透明度）
         - 無 alpha → JPEG（對截圖類圖片壓縮率最佳）

    Args:
        img      : PIL.Image.Image 原始圖片物件
        save_dir : 儲存目標目錄（呼叫端負責確保目錄存在）
        name     : 不含副檔名的基礎檔名（通常為 UUID 字串）

    Returns:
        (final_path, mime_type)
          final_path : 儲存後的完整檔案路徑（含副檔名）
          mime_type  : 對應的 MIME type 字串（"image/webp" 或 "image/jpeg"）
    """
    from PIL import Image  # type: ignore[import-untyped]

    # 步驟 1：縮放，最長邊不超過 1568px（使用高品質 LANCZOS 重採樣算法）
    img.thumbnail((1568, 1568), Image.Resampling.LANCZOS)

    # 步驟 2：依透明度選擇格式
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )

    if has_alpha:
        # 有透明度：儲存為 WebP（保留 alpha 通道，quality=85 平衡品質與大小）
        final_path = os.path.join(save_dir, f"{name}.webp")
        img.save(final_path, "WEBP", quality=85)
        return final_path, "image/webp"
    else:
        # 無透明度：確保為 RGB 模式後儲存為 JPEG（optimize=True 啟用霍夫曼優化）
        if img.mode != "RGB":
            img = img.convert("RGB")
        final_path = os.path.join(save_dir, f"{name}.jpg")
        img.save(final_path, "JPEG", quality=85, optimize=True)
        return final_path, "image/jpeg"


# ── 檔案搜尋輔助函式 ───────────────────────────────────────────────────────────

# 遞迴搜尋時跳過的目錄名稱集合
# 這些目錄通常包含大量自動生成的檔案，不應出現在 @ 自動補全結果中
_EXCLUDE_DIRS = {
    ".git",  # Git 版本控制資料
    "__pycache__",  # Python 編譯快取
    "node_modules",  # Node.js 依賴套件
    ".venv",  # Python 虛擬環境（隱藏目錄命名慣例）
    "venv",  # Python 虛擬環境（常見命名慣例）
    ".tox",  # tox 測試環境
    "dist",  # 建置輸出目錄
    "build",  # 建置輸出目錄
}


def _search_files(query: str, root: str) -> list[tuple[str, str]]:
    """在 root 目錄下搜尋符合 query 的檔案，用於 @ 自動補全。

    搜尋邏輯（任一條件成立即匹配）：
      1. query 為空字串 → 列出所有檔案（最多 20 筆）
      2. query 是檔案名稱的子字串（大小寫不敏感）
      3. query 是相對路徑的子字串（大小寫不敏感）
      4. query 匹配 fnmatch 通配符模式（如 "*.py"）

    排序規則：
      - 檔案名稱以 query 開頭的排在前面（更精確的匹配優先）
      - 其餘按照相對路徑字母順序排列

    排除規則：
      - 路徑中任何一段落在 _EXCLUDE_DIRS 集合中的檔案會被跳過

    Args:
        query : 搜尋關鍵字（支援子字串和 fnmatch 通配符）
        root  : 搜尋根目錄的絕對路徑

    Returns:
        最多 20 筆 (display_name, insert_text) 元組的列表，
        兩個值均為相對路徑字串（顯示文字同時也是插入文字）
    """
    root_path = Path(root)
    results: list[tuple[str, str]] = []
    query_lower = query.lower()

    try:
        for p in root_path.rglob("*"):
            # 跳過路徑中含有排除目錄名的所有檔案
            if any(part in _EXCLUDE_DIRS for part in p.parts):
                continue
            if not p.is_file():
                continue
            name = p.name.lower()
            rel = str(p.relative_to(root_path))
            # 任一條件成立即納入結果
            if (
                not query_lower  # 空查詢：列出所有
                or query_lower in name  # 檔名子字串匹配
                or query_lower in rel.lower()  # 路徑子字串匹配
                or fnmatch.fnmatch(name, f"*{query_lower}*")  # 通配符匹配
            ):
                results.append((rel, rel))
                if len(results) >= 20:
                    break
    except PermissionError:
        pass  # 若無讀取權限則靜默跳過

    # 排序：名稱以 query 開頭的優先；其餘按路徑字母順序
    results.sort(
        key=lambda t: (not Path(t[0]).name.lower().startswith(query_lower), t[0])
    )
    return results[:20]


# @ 附件檔案的大小上限（100 KB）
# 超過此大小的檔案不讀取，避免傳送過大的 payload 給 WebSocket 伺服器
FILE_CONTENT_LIMIT = 100 * 1024  # 100 KB — files larger than this are skipped


def _extract_file_attachments(text: str) -> list[dict]:
    """從訊息文字中掃描 @路徑 參考，讀取並回傳對應的檔案內容。

    用途：
      當使用者在訊息中輸入 @bridge.py 或 @src/main.py 時，
      本函式會自動讀取這些檔案的內容，附加到傳送給伺服器的 payload 中。
      這讓 Agent 能夠直接看到被引用的程式碼，無需額外複製貼上。

    匹配規則：
      @路徑 中的路徑必須不含空白字元（@符號後到下一個空白/行尾為止）
      路徑以目前工作目錄（os.getcwd()）作為根目錄計算

    過濾條件（符合任一條件則跳過）：
      - 路徑不存在或不是檔案
      - 檔案大小超過 FILE_CONTENT_LIMIT（100 KB）
      - 讀取時發生 OSError（無讀取權限等）

    Args:
        text: 使用者輸入的訊息文字（可能含有多個 @路徑 參考）

    Returns:
        [{"path": 相對路徑, "content": 檔案文字內容}, ...] 的列表
        同一路徑最多出現一次（去重）；不可讀的路徑靜默略過
    """
    attachments = []
    seen: set[str] = set()  # 去重集合，避免同一檔案附加多次
    root = Path(os.getcwd())
    for match in re.finditer(r"@([^\s@]+)", text):
        rel = match.group(1)
        if rel in seen:
            continue
        seen.add(rel)
        candidate = root / rel
        if candidate.is_file():
            try:
                if candidate.stat().st_size > FILE_CONTENT_LIMIT:
                    continue  # 檔案過大，跳過
                content = candidate.read_text(encoding="utf-8", errors="replace")
                attachments.append({"path": rel, "content": content})
            except OSError:
                pass  # 讀取失敗（權限不足等），靜默略過
    return attachments


# ── 自訂 Textual 訊息類別 ──────────────────────────────────────────────────────


class ServerMessage(Message):
    """Textual 自訂訊息類別：將 WebSocket 收到的伺服器訊息封裝後傳遞到 UI 執行緒。

    設計說明：
      WebSocket 接收（_run_ws coroutine）和 UI 更新（on_server_message handler）
      執行在不同的 asyncio context 中。Textual 要求所有 UI 更新必須在主執行緒進行，
      因此使用 post_message() 將伺服器訊息排入 Textual 的訊息佇列，
      確保 UI 更新安全地發生在主迴圈中。

    Attributes:
        data: 從 WebSocket 接收並解析後的 JSON dict，包含 type 和各訊息特定欄位
    """

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data  # 伺服器傳來的 JSON dict（已解析）


# ── Textual Widget 元件 ────────────────────────────────────────────────────────


class ChatLog(RichLog):
    """可捲動的聊天訊息記錄視窗。

    繼承自 Textual 的 RichLog，並加入以下功能：
      - PageUp/PageDown/End 鍵盤快捷鍵（捲動半頁）
      - 智慧自動捲動：只有當視窗已捲到底部時，新訊息才自動捲到最新
      - 滑鼠拖曳選取文字並自動複製到剪貼簿

    智慧自動捲動邏輯（write_msg）：
      - 若使用者向上捲動中：不自動捲到最新（保持閱讀位置）
      - 若視窗在底部：新訊息到來時自動捲到最新（追蹤模式）
      這讓使用者在回顧歷史訊息時不會被新訊息打斷。
    """

    BINDINGS = [
        Binding("pageup", "scroll_page_up", "Page Up", show=False),
        Binding("pagedown", "scroll_page_down", "Page Down", show=False),
        Binding("end", "scroll_to_end", "End", show=False),
    ]

    def __init__(self, **kwargs):
        # highlight=False: 不做語法高亮（我們自己用 Rich Text 控制樣式）
        # markup=False: 不解析 Rich markup 字串（避免誤判 [ 字元）
        # wrap=True: 長行自動換行（適合聊天介面）
        # auto_scroll=False: 停用預設自動捲動（改用 write_msg 中的智慧邏輯）
        super().__init__(
            auto_scroll=False, highlight=False, markup=False, wrap=True, **kwargs
        )
        # Phase 1: visual row → plain text mapping
        # Each element: (message_index, char_offset_start, plain_text_of_this_visual_row)
        self._rendered_lines: list[tuple[int, int, str]] = []
        self._msg_index: int = 0  # monotonic counter for message_index

        # Phase 2: mouse selection state
        self._sel_start: tuple[int, int] | None = None  # (visual_row, col)
        self._sel_end: tuple[int, int] | None = None
        self._selecting: bool = False

    # ── Phase 1 helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _strip_rich_markup(content: "Text | str") -> str:
        """Strip Rich markup from content and return plain text.

        Since ChatLog uses markup=False, raw strings have no markup to strip
        — return them as-is. Rich Text objects use .plain which already strips styling.
        """
        if isinstance(content, str):
            return content  # markup=False: brackets are literal, not markup tags
        return content.plain

    def _wrap_plain_text(self, plain: str, width: int) -> list[str]:
        """Soft-wrap plain text into visual rows matching RichLog's own rendering.

        Uses Rich's Text.wrap() so word-break points are identical to what
        RichLog actually renders — preventing row-index mismatches on
        ASCII/mixed-language lines that break at word boundaries.
        """
        if width <= 0:
            return [plain] if plain else [""]
        if not plain:
            return [""]

        # Use Rich's own wrapping engine so our row count matches RichLog exactly.
        # Text.wrap() handles embedded newlines and CJK widths via wcwidth.
        from rich.text import Text as _RichText
        from rich.console import Console as _RichConsole

        t = _RichText(plain)
        con = _RichConsole(width=width, no_color=True, highlight=False)
        wrapped = t.wrap(con, width=width)
        result = [line.plain for line in wrapped]
        return result if result else [""]

    def _append_to_rendered_lines(self, content: "Text | str") -> None:
        """Append visual rows for a new message to _rendered_lines."""
        plain = self._strip_rich_markup(content)
        # Use the actual renderable-text width (excludes borders + vertical scrollbar).
        width = self.scrollable_content_region.width
        if width <= 0:
            width = 80  # fallback before layout
        visual_rows = self._wrap_plain_text(plain, width)
        msg_idx = self._msg_index
        self._msg_index += 1
        char_offset = 0
        for row_text in visual_rows:
            self._rendered_lines.append((msg_idx, char_offset, row_text))
            char_offset += len(row_text)

    def _rebuild_rendered_lines(self) -> None:
        """Rebuild _rendered_lines from scratch after a resize.

        Stores all messages as plain text in _msg_plain_cache, then re-wraps.
        Uses lazy rebuild: only rebuilds the cached messages.
        """
        # We rely on _msg_plain_cache populated during write_msg calls.
        if not hasattr(self, "_msg_plain_cache"):
            return
        self._rendered_lines = []
        width = self.scrollable_content_region.width
        if width <= 0:
            width = 80
        for msg_idx, plain in enumerate(self._msg_plain_cache):
            visual_rows = self._wrap_plain_text(plain, width)
            char_offset = 0
            for row_text in visual_rows:
                self._rendered_lines.append((msg_idx, char_offset, row_text))
                char_offset += len(row_text)

    def write_msg(self, content: "Text | str") -> None:
        """寫入訊息並依當前捲動位置決定是否自動捲到最新。

        Args:
            content: Rich Text 物件或純字串
        """
        # Phase 1: keep plain-text cache for resize rebuild
        plain = self._strip_rich_markup(content)
        if not hasattr(self, "_msg_plain_cache"):
            self._msg_plain_cache: list[str] = []
        self._msg_plain_cache.append(plain)

        # Phase 1: sync _rendered_lines
        self._append_to_rendered_lines(content)

        at_bottom = self.scroll_y >= self.max_scroll_y
        self.write(content, scroll_end=at_bottom)

    def on_resize(self, event: "events.Resize") -> None:  # noqa: ARG002
        """Rebuild _rendered_lines when the widget is resized (wrap width changes)."""
        # Invalidate any in-progress selection — row indices are now stale
        self._sel_start = None
        self._sel_end = None
        self._rebuild_rendered_lines()

    # ── Phase 2: mouse selection handlers ─────────────────────────────────────

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            row = self.scroll_offset.y + event.y
            self._sel_start = (row, event.x)
            self._sel_end = (row, event.x)
            self._selecting = True
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._selecting:
            row = self.scroll_offset.y + event.y
            new_end = (row, event.x)
            if new_end != self._sel_end:
                self._sel_end = new_end
                self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 1 and self._selecting:
            self._selecting = False
            self.release_mouse()
            selected = self._get_selected_text()
            if selected:
                self._copy_and_notify(selected)
            # Keep sel_start/sel_end so user can see the selection range

    def on_blur(self) -> None:
        """Safety release: force-release mouse capture if widget loses focus mid-drag."""
        if self._selecting:
            self._selecting = False
            self.release_mouse()
            self._sel_start = None
            self._sel_end = None

    # ── Phase 3: text extraction helpers ──────────────────────────────────────

    def _cell_to_char_index(self, text: str, cell_col: int) -> int:
        """Convert a terminal visual column offset to a Python string char index.

        Uses wcwidth for CJK-safe calculation (CJK chars occupy 2 cells but 1 char).
        """
        try:
            from wcwidth import wcwidth as _wcwidth  # type: ignore[import-not-found]

            # -1 = unprintable control char → treat as 1 cell
            # 0  = combining / zero-width char → truly 0 cells (attaches to prev)
            def _cw2(_c: str) -> int:
                w = _wcwidth(_c)
                return w if w >= 0 else 1
        except ImportError:

            def _cw2(_c: str) -> int:
                return 1  # type: ignore[misc]

        col = 0
        for i, ch in enumerate(text):
            w = _cw2(ch)
            if (
                col + w > cell_col
            ):  # cell_col falls inside this character → return its index
                return i
            col += w
        return len(text)

    def _get_visual_row_text(self, row: int) -> str:
        """Return the plain text for a given absolute visual row index.

        Returns empty string if row is out of bounds.
        """
        if not self._rendered_lines or row < 0 or row >= len(self._rendered_lines):
            return ""
        return self._rendered_lines[row][2]

    def _get_selected_text(self) -> str:
        """Extract plain text for the current selection range."""
        if self._sel_start is None or self._sel_end is None:
            return ""
        r0, c0 = min(self._sel_start, self._sel_end)
        r1, c1 = max(self._sel_start, self._sel_end)
        if r0 == r1:
            line_text = self._get_visual_row_text(r0)
            i0 = self._cell_to_char_index(line_text, c0)
            i1 = self._cell_to_char_index(line_text, c1)
            return line_text[i0:i1]
        else:
            parts: list[str] = []
            row0_text = self._get_visual_row_text(r0)
            parts.append(row0_text[self._cell_to_char_index(row0_text, c0) :])
            for r in range(r0 + 1, r1):
                parts.append(self._get_visual_row_text(r))
            row1_text = self._get_visual_row_text(r1)
            parts.append(row1_text[: self._cell_to_char_index(row1_text, c1)])
            return "\n".join(parts)

    # ── Phase 5: selection highlight rendering ────────────────────────────────

    def render_line(self, y: int) -> "Strip":  # type: ignore[override]
        """Overlay selection highlight on top of RichLog's normal rendering.

        Called once per visible row on each refresh().  We let the parent render
        the Rich-styled content, then splice a reverse-video highlight over the
        selected cell range so the user sees visual feedback while dragging.
        No full-widget redraws — only the affected rows are re-rendered.
        """
        from textual.strip import Strip  # local import avoids circular at module level

        strip: Strip = super().render_line(y)

        # No selection → nothing to overlay
        if self._sel_start is None or self._sel_end is None:
            return strip

        r0, c0 = min(self._sel_start, self._sel_end)
        r1, c1 = max(self._sel_start, self._sel_end)

        abs_row = self.scroll_offset.y + y

        # Row is outside the selection range
        if abs_row < r0 or abs_row > r1:
            return strip

        # Selection highlight: reverse-video is universally visible on any theme
        SEL_STYLE = Style(reverse=True)
        total_width = strip.cell_length

        if r0 == r1:
            # Single-row selection: highlight only the dragged column range
            hi_start = min(c0, c1)
            hi_end = max(c0, c1)
        elif abs_row == r0:
            # First row of multi-row selection: start col → end of row
            hi_start = c0
            hi_end = total_width
        elif abs_row == r1:
            # Last row: start of row → end col
            hi_start = 0
            hi_end = c1
        else:
            # Middle rows: highlight full row
            hi_start = 0
            hi_end = total_width

        # Clamp to actual content width and guard empty range
        hi_start = max(0, min(hi_start, total_width))
        hi_end = max(0, min(hi_end, total_width))
        if hi_start >= hi_end:
            return strip

        left = strip.crop(0, hi_start)
        mid = strip.crop(hi_start, hi_end).apply_style(SEL_STYLE)
        right = strip.crop(hi_end, total_width)
        return left + mid + right

    # ── Phase 4: clipboard + notification ─────────────────────────────────────

    def _copy_and_notify(self, text: str) -> None:
        """Copy text to clipboard via OSC 52 + pyperclip fallback, then show toast."""
        copied = False

        # Primary: Textual built-in OSC 52 (cross-platform, SSH-friendly)
        try:
            self.app.copy_to_clipboard(text)
            copied = True
        except Exception:
            pass

        # Fallback: pyperclip (for terminals that block OSC 52, e.g. Apple Terminal.app)
        if not copied:
            try:
                import pyperclip  # type: ignore[import-untyped]

                pyperclip.copy(text)
                copied = True
            except ImportError:
                self.app.notify(
                    "⚠ 複製失敗：請安裝 pyperclip（pip install pyperclip）",
                    severity="warning",
                    timeout=4,
                )
                return
            except Exception:
                self.app.notify(
                    "⚠ 複製失敗：剪貼簿寫入錯誤",
                    severity="warning",
                    timeout=4,
                )
                return

        if copied:
            self.app.notify("✓ 已複製", timeout=2)
            # Clear selection highlight after copy
            self._sel_start = None
            self._sel_end = None
            self.refresh()

    # ── Scroll / keyboard actions ──────────────────────────────────────────────

    def action_scroll_page_up(self) -> None:
        """向上捲動半頁（PageUp 快捷鍵觸發）。"""
        self.scroll_relative(y=-(self.size.height // 2), animate=False)

    def action_scroll_page_down(self) -> None:
        """向下捲動半頁（PageDown 快捷鍵觸發）。"""
        self.scroll_relative(y=(self.size.height // 2), animate=False)

    def action_scroll_to_end(self) -> None:
        """捲動到最底部（End 快捷鍵觸發）。"""
        self.scroll_end(animate=False)


class RoomHeader(Static):
    """固定在頂部的房間狀態標題列。

    顯示內容：
      - 房間名稱（Room ID）
      - 討論主題（Topic）
      - 各 Agent 的即時狀態：
        - 閒置中（● standby）：等待輪到自己
        - 思考中（⠋ spinner）：正在生成回覆
        - 工具呼叫（spinner + 工具名稱和輸入摘要）：正在執行工具

    Spinner 動畫：
      使用 10 幀的 Unicode braille 點字 spinner，
      每 0.1 秒更新一幀（由 Textual Timer 驅動）。
      當至少有一個 Agent 在思考時啟動，全部停止時停止，
      避免不必要的定時器佔用 CPU。

    CSS：
      深色背景（#0a1a0a）+ 橘色文字（#ffaa00）+ 橘色底線，
      形成清晰的視覺分隔，讓使用者一眼識別狀態區域。
    """

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

    # 10 幀 braille spinner，依序循環播放
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._room_id: str = ""  # 房間 ID
        self._topic: str = ""  # 討論主題
        self._agents: list = []  # 目前在房間內的 Agent 列表
        self._agent_status: dict[str, str] = {}  # agent_name → 狀態摘要字串
        self._agent_status_time: dict[str, str] = {}  # agent_name → 狀態更新時間戳
        self._thinking: set[str] = set()  # 目前正在思考的 Agent 名稱集合
        self._frame: int = 0  # 當前 spinner 幀索引
        self._timer: Timer | None = None  # Textual Timer 物件（None 表示停止中）
        # Owner typing state
        self._owner_name: str = ""  # owner 名稱
        self._owner_typing: bool = False  # owner 是否正在打字
        self._owner_status_time: str = ""  # owner 狀態時間戳
        self._observers: list[dict] = []  # 所有 observers 名單

    def update_info(
        self,
        room_id: str,
        topic: str,
        agents: list,
    ) -> None:
        """更新房間基本資訊並重新渲染標題。

        在以下時機呼叫：
          - 加入房間後（收到 joined 訊息）
          - Agent 加入或離開房間後

        Args:
            room_id : 房間 ID 字串
            topic   : 討論主題字串
            agents  : 目前在房間內的 Agent dict 列表，每個 dict 含 name/engine 等欄位
        """
        self._room_id = room_id
        self._topic = topic
        self._agents = agents
        self._refresh_display()

    def _set_status(self, agent_name: str, status: str) -> None:
        """更新 agent 狀態摘要並記錄當前時間戳（yyyy-dd-MM hh:mm:ss.zzz）。"""
        from datetime import datetime

        now = datetime.now()
        ms = now.microsecond // 1000
        self._agent_status[agent_name] = status
        self._agent_status_time[agent_name] = now.strftime(
            f"%Y-%d-%m %H:%M:%S.{ms:03d}"
        )

    def set_owner_info(self, owner_name: str) -> None:
        """設定 owner 名稱並初始化狀態為 idle。"""
        print(f"[DEBUG] set_owner_info called: {owner_name}")  # DEBUG
        self._owner_name = owner_name
        self._owner_typing = False
        self._update_owner_time()
        self._refresh_display()

    def set_observers(self, observers: list[dict]) -> None:
        """設定 observers 列表並重新渲染。"""
        print(f"[DEBUG] set_observers called: {observers}")  # DEBUG
        self._observers = observers
        self._refresh_display()

    def set_owner_typing(self, typing: bool) -> None:
        """設定 owner 是否正在打字。"""
        if self._owner_typing != typing:
            self._owner_typing = typing
            self._update_owner_time()
            # Start/stop spinner animation when owner types
            if typing and not self._timer:
                self._timer = self.set_interval(0.1, self._tick)
            elif not typing and not self._thinking and self._timer:
                self._timer.stop()
                self._timer = None
            self._refresh_display()

    def _update_owner_time(self) -> None:
        """更新 owner 狀態的時間戳。"""
        from datetime import datetime

        now = datetime.now()
        ms = now.microsecond // 1000
        self._owner_status_time = now.strftime(f"%Y-%d-%m %H:%M:%S.{ms:03d}")

    def start_thinking(self, agent_name: str, summary: str = "") -> None:
        """標記指定 Agent 開始思考，並啟動 spinner 動畫。

        若 spinner 計時器尚未啟動，則建立新的 0.1 秒間隔計時器。
        同一房間多個 Agent 同時思考時，共用同一個計時器。

        Args:
            agent_name : 開始思考的 Agent 名稱
            summary    : 初始狀態摘要（預設為 "thinking..."）
        """
        self._thinking.add(agent_name)
        self._set_status(agent_name, summary or "thinking...")
        if not self._timer:
            # 啟動 spinner 計時器：每 0.1 秒呼叫 _tick() 推進 spinner 幀
            self._timer = self.set_interval(0.1, self._tick)
        self._refresh_display()

    def stop_thinking(self, agent_name: str) -> None:
        """標記指定 Agent 停止思考，若所有 Agent 都停止則停止 spinner。

        Args:
            agent_name: 停止思考的 Agent 名稱
        """
        self._thinking.discard(agent_name)
        self._agent_status.pop(agent_name, None)
        if not self._thinking and self._timer:
            # 所有 Agent 都停止思考了，停止計時器節省資源
            self._timer.stop()
            self._timer = None
        self._refresh_display()

    def update_block(self, agent_name: str, blocks: list[dict]) -> None:
        """從最新的 agent_thinking 事件更新指定 Agent 的狀態摘要。

        從 blocks 列表的最後一個 block 決定顯示的狀態摘要，
        優先顯示最具體的資訊（工具名稱 > 工具結果 > 工具錯誤 > 文字 > 思考）：

          thinking   → "thinking..."（正在推理）
          tool_use   → "ToolName(input_preview)"（工具名稱 + 輸入前 18 字元）
          tool_result→ "ToolName:done(result_preview)"（工具完成 + 結果前 18 字元）
          tool_error → "ToolName:error(error_preview)"（工具失敗 + 錯誤前 18 字元）
          text       → "responding..."（正在輸出最終回覆）
          其他       → "thinking..."（未知 block 類型，使用預設）

        Args:
            agent_name : 需要更新狀態的 Agent 名稱
            blocks     : 從 agent_thinking 事件收到的 content block 列表
        """
        for block in reversed(blocks):
            btype = block.get("type", "")
            if btype == "thinking":
                self._set_status(agent_name, "thinking...")
                break
            elif btype == "tool_use":
                tool = block.get("tool", "?")
                inp = block.get("input", {})
                # 取工具輸入的第一個值作為摘要（最多 50 字元）
                first_val = str(next(iter(inp.values()), "")) if inp else ""
                self._set_status(agent_name, f"{tool}({first_val[:50]})")
                break
            elif btype == "tool_result":
                tool = block.get("tool", "?")
                result = block.get("result", "")
                self._set_status(agent_name, f"{tool}:done({result[:50]})")
                break
            elif btype == "tool_error":
                tool = block.get("tool", "?")
                error = block.get("error", "error")
                self._set_status(agent_name, f"{tool}:error({error[:50]})")
                break
            elif btype == "text":
                # Text block 表示 Agent 正在輸出最終回覆（而非推理中）
                self._set_status(agent_name, "responding...")
                break
            else:
                # 未知 block 類型：使用通用標籤，避免顯示殘留的舊狀態
                self._set_status(agent_name, "thinking...")
                break
        self._refresh_display()

    def _tick(self) -> None:
        """計時器回調：推進 spinner 幀索引並重新渲染。

        由 Textual Timer 每 0.1 秒呼叫一次。
        """
        self._frame += 1
        self._refresh_display()

    def _refresh_display(self) -> None:
        """重新渲染 RoomHeader 的顯示內容。

        組合以下資訊並更新 Static 文字：
          1. 第一行：房間名稱和討論主題
          2. 第二行起：每個 Agent 一行，顯示名稱、engine 標籤和狀態
             - 思考中：spinner + Agent 名稱 + engine + 狀態摘要
             - 閒置中：● + Agent 名稱 + engine + "standby"
             - 無 Agent：顯示等待訊息

        注意：此函式可能每 0.1 秒呼叫一次（spinner 動畫期間），
              請確保邏輯輕量，避免影響 TUI 整體響應速度。
        """
        print(f"[DEBUG] RoomHeader._refresh_display: agents = {self._agents}")  # DEBUG
        print(
            f"[DEBUG] _refresh_display: owner_name='{self._owner_name}', typing={self._owner_typing}"
        )  # DEBUG
        spin = self.SPINNER[self._frame % len(self.SPINNER)]
        lines = [f"OpenParty — Room: {self._room_id}, owner: {self._owner_name}, Topic: {self._topic}"]
        lines.append("\n")

        # Observers section (including owner)
        lines.append("Observers:")
        # 先顯示 owner（若存在）
        if self._owner_name:
            ts_part = f"{self._owner_status_time} " if self._owner_status_time else ""
            status_symbol = spin if self._owner_typing else "●"
            status_text = "typing..." if self._owner_typing else "idle"

            lines.append(
                f"   {ts_part}{status_symbol} {rich_escape("[owner]")} {self._owner_name}: {status_text}"
            )
        # 再顯示其他 non-owner observers
        if self._observers:
            for obs in self._observers:
                if obs.get("is_owner"):
                    continue  # owner 已經在上面顯示過
                obs_name = obs.get("name", "unknown")
                lines.append(f"   ● {rich_escape("[observer]")} {obs_name}: idle")

        lines.append("\n")

        # Agents section
        lines.append("Agents:")
        if self._agents:
            for a in self._agents:
                name = a["name"]
                engine = a.get("engine", "") or ""
                print(f"[DEBUG] agent={name}, engine='{engine}'")  # DEBUG
                if not engine:
                    print(f"[WARNING] Missing engine for agent: {name}")  # DEBUG
                engine_tag = f" [{engine}]" if engine else ""
                if name in self._thinking:
                    summary = self._agent_status.get(name, "") or "thinking..."
                    ts = self._agent_status_time.get(name, "")
                    ts_part = f"{ts} " if ts else ""
                    lines.append(f"   {ts_part}{spin} {name}{engine_tag}: {summary}")
                else:
                    ts = self._agent_status_time.get(name, "")
                    ts_part = f"{ts} " if ts else ""
                    lines.append(f"   {ts_part}● {name}{engine_tag}: standby")
        else:
            lines.append("   (waiting...)")
        self.update("\n".join(lines))


class CompletionList(Static):
    """自動補全下拉選單（顯示在輸入框上方）。

    支援三種補全類型（completing_type）：
      command  : /斜線命令（顯示命令名稱 + 說明文字）
      mention  : $/# 提及（顯示 Agent 名稱，無說明文字）
      file     : @檔案路徑（顯示相對路徑，無說明文字）

    視覺設計：
      - 目前選中項目：cyan 粗體 + ▶ 符號
      - 其他項目：dim 灰色
      - command 類型顯示補全說明（右側 dim 文字）
      - mention/file 類型只顯示名稱/路徑（更緊湊）

    顯示/隱藏：
      - show_items() 呼叫後顯示（styles.display = "block"）
      - hide() 呼叫後隱藏（styles.display = "none"）
      - 預設為隱藏狀態，只在有匹配項目時顯示
    """

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
        self.items: list[tuple[str, str]] = []  # 目前顯示的補全項目 [(值, 說明)]
        self.selected_idx: int = 0  # 當前選中項目的索引（0-based）
        self.completing_type: str = "command"  # 補全類型：command/mention/file
        self.styles.display = "none"  # 初始為隱藏狀態

    def show_items(self, items: list[tuple[str, str]], completing_type: str) -> None:
        """顯示補全選單並更新選項列表。

        Args:
            items          : 補全項目列表，每個元素為 (插入值, 說明文字)
            completing_type: 補全類型（影響視覺樣式）
        """
        self.items = items
        self.selected_idx = 0  # 每次重新顯示都重置到第一項
        self.completing_type = completing_type
        self.styles.display = "block"
        self._refresh()

    def hide(self) -> None:
        """隱藏補全選單並清空項目列表。"""
        self.items = []
        self.styles.display = "none"

    def move_up(self) -> None:
        """移動選中項目到上一項（不超過第一項）。"""
        if self.items:
            self.selected_idx = max(0, self.selected_idx - 1)
            self._refresh()

    def move_down(self) -> None:
        """移動選中項目到下一項（不超過最後一項）。"""
        if self.items:
            self.selected_idx = min(len(self.items) - 1, self.selected_idx + 1)
            self._refresh()

    def get_selected(self) -> "str | None":
        """取得當前選中項目的插入值。

        Returns:
            選中項目的插入文字；若列表為空則回傳 None
        """
        if not self.items:
            return None
        return self.items[self.selected_idx][0]

    def _refresh(self) -> None:
        """重新渲染補全選單的顯示內容。

        根據 completing_type 調整顯示格式：
          - command  : "▶ /cmd-name    說明文字"（選中）/ "   /cmd-name   說明文字"（未選）
          - mention  : "▶ $name"（選中）/ "name"（未選）
          - file     : "▶ path/to/file"（選中）/ "path/to/file"（未選）

        使用 Rich markup 語法進行樣式設定（Static.update() 接受 markup 字串）。
        注意：值和說明文字需要 rich_escape() 處理，避免 [ 字元被誤解為 markup。
        """
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
                elif self.completing_type == "file":
                    lines.append(f"[bold cyan]▶ {escaped_value}[/]")
                else:
                    # command：左側命令名稱（22 字元固定寬），右側 dim 說明
                    lines.append(
                        f"[bold cyan]▶  {escaped_value:<22} [dim]{escaped_desc}[/dim][/]"
                    )
            else:
                if self.completing_type in ("mention", "file"):
                    lines.append(f"[dim]{escaped_value}[/dim]")
                else:
                    lines.append(f"[dim]   {escaped_value:<22} {escaped_desc}[/dim]")
        self.update("\n".join(lines))


class StatusBar(Static):
    """底部單行狀態列：顯示當前 round 編號和思考/閒置狀態。

    視覺設計（雙色切換）：
      - 閒置時：橘色背景（#ffaa00）+ 深色文字（#0a1a0a）→ 亮眼提示「等待輸入」
      - 思考時：深色背景（#0a1a0a）+ 橘色文字（#ffaa00）→ 反轉配色表示「等待中」

    顯示格式：" Round N / state — display_name"
      例：" Round 3 / thinking — [owner] Andy"
          " idle / idle — [owner] Andy"（Round 0 時顯示 idle）
    """

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: #ffaa00;
        color: #0a1a0a;
    }
    """

    def __init__(self, owner: bool, display_name: str, **kwargs):
        """
        Args:
            owner        : 是否為房間擁有者（目前未使用，保留以備未來差異化顯示）
            display_name : 顯示在狀態列右側的使用者名稱
        """
        super().__init__("", **kwargs)
        self._display_name = display_name  # 顯示名稱（如 "[owner] Andy"）
        self._round: int = 0  # 當前 round 編號（0 = 尚未開始）
        self._is_thinking: bool = False  # 是否有 Agent 正在思考

    def on_mount(self) -> None:
        """Widget 掛載到 DOM 後的初始化渲染。"""
        self._refresh_display()

    def set_round(self, round_num: int) -> None:
        """更新當前 round 編號並重新渲染。

        Args:
            round_num: 新的 round 編號（從 turn_start/turn_end 事件取得）
        """
        self._round = round_num
        self._refresh_display()

    def set_thinking(self, thinking: bool) -> None:
        """切換思考/閒置狀態並更新背景顏色。

        思考時切換為深色背景（反轉配色），閒置時恢復橘色背景。

        Args:
            thinking: True = 有 Agent 正在思考；False = 所有 Agent 閒置
        """
        self._is_thinking = thinking
        if thinking:
            # 思考中：反轉配色（深色背景 + 橘色文字）
            self.styles.background = "#0a1a0a"
            self.styles.color = "#ffaa00"
        else:
            # 閒置中：標準配色（橘色背景 + 深色文字）
            self.styles.background = "#ffaa00"
            self.styles.color = "#0a1a0a"
        self._refresh_display()

    def _refresh_display(self) -> None:
        """重新渲染狀態列文字。"""
        round_str = f"Round {self._round}" if self._round > 0 else "idle"
        state = "thinking" if self._is_thinking else "idle"
        self.update(f" {round_str} / {state}  — {self._display_name}")


class MessageInput(TextArea):
    """多行文字輸入框：支援 Shift+Enter 換行、Enter 送出、圖片貼上和自動補全。

    繼承自 Textual TextArea，加入以下功能：

    1. 鍵盤行為覆蓋（on_key）：
       - Shift+Enter → 插入換行符（允許多行訊息）
       - Enter（無 Shift）→ 觸發 Submitted 訊息（送出）或完成自動補全
       - Up/Down/Escape/Tab → 在補全選單顯示時導航選項

    2. 圖片貼上攔截（on_paste）：
       - 偵測到剪貼簿有圖片時，攔截預設貼上行為
       - 自動壓縮圖片並加入 app._pending_images 佇列
       - 下次送出訊息時一併附帶圖片資料

    3. 相容 Input API：
       - 提供 .value 屬性（讀寫）讓現有程式碼無需修改
       - 提供 .cursor_position 屬性（讀寫）讓游標定位程式碼無需修改

    注意：圖片貼上功能僅對 owner（房間擁有者）啟用。
    """

    class Submitted(Message):
        """使用者按下 Enter 送出時觸發的 Textual 訊息。

        Attributes:
            input : 觸發此訊息的 MessageInput Widget 實例
            value : 送出時的文字內容（已包含換行符的多行字串）
        """

        def __init__(self, input: "MessageInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    @property
    def value(self) -> str:  # type: ignore[override]
        """取得輸入框的完整文字內容（與 Input.value 相容）。"""
        return self.text

    @value.setter
    def value(self, new: str) -> None:
        """設定輸入框的文字內容（清空後插入，與 Input.value 相容）。"""
        self.clear()
        self.insert(new)

    @property
    def cursor_position(self) -> int:  # type: ignore[override]
        """取得游標的字元偏移量（從文字開頭計算）。

        模擬 Input.cursor_position 的行為，將 (row, col) 二維位置
        轉換為線性字元偏移量。計算方式：
          pos = sum(各行長度 + 1 換行符) + 游標在當前行的偏移

        注意：這是近似計算，對 Unicode 多位元組字元可能有偏差。
        """
        row, col = self.cursor_location
        lines = self.text.split("\n")
        pos = sum(len(lines[i]) + 1 for i in range(row)) + col
        return pos

    @cursor_position.setter
    def cursor_position(self, pos: int) -> None:
        """設定游標到指定的字元偏移量位置。

        將線性字元偏移量轉換為 (row, col) 二維位置後呼叫 move_cursor()。

        Args:
            pos: 目標字元偏移量（從文字開頭計算，0-indexed）
        """
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

    def on_paste(self, event: events.Paste) -> None:
        """攔截貼上事件，偵測並處理剪貼簿圖片。

        執行流程：
          1. 確認是房間擁有者（非 owner 不處理圖片）
          2. 嘗試從 OS 剪貼簿取得圖片
          3. 若無圖片 → 讓預設文字貼上邏輯繼續執行
          4. 若有圖片：
             a. 阻止預設貼上行為（event.prevent_default()）
             b. 確保儲存目錄存在
             c. 檢查原始大小是否超過 5 MB 上限
             d. 壓縮並儲存圖片
             e. 再次確認壓縮後大小
             f. 加入 app._pending_images 佇列
             g. 在聊天訊息中顯示附件提示

        注意：此函式對文字貼上完全透明——只有確認存在圖片才會攔截。
        """
        app: OpenPartyApp = self.app  # type: ignore[assignment]
        # 只有擁有者才啟用圖片貼上功能
        if not getattr(app, "owner", False):
            return
        # 嘗試從 OS 剪貼簿取得圖片（非阻塞）
        img = _grab_clipboard_image()
        if img is None:
            # 無圖片：讓預設文字貼上繼續
            return
        # 有圖片：阻止預設貼上並接管處理
        event.prevent_default()
        event.stop()
        # 確保圖片暫存目錄存在
        os.makedirs(app._image_save_dir, exist_ok=True)
        # 初步大小檢查（使用原始 bytes，避免壓縮了超大圖片浪費時間）
        from io import BytesIO

        buf = BytesIO()
        img.save(buf, format=img.format or "PNG")
        raw_size = buf.tell()
        if raw_size > IMAGE_MAX_BYTES:
            app._chat(
                f"[error] 圖片太大（{raw_size // 1024 // 1024} MB），超過 5 MB 上限，已略過。"
            )
            return
        # 壓縮並儲存（縮放 + 格式轉換）
        name = str(uuid_mod.uuid4())
        try:
            final_path, mime = _save_clipboard_image(img, app._image_save_dir, name)
        except Exception as exc:
            app._chat(f"[error] 圖片儲存失敗：{exc}")
            return
        # 壓縮後再次確認大小（有些圖片壓縮後仍超標）
        final_size = os.path.getsize(final_path)
        if final_size > IMAGE_MAX_BYTES:
            os.unlink(final_path)
            app._chat(
                f"[error] 壓縮後圖片仍超過 5 MB（{final_size // 1024 // 1024} MB），已略過。"
            )
            return
        # 加入待發送佇列（送出下一則訊息時一併傳送）
        app._pending_images.append({"path": final_path, "mime": mime})
        fname = os.path.basename(final_path)
        kb = final_size // 1024
        app._chat(f"[🖼 {fname} ({kb} KB) 已附加，傳送訊息時一併送出]")

    def on_key(self, event: events.Key) -> None:
        """攔截鍵盤事件，處理送出、換行與補全導航。

        鍵盤邏輯（依優先順序）：
          Shift+Enter   → 插入換行符（多行訊息）
          Enter（無補全）→ 觸發 Submitted 訊息（送出訊息）
          Enter（有補全）→ 完成補全選取（_completion_enter）
          Up/Down（補全中）→ 移動補全選單游標
          Escape（補全中）→ 隱藏補全選單
          Tab（補全中）   → 填入選中項目（不立即送出）

        注意：只有在補全選單顯示時（_completing = True），
              Up/Down/Escape/Tab 才被攔截；否則保持預設行為。
        """
        app: OpenPartyApp = self.app  # type: ignore[assignment]

        # ── Shift+Enter → 換行 ────────────────────────────────────────────
        if event.key == "shift+enter":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return

        # ── Enter（無 Shift）→ 送出或完成補全 ────────────────────────────
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if getattr(app, "_completing", False):
                # 補全選單顯示中：完成選取
                app._completion_enter()
            else:
                # 一般狀態：送出訊息
                text = self.text
                self.post_message(self.Submitted(self, text))
            return

        # ── 補全導航（僅在補全選單顯示時生效）──────────────────────────
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


# ── Modal 對話框（彈出選擇視窗）─────────────────────────────────────────────────


class _PickerScreen(ModalScreen):
    """模態選擇視窗的基礎類別。

    提供統一的鍵盤操作（Escape 取消、Up/Down 移動、Enter 選取）
    和基本的 CSS 樣式（置中對話框、搜尋欄、列表）。

    子類別需實作：
      compose() → 定義視窗內容
      _do_pick() → 處理選取邏輯並呼叫 self.dismiss(result)

    使用方式（在 App 中）：
      result = await self.push_screen_wait(MyPickerScreen(items))
      # result 為選取的物件或 None（取消時）
    """

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
        """Escape 鍵：取消選取，回傳 None 給呼叫端。"""
        self.dismiss(None)

    def action_move_up(self) -> None:
        """向上鍵：移動 ListView 游標到上一項。"""
        self.query_one("#picker-list", ListView).action_cursor_up()

    def action_move_down(self) -> None:
        """向下鍵：移動 ListView 游標到下一項。"""
        self.query_one("#picker-list", ListView).action_cursor_down()

    def on_list_view_selected(self, _event: ListView.Selected) -> None:  # type: ignore[override]  # pyright: ignore[reportUnusedParameter]
        """ListView 選取事件（雙擊或 Enter）：委派給 _do_pick()。"""
        self._do_pick()

    def action_pick(self) -> None:
        """Enter 鍵動作：委派給 _do_pick()。"""
        self._do_pick()

    def _do_pick(self):
        """執行選取邏輯（子類別必須實作）。

        實作需呼叫 self.dismiss(result) 將結果回傳給呼叫端：
          self.dismiss(chosen_item)  # 選取了某項目
          self.dismiss(None)         # 取消或無有效選項
        """
        raise NotImplementedError


class ModelPickerScreen(_PickerScreen):
    """新增 Agent 時的 Model 選擇視窗。

    功能：
      - 顯示所有可用的 engine/model 組合
      - 支援即時搜尋過濾（輸入文字後動態更新列表）
      - 選取後回傳對應的 model dict 給呼叫端

    顯示格式：
      "[engine] provider - model_name"
      例：[claude] claude - Sonnet 4.6 · Best for everyday
          [opencode] zen - mimo-v2-pro-free

    列表標題格式：
      "選擇 Agent [N/Total]  ↑↓ Enter Esc"
      （N = 目前過濾後的數量，Total = 全部可用數量）
    """

    def __init__(self, items: list[dict], **kwargs):
        """
        Args:
            items: 可用的 model 列表，每個 dict 含 display/full_id/engine/base_name 欄位
        """
        super().__init__(**kwargs)
        self.all_items: list[dict] = items  # 未過濾的完整列表
        self.filtered: list[dict] = list(items)  # 當前過濾後的列表

    def compose(self) -> ComposeResult:
        """組合視窗 DOM：標題 + 搜尋欄 + 列表。"""
        with Vertical(id="picker-box"):
            yield Static(id="picker-title")
            yield Input(placeholder="/ 搜尋...", id="picker-search")
            yield ListView(id="picker-list")

    def on_mount(self) -> None:
        """掛載後：渲染初始列表並聚焦搜尋欄。"""
        self._refresh_list()
        self.query_one("#picker-search", Input).focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:  # type: ignore[override, reportUnusedParameter]
        """搜尋欄按 Enter：直接執行選取（方便快速選第一個結果）。"""
        self._do_pick()

    def on_input_changed(self, event: Input.Changed) -> None:
        """搜尋欄內容變更時：過濾列表並重新渲染。

        過濾規則（大小寫不敏感）：
          query 字串出現在 display 欄位中即匹配
          空字串時顯示所有項目
        """
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
        """重新渲染 ListView 和標題（依 self.filtered 更新）。"""
        lv = self.query_one("#picker-list", ListView)
        lv.clear()
        for item in self.filtered:
            engine = item.get("engine", "")
            # 顯示格式：[engine] display_name
            display_with_engine = (
                f"[{engine}] {item['display']}" if engine else item["display"]
            )
            lv.append(ListItem(Label(rich_escape(display_with_engine))))
        title = self.query_one("#picker-title", Static)
        title.update(
            f" 選擇 Agent [{len(self.filtered)}/{len(self.all_items)}]  ↑↓ Enter Esc "
        )

    def _do_pick(self) -> None:
        """取得 ListView 當前選中項目並 dismiss（回傳給呼叫端）。"""
        lv = self.query_one("#picker-list", ListView)
        idx = lv.index if lv.index is not None else 0  # 若無選中則預設第一項
        if self.filtered and 0 <= idx < len(self.filtered):
            self.dismiss(self.filtered[idx])
        else:
            self.dismiss(None)


class KickPickerScreen(_PickerScreen):
    """踢除成員時的 Agent 選擇視窗。

    功能：
      - 顯示目前在房間內的所有 Agent
      - 選取後回傳對應的 agent dict 給呼叫端

    顯示格式：
      "agent_name  (model_id)"
      例：claude-sonnet  (claude/claude-sonnet-4-6)

    注意：此視窗無搜尋欄（成員數量通常較少，不需要過濾）。
    """

    def __init__(self, agents: list[dict], **kwargs):
        """
        Args:
            agents: 可踢除的 Agent 列表，每個 dict 含 name/model 欄位
        """
        super().__init__(**kwargs)
        self.agents = agents

    def compose(self) -> ComposeResult:
        """組合視窗 DOM：標題 + 列表。"""
        with Vertical(id="picker-box"):
            yield Static(" 選擇踢除成員 (↑↓ Enter Esc) ", id="picker-title")
            yield ListView(id="picker-list")

    def on_mount(self) -> None:
        """掛載後：填入 Agent 列表並聚焦列表。"""
        lv = self.query_one("#picker-list", ListView)
        for a in self.agents:
            lv.append(ListItem(Label(f"{a['name']}  ({a.get('model', '?')})")))
        lv.focus()

    def _do_pick(self) -> None:
        """取得 ListView 當前選中項目並 dismiss（回傳給呼叫端）。"""
        lv = self.query_one("#picker-list", ListView)
        idx = lv.index
        if idx is not None and 0 <= idx < len(self.agents):
            self.dismiss(self.agents[idx])
        else:
            self.dismiss(None)


# ── 主應用程式 ─────────────────────────────────────────────────────────────────


class OpenPartyApp(App):
    """OpenParty TUI 主應用程式。

    整體架構：
      本類別是整個 TUI 的核心，負責：
        1. 建立並管理 UI 佈局（compose）
        2. 維護 WebSocket 連線到 OpenParty 伺服器（_run_ws）
        3. 處理伺服器訊息並更新 UI（on_server_message）
        4. 處理使用者輸入和命令（on_message_input_submitted、_handle_command）
        5. 管理自動補全狀態（_update_completion、_completion_enter、_completion_tab）
        6. 管理圖片暫存（_pending_images、_image_save_dir）

    UI 佈局（由上到下）：
      ┌─────────────────────────────────────────────┐
      │ RoomHeader                                  │ ← 房間資訊 + Agent 狀態
      ├─────────────────────────────────────────────┤
      │                                             │
      │ ChatLog                                     │ ← 聊天訊息（可捲動）
      │                                             │
      ├─────────────────────────────────────────────┤
      │ CompletionList (隱藏時不佔位)               │ ← 自動補全選單（彈出在輸入框上方）
      ├─────────────────────────────────────────────┤
      │ StatusBar                                   │ ← round 狀態列
      ├─────────────────────────────────────────────┤
      │ MessageInput (僅 owner 可見)                │ ← 訊息輸入框
      └─────────────────────────────────────────────┘

    狀態管理：
      self.agents         → 房間內的 Agent 列表（dict with name/model/engine/agent_id）
      self._thinking      → 正在思考中的 Agent 名稱集合
      self._turn_complete → 本輪已完成 turn 的 Agent 名稱集合（防止舊的 agent_thinking 更新頭部）
      self._topic         → 當前討論主題
      self._last_round    → 最後看到的 round 編號（用於插入 round 分隔線）

    模式：
      owner=True  : 擁有者模式，可輸入訊息、管理成員（顯示 MessageInput）
      owner=False : 觀察者模式，只能觀看（隱藏 MessageInput）
    """

    # 外部 CSS 檔案路徑（提供更複雜的樣式定義）
    CSS_PATH = "openparty.tcss"

    BINDINGS = [
        # Ctrl+C：強制退出（priority=True 確保即使子 Widget 擁有焦點也能觸發）
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
        """
        Args:
            room_id    : 要加入的房間 ID
            server_url : OpenParty WebSocket 伺服器 URL（如 ws://localhost:8765）
            name       : 使用者的顯示名稱（owner 模式下會加上 "[owner]" 前綴）
            owner      : True = 房間擁有者（可發言）；False = 只讀觀察者
        """
        super().__init__(**kwargs)
        self.room_id = room_id
        self.server_url = server_url
        self.owner_name = (
            name  # 注意：`name` 是 Textual App 的保留屬性，故用 owner_name
        )
        self.owner = owner  # 是否為房間擁有者
        self.ws = None  # WebSocket 連線物件（None = 尚未連線或已斷線）
        self.agents: list[dict] = []  # 目前在房間內的 Agent 列表
        self.available_engines: list[
            str
        ] = []  # 伺服器回報可用的 engine 列表（如 ["claude", "opencode"]）
        self._thinking: set[str] = set()  # 正在思考中的 Agent 名稱集合
        self._turn_complete: set[str] = (
            set()
        )  # 本輪已完成的 Agent（防止遲到的 agent_thinking 更新頭部）
        self._topic: str = ""  # 當前討論主題
        self._last_round: int = 0  # 最後渲染的 round 編號（用於插入分隔線）

        # ── 自動補全狀態 ──
        self._completing: bool = False  # 是否正在補全中
        self._completing_type: str = "command"  # 補全類型：command/mention/file
        self._completion_items: list[tuple[str, str]] = []  # 當前補全項目列表

        # ── 圖片貼上狀態 ──
        self._pending_images: list[
            dict
        ] = []  # 待傳送的圖片 [{"path": ..., "mime": ...}]
        # 圖片暫存目錄：使用 session UUID 避免多個 TUI 實例衝突
        self._session_id: str = str(uuid_mod.uuid4())[:8]
        self._image_save_dir: str = os.path.join(
            "/tmp", "openparty", "images", self._session_id
        )

    @property
    def display_name(self) -> str:
        """取得使用者的完整顯示名稱（擁有者加上 [owner] 前綴）。

        例：
          owner=True,  name="Andy" → "[owner] Andy"
          owner=False, name="Bob"  → "Bob"
        """
        return f"[owner] {self.owner_name}" if self.owner else self.owner_name

    def compose(self) -> ComposeResult:
        """定義 TUI 的 Widget 佈局（由 Textual 框架呼叫）。

        Widget 順序決定了視覺排列（上到下）：
          1. RoomHeader：固定頂部標題
          2. ChatLog：主要聊天區域（佔用大部分空間）
          3. CompletionList：補全選單（預設隱藏，顯示在輸入框上方）
          4. StatusBar：底部狀態列
          5. MessageInput：輸入框（僅 owner 渲染）
        """
        yield RoomHeader(id="room-header")
        yield ChatLog(id="chat")
        yield CompletionList()
        yield StatusBar(self.owner, self.display_name, id="round-status-bar")
        if self.owner:
            yield MessageInput(id="input")

    def on_mount(self) -> None:
        """App 掛載到終端後的初始化。

        啟動 WebSocket 連線任務（非同步，不阻塞 UI）。
        若為擁有者，聚焦輸入框以便立即輸入。
        """
        print(
            f"[DEBUG] on_mount: owner={self.owner}, owner_name={self.owner_name}"
        )  # DEBUG
        if self.owner:
            self.query_one("#input", MessageInput).focus()
            # Set owner info in header immediately
            header = self.query_one("#room-header", RoomHeader)
            header.set_owner_info(self.owner_name)
        asyncio.create_task(self._run_ws())

    def on_unmount(self) -> None:
        """App 卸載時的清理工作（使用者退出時呼叫）。

        刪除圖片暫存目錄，避免在 /tmp 留下殘留檔案。
        """
        if os.path.isdir(self._image_save_dir):
            try:
                shutil.rmtree(self._image_save_dir)
            except Exception:
                pass

    def _refresh_header(self) -> None:
        """更新 RoomHeader 顯示當前的 Agent 列表和討論主題。

        在以下時機呼叫：
          - 收到 joined 訊息後（初始化）
          - Agent 加入或離開房間後
        """
        header = self.query_one("#room-header", RoomHeader)
        header.update_info(
            self.room_id,
            self._topic,
            self.agents,
        )
        if self.owner and self.owner_name:
            header.set_owner_info(self.owner_name)

    # ── WebSocket 連線管理 ─────────────────────────────────────────────────────

    async def _run_ws(self) -> None:
        """建立 WebSocket 連線到 OpenParty 伺服器，並持續接收訊息。

        連線建立後的流程：
          1. 傳送 join 訊息（攜帶角色、房間 ID、名稱、是否擁有者）
          2. 進入訊息接收循環
          3. 每條訊息解析 JSON 後包裝成 ServerMessage 事件傳遞給 UI 執行緒

        錯誤處理：
          ConnectionRefusedError   → 伺服器未啟動，顯示錯誤提示
          ConnectionClosed         → 連線被伺服器關閉（正常關閉或超時）
          其他 Exception           → 顯示通用錯誤訊息

        注意：所有錯誤都是非致命的（只顯示訊息），不會讓 App 崩潰。
              連線結束後 self.ws 設為 None，使用者仍可繼續使用 TUI（只是無法傳送訊息）。
        """
        try:
            async with websockets.connect(self.server_url) as ws:
                self.ws = ws
                # 傳送加入訊息，告知伺服器本客戶端的身份
                await ws.send(
                    json.dumps(
                        {
                            "type": "join",
                            "role": "observer",  # 即使是 owner 也用 observer 角色（owner 透過 owner 欄位識別）
                            "room_id": self.room_id,
                            "name": self.display_name,
                            "owner": self.owner,
                        }
                    )
                )
                # 持續接收伺服器訊息
                async for raw in ws:
                    # 將 JSON 訊息包裝為 ServerMessage 事件，排入 Textual 訊息佇列
                    # 這確保 UI 更新在主執行緒中安全進行
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
            self.ws = None  # 清除連線物件，防止後續嘗試傳送訊息到已關閉的連線

    # ── 聊天輔助函式 ───────────────────────────────────────────────────────────

    def _chat(self, content: "Text | str") -> None:
        """將文字或 Rich Text 寫入聊天記錄視窗。

        這是所有訊息顯示的統一入口，其他方法都透過此函式寫入。

        Args:
            content: Rich Text 物件（保留樣式）或純字串（無樣式）
        """
        self.query_one("#chat", ChatLog).write_msg(content)

    def _print_message(self, entry: dict) -> None:
        """將伺服器訊息 entry dict 格式化後寫入聊天記錄。

        訊息格式（由上到下）：
          1. Round 分隔線（若 round 編號增加）：
             "────── Round N ──────"（深灰色，僅在 round > 0 時顯示）
          2. 空行（視覺分隔）
          3. 訊息標題：timestamp + sender + (model)
          4. 訊息正文（各行縮排 4 格）

        訊息類型的視覺樣式：
          私訊（is_private=True）   : 洋紅色標題 + 洋紅色正文 + 【私訊 → ...】標籤
          擁有者訊息（[owner] 前綴）: 黑底白字標題 + 黑底白字正文
          Agent 訊息               : Agent 顏色標題（輪轉色）+ dim 灰色正文

        正文中的 Markdown 格式（**bold**、*italic*、#name、$name）
        會透過 _parse_to_rich() 轉換為 Rich Text 樣式。

        Args:
            entry: 伺服器傳來的訊息 dict，包含：
                   name       : 發送者名稱
                   model      : 發送者使用的 model（可選）
                   content    : 訊息文字內容
                   is_private : 是否為私訊
                   private_to : 私訊接收者名稱列表
                   round      : 所屬的 round 編號
        """
        name = entry.get("name", "?")
        model = entry.get("model", "")
        content = entry.get("content", "")
        is_private = entry.get("is_private", False)
        private_to = entry.get("private_to", [])
        is_owner_msg = name.startswith("[owner]")

        # 插入 round 分隔線（當 round 編號增加時）
        msg_round = entry.get("round", 0)
        if msg_round > self._last_round:
            width = self.size.width or 80
            round_label = f" Round {msg_round} "
            side = max(2, (width - len(round_label)) // 2)
            divider = "─" * side + round_label + "─" * side
            if self._last_round > 0:
                # 只在第二個 round 開始後才插入分隔線（避免第一個 round 前有多餘分隔）
                self._chat(Text(divider, style=Style(color="#45505A", bold=True)))
            self._last_round = msg_round

        self._chat(Text(""))  # 空行（視覺分隔）

        # 組合訊息標題字串
        model_label = _model_label(model)
        ts = now()
        header_str = (
            f"  {ts}  {name}  ({model_label})" if model_label else f"  {ts}  {name}"
        )

        if is_private:
            # 私訊：洋紅色標題 + 私訊標籤 + 洋紅色正文
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
            # 擁有者訊息：黑底白字（高對比度，清楚標示擁有者的指示）
            self._chat(Text(header_str, style=OWNER_STYLE))
            for line in content.split("\n"):
                self._chat(_parse_to_rich(f"    {line}", OWNER_BODY))
        else:
            # Agent 訊息：使用 Agent 的輪轉顏色
            agent_st = _agent_style(name)
            header_text = Text(
                header_str, style=Style.combine([agent_st, Style(bold=True)])
            )
            self._chat(header_text)
            for line in content.split("\n"):
                self._chat(_parse_to_rich(f"    {line}", DIM_STYLE))

    # ── 伺服器訊息處理器 ───────────────────────────────────────────────────────

    def on_server_message(self, event: ServerMessage) -> None:
        """處理所有從 WebSocket 收到的伺服器訊息（在 Textual 主執行緒中執行）。

        訊息類型路由表：
          joined           → 初始化：設定 topic、agents、歷史訊息
          agent_joined     → 新 Agent 加入：更新 agents 列表、顯示通知
          agent_left       → Agent 離開：更新 agents 列表、清除思考狀態
          model_updated    → Agent 模型更新：更新 agents 列表中的 model 欄位
          system_message   → 系統訊息：黃色顯示
          waiting_for_owner→ 等待擁有者指令的通知：dim 顯示
          turn_start       → Agent 開始思考：啟動 spinner、更新狀態列
          turn_end         → Agent 完成回覆：停止 spinner、記錄延遲
          agent_thinking   → Agent 思考進度更新：更新標題列狀態摘要
          message          → 一般聊天訊息：格式化後顯示
          spawn_result     → 啟動 Agent 結果通知：成功/失敗訊息
          room_state       → 靜默忽略（目前無需處理）
          其他             → dim 顯示原始 JSON（偵錯用）
        """
        msg = event.data
        t = msg.get("type")

        if t == "joined":
            # 初始化：伺服器確認加入成功，並回傳當前房間狀態
            self.available_engines = msg.get("available_engines", [])
            state = msg.get("room_state", {})
            topic = state.get("topic", "(waiting for owner to set topic)")
            participants = state.get("participants", [])
            print(f"[DEBUG] joined: participants = {participants}")  # DEBUG
            self.agents = list(participants)
            self._topic = topic
            # 從 server 取得 owner 名字並設定到 RoomHeader
            owner_name_from_server = msg.get("owner_name", "")
            observers = state.get("observers", [])
            header = self.query_one("#room-header", RoomHeader)
            if owner_name_from_server:
                header.set_owner_info(owner_name_from_server)
            # 從 room_state 中取得 observers 列表並設定到 RoomHeader
            if observers:
                header.set_observers(observers)
            self._refresh_header()
            if self.owner:
                self._chat(
                    Text(
                        "  You are the room owner. Send a message to set the topic and start.",
                        style=GREEN_STYLE,
                    )
                )
            # 重播歷史訊息（若重新連線到有歷史的房間）
            history = msg.get("history", [])
            if history:
                self._chat(
                    Text(f"  [replaying {len(history)} messages]", style=DIM_STYLE)
                )
                for entry in history:
                    self._print_message(entry)

        elif t == "agent_joined":
            # 新 Agent 加入：顯示通知並更新 agents 列表
            agent_st = _agent_style(msg["name"])
            self._chat(
                Text(
                    f"{now()}  ++ {msg['name']} ({msg['model']}) joined", style=agent_st
                )
            )
            print(f"[DEBUG] agent_joined: msg = {msg}")  # DEBUG
            self.agents.append(
                {
                    "agent_id": msg.get("agent_id", msg["name"]),
                    "name": msg["name"],
                    "model": msg.get("model", ""),
                    "engine": msg.get("engine", ""),
                }
            )
            # 若 topic 在 agent_joined 訊息中更新，則同步更新本地 topic
            self._topic = msg.get("topic", "") or self._topic
            self._refresh_header()

        elif t == "agent_left":
            # Agent 離開：顯示通知、從列表移除、清除思考狀態
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
            # Agent 模型更新（橋接器偵測到實際模型版本後通知）
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
            # 系統訊息：黃色顯示（注意與一般訊息區分）
            self._chat(Text(f"{now()}  *** {msg.get('text', '')}", style=YELLOW_STYLE))

        elif t == "waiting_for_owner":
            # 伺服器通知：正在等待擁有者傳送下一個指令
            self._chat(
                Text(f"{now()}  [server] {msg.get('message', '')}", style=DIM_STYLE)
            )

        elif t == "turn_start":
            # Agent 開始思考：加入 thinking 集合、啟動 spinner、更新狀態列
            agent_name = msg["name"]
            self._thinking.add(agent_name)
            self._turn_complete.discard(
                agent_name
            )  # 重置「已完成」標記（新 turn 開始）
            header = self.query_one("#room-header", RoomHeader)
            # start_thinking() 內部已呼叫 _refresh_display()，無需額外呼叫 update_info()
            header.start_thinking(agent_name)
            self.query_one("#round-status-bar", StatusBar).set_thinking(True)

        elif t == "turn_end":
            # Agent 完成回覆：停止 spinner、記錄延遲、更新狀態列
            latency = msg.get("latency_ms", 0)
            self._chat(Text(f"  ({latency}ms)", style=DIM_STYLE))
            agent_name = msg.get("name", "")
            if agent_name:
                self._thinking.discard(agent_name)
                self._turn_complete.add(
                    agent_name
                )  # 標記已完成，防止遲到的 agent_thinking 更新頭部
                self.query_one("#room-header", RoomHeader).stop_thinking(agent_name)
            # 若所有 Agent 都完成了，狀態列切換回閒置
            if not self._thinking:
                self.query_one("#round-status-bar", StatusBar).set_thinking(False)

        elif t == "agent_thinking":
            # Agent 思考進度更新：更新標題列的狀態摘要
            agent_name = msg.get("name", "")
            # 防護：若 turn 已結束（turn_end 已收到），忽略遲到的 agent_thinking 訊息
            # 這避免了因網路延遲導致 agent_thinking 在 turn_end 之後到達的 race condition
            if agent_name in self._turn_complete:
                return
            blocks = msg.get("blocks", [])
            self.query_one("#room-header", RoomHeader).update_block(agent_name, blocks)

        elif t == "message":
            # 一般聊天訊息：格式化後寫入聊天記錄
            self._print_message(msg)

        elif t == "spawn_result":
            # 啟動 Agent 結果：成功（綠色）或失敗（紅色）
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
            # 更新 observers 列表（包含 owner 和其他 observer）
            owner_name = msg.get("owner_name", "")
            observers = msg.get("observers", [])
            header = self.query_one("#room-header", RoomHeader)
            if owner_name:
                header.set_owner_info(owner_name)
            # 設定其他 observers
            header.set_observers(observers)

        else:
            # 未知訊息類型：以 dim 樣式顯示原始 JSON（偵錯用，最多 80 字元）
            self._chat(Text(f"{now()}  [{t}] {json.dumps(msg)[:80]}", style=DIM_STYLE))

    # ── 輸入事件處理 ───────────────────────────────────────────────────────────

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """TextArea 內容變更時觸發（即時補全更新）。

        每當輸入框文字改變，取游標所在行（游標左側）的內容，
        傳給 _update_completion() 判斷是否需要顯示補全選單。

        設計說明：
          只取游標左側的文字（不含游標右側），這樣補全邏輯
          只關心使用者「正在輸入的部分」，而不是整行文字。
          支援多行訊息：游標可以在任意一行，補全在該行上運作。
        """
        if event.text_area.id == "input" and self.owner:
            inp = event.text_area
            row, col = inp.cursor_location
            buf = inp.text
            # 取游標所在行的游標左側文字
            current_line = buf.split("\n")[row][:col] if buf else ""
            self._update_completion(current_line)
            # Update owner typing status in header
            header = self.query_one("#room-header", RoomHeader)
            header.set_owner_typing(len(buf.strip()) > 0)

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """輸入框送出訊息時觸發（使用者按 Enter 送出）。

        流程：
          1. 確認是正確的輸入框且是擁有者
          2. 取得並清空輸入框文字
          3. 隱藏補全選單
          4. 若文字非空，在 Worker 中非同步執行命令處理

        使用 run_worker 的原因：
          _handle_command 是 async 函式，可能需要等待伺服器回應
          （如 /add-agent 需要取得模型列表）。使用 Worker 讓它在背景執行，
          不阻塞 UI 的主事件循環。
        """
        if event.input.id != "input" or not self.owner:
            return
        text = event.value.strip()
        inp = self.query_one("#input", MessageInput)
        inp.value = ""  # 立即清空輸入框（提供即時反饋）
        cl = self.query_one(CompletionList)
        cl.hide()
        self._completing = False
        # Reset owner typing status after sending
        if self.owner:
            header = self.query_one("#room-header", RoomHeader)
            header.set_owner_typing(False)
        if text:
            self.run_worker(self._handle_command(text), exclusive=False)

    # ── 自動補全邏輯 ───────────────────────────────────────────────────────────

    def _update_completion(self, buf: str) -> None:
        """根據輸入框游標左側的文字更新自動補全選單。

        觸發條件（依優先順序，第一個匹配者生效）：
          1. @ 開頭的路徑（@partial）→ 檔案路徑補全（_search_files）
          2. $/# 開頭的提及（$partial 或 #partial）→ Agent 名稱補全
          3. / 開頭的命令（/partial）→ 斜線命令補全

        若無匹配條件或補全結果為空，隱藏選單並重置補全狀態。

        注意：命令補全有特殊邏輯——若輸入的文字已完整匹配唯一的命令，
              不再顯示補全選單（避免已完成輸入的命令仍顯示補全提示）。

        Args:
            buf: 游標左側的文字（可能是當前行的部分或全部）
        """
        cl = self.query_one(CompletionList)

        # 優先級 1：@ 檔案路徑補全
        file_match = re.search(r"@([^\s]*)$", buf)
        if file_match:
            partial = file_match.group(1)
            matches = _search_files(partial, root=os.getcwd())
            if matches:
                self._completing = True
                self._completing_type = "file"
                self._completion_items = matches
                cl.show_items(matches, "file")
                return

        # 優先級 2：$/# Agent 名稱提及補全
        at_match = re.search(r"([#$])([\w\-]*)$", buf)
        if at_match:
            sigil = at_match.group(1)  # "$" 或 "#"
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

        # 優先級 3：/ 斜線命令補全
        if buf.startswith("/"):
            matches = [(cmd, desc) for cmd, desc in COMMANDS if cmd.startswith(buf)]
            # 若輸入完全匹配唯一命令（matches[0][0] == buf），不顯示補全（已完整）
            if matches and buf != matches[0][0]:
                self._completing = True
                self._completing_type = "command"
                self._completion_items = matches
                cl.show_items(matches, "command")
                return

        # 無匹配：隱藏補全選單
        self._completing = False
        cl.hide()

    def _completion_enter(self) -> None:
        """在補全選單顯示時按 Enter：選取當前項目。

        行為依補全類型而異：
          mention : 替換輸入框中的 $/# 提及前綴為完整名稱（加空格）
                    例："$claud" → "$claude-sonne " （游標移到末尾）
          file    : 替換游標所在行的 @partial 為完整路徑（加空格）
                    注意：只修改游標所在行，保持其他行不變
          command : 若命令需要引數（COMMANDS_WITH_ARGS）→ 填入命令等待引數
                    若命令不需引數 → 立即執行命令（等同直接輸入命令）
        """
        cl = self.query_one(CompletionList)
        selected = cl.get_selected()
        if selected is None:
            return
        inp = self.query_one("#input", MessageInput)
        cl.hide()
        self._completing = False

        if self._completing_type == "mention":
            # 替換輸入框末尾的 $/# 提及（任意 sigil + 名稱）為選中的完整名稱
            inp.value = re.sub(r"[#$][\w\-]*$", selected + " ", inp.value)
            inp.cursor_position = len(inp.value)
        elif self._completing_type == "file":
            # 只替換游標所在行的 @partial，保持其他行不變
            buf = inp.text
            lines = buf.split("\n")
            inp_obj = self.query_one("#input", MessageInput)
            row, col = inp_obj.cursor_location
            current_line = lines[row][:col]  # 游標左側
            suffix = lines[row][col:]  # 游標右側
            new_line = re.sub(r"@[^\s]*$", "@" + selected + " ", current_line)
            lines[row] = new_line + suffix
            inp.value = "\n".join(lines)
            inp_obj.move_cursor((row, len(new_line)))
        else:
            # 命令補全
            if selected in COMMANDS_WITH_ARGS:
                # 需要引數的命令：填入命令後加空格，等待使用者輸入引數
                inp.value = selected + " "
                inp.cursor_position = len(inp.value)
            else:
                # 不需引數的命令：清空輸入框並立即執行
                inp.value = ""
                self.run_worker(self._handle_command(selected.strip()), exclusive=False)

    def _completion_tab(self) -> None:
        """在補全選單顯示時按 Tab：填入選中項目（但不執行命令）。

        與 _completion_enter 的差異：
          Tab 永遠只填入文字，不立即執行命令。
          這符合命令列慣例：Tab 用於補全，Enter 才執行。

        行為依補全類型而異：
          mention : 同 _completion_enter（替換提及前綴）
          file    : 同 _completion_enter（替換 @partial）
          command : 填入命令名稱（有引數需求的加空格，否則也加空格）
                    注意：不執行命令，讓使用者繼續編輯
        """
        cl = self.query_one(CompletionList)
        selected = cl.get_selected()
        if selected is None:
            return
        inp = self.query_one("#input", MessageInput)
        cl.hide()
        self._completing = False

        if self._completing_type == "mention":
            inp.value = re.sub(r"[#$][\w\-]*$", selected + " ", inp.value)
            inp.cursor_position = len(inp.value)
        elif self._completing_type == "file":
            buf = inp.text
            lines = buf.split("\n")
            inp_obj = self.query_one("#input", MessageInput)
            row, col = inp_obj.cursor_location
            current_line = lines[row][:col]
            suffix = lines[row][col:]
            new_line = re.sub(r"@[^\s]*$", "@" + selected + " ", current_line)
            lines[row] = new_line + suffix
            inp.value = "\n".join(lines)
            inp_obj.move_cursor((row, len(new_line)))
        else:
            # 命令補全：Tab 只填入，不執行
            new_val = selected if selected not in COMMANDS_WITH_ARGS else selected + " "
            inp.value = new_val + " " if not new_val.endswith(" ") else new_val
            inp.cursor_position = len(inp.value)

    # ── 命令處理器 ─────────────────────────────────────────────────────────────

    async def _handle_command(self, text: str) -> None:
        """處理使用者輸入的命令或一般訊息。

        命令路由表：
          /leave          : 關閉 WebSocket 連線並退出 App
          /add-agent      : 顯示 Model 選擇視窗，選後通知伺服器啟動新 Agent
          /kick-all       : 踢除房間內所有 Agent
          /kick           : 顯示 Agent 選擇視窗，選後踢除指定 Agent
          /broadcast <msg>: 廣播訊息給所有 Agent（並行回答）
          其他文字        : 以一般訊息方式傳送（可附帶 @檔案 或剪貼簿圖片）

        一般訊息的附件處理：
          1. 在 executor 中解析 @路徑 參考（避免阻塞 UI 的 asyncio 事件迴圈）
          2. 若有 _pending_images（剪貼簿圖片），附加到 payload 並清空佇列

        Args:
            text: 使用者輸入的完整文字（已 strip()）
        """
        if not text:
            return

        if text == "/leave":
            # 優雅關閉：先關閉 WebSocket，再退出 App
            if self.ws is not None:
                await self.ws.close()
            self.exit()
            return

        if text == "/add-agent":
            # 顯示讀取中提示（非同步操作前給使用者反饋）
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
                return  # 使用者按 Escape 取消
            # 生成不重複的 Agent 名稱：從 base_name 開始，若衝突則加 -2、-3...
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
            # 通知伺服器啟動新 Agent（伺服器負責實際啟動 bridge.py 子行程）
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
            # 踢除所有 Agent：逐一傳送 kick_agent 訊息
            if not self.agents:
                self._chat(
                    Text(f"{now()}  [system] 目前沒有成員可踢除", style=YELLOW_STYLE)
                )
            else:
                if self.ws is not None:
                    for agent in list(self.agents):  # list() 複製以避免在迭代中修改
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
            # 踢除指定 Agent：顯示選擇視窗後傳送 kick_agent 訊息
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
                return  # 使用者按 Escape 取消
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
            # 廣播命令：取 "/broadcast " 之後的內容作為訊息
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

        # 一般訊息：解析 @路徑 附件，附加圖片，傳送給伺服器
        if self.ws is not None:
            payload: dict = {"type": "message", "content": text}
            # 在 executor 中執行檔案 I/O（避免阻塞 asyncio 事件迴圈）
            loop = asyncio.get_event_loop()
            files = await loop.run_in_executor(None, _extract_file_attachments, text)
            if files:
                payload["files"] = files
            # 附加剪貼簿圖片（若有）並清空佇列
            if self._pending_images:
                payload["images"] = list(self._pending_images)
                self._pending_images.clear()
            await self.ws.send(json.dumps(payload))


# ── 模型清單取得（頂層非同步函式）──────────────────────────────────────────────


async def _fetch_models(available_engines: list[str]) -> list[dict]:
    """根據伺服器回報的可用 engine 列表，取得所有可供選擇的 model 清單。

    資料來源：
      opencode engine: 向本機的 opencode serve HTTP API 查詢（GET /provider）
                       只列出「已連線」（connected）的 provider 下的 model
      claude engine  : 硬編碼三個預設 Claude model（Opus/Sonnet/Haiku）
                       無需 API 呼叫，直接回傳

    回傳格式（每個 model 一個 dict）：
      display   : 人類可讀的顯示名稱（如 "claude - Sonnet 4.6 · Best for everyday"）
      full_id   : 完整 model ID，格式 "provider/model"（如 "claude/claude-sonnet-4-6"）
      engine    : engine 類型（"opencode" 或 "claude"）
      base_name : 用於生成 Agent 名稱的基礎字串（最多 12 字元）

    Args:
        available_engines: 伺服器回報可用的 engine 列表（如 ["claude", "opencode"]）

    Returns:
        可用 model 的 dict 列表；若所有來源均失敗則回傳空列表
    """
    result = []

    # ── opencode engine：從本機 opencode serve 取得 provider/model 列表 ──
    if "opencode" in available_engines:
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"{OPENCODE_URL}/provider",
                    timeout=aiohttp.ClientTimeout(total=3),  # 3 秒超時，避免阻塞 UI
                ) as r:
                    data = await r.json() if r.status == 200 else {}
            # 只列出 connected 狀態的 provider（確保 API key 已設定）
            connected = set(data.get("connected", []))
            for provider in data.get("all", []):
                pid = provider.get("id", "")
                if pid not in connected:
                    continue  # 跳過未連線的 provider
                models = provider.get("models", {})
                items = models.values() if isinstance(models, dict) else models
                for m in items:
                    mid = m.get("id", "")
                    mname = m.get("name", mid)
                    # 從 model ID 提取基礎名稱：去除 provider 前綴和版本標籤
                    base_name = mid.split("/")[-1].split(":")[0] if mid else "agent"
                    result.append(
                        {
                            "display": f"{pid} - {mname}",
                            "full_id": f"{pid}/{mid}",
                            "engine": "opencode",
                            "base_name": base_name[
                                :12
                            ],  # 限制 12 字元（避免 Agent 名稱過長）
                        }
                    )
        except Exception:
            pass  # opencode serve 未啟動或無法連線，靜默忽略

    # ── claude engine：硬編碼三個預設 Claude model ──
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
                    "base_name": base,  # 例："claude-sonnet"（已設計為簡短的 Agent 名稱）
                }
            )

    return result


# ── 程式入口點 ─────────────────────────────────────────────────────────────────


def main() -> None:
    """OpenParty TUI 的程式入口點。

    解析命令列引數並啟動 Textual App。

    命令列引數：
      --room   : 要加入的房間 ID（預設 "debate-001"）
      --server : WebSocket 伺服器 URL（預設 ws://localhost:8765）
      --name   : 顯示名稱（預設 "Human"）
      --owner  : 以房間擁有者身份加入（加此旗標才能輸入訊息）

    使用範例：
      # 只讀觀察者（查看討論過程）
      python openparty_tui.py --room my-room

      # 房間擁有者（可發言、管理成員）
      python openparty_tui.py --room my-room --owner --name Andy
    """
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
