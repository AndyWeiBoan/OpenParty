"""
OpenParty Agent Bridge
======================
將一個 Claude Agent SDK 實例連接到 OpenParty Room（多 agent 討論房間）的橋接器。

為什麼不用 MCP？
  MCP 在長時間等待 wait_for_turn 呼叫時會超時。本橋接器改用 WebSocket 長連線，
  可以無限期阻塞等待 your_turn 訊號，不受 HTTP 請求超時限制。

主要工作流程（單次 turn）：
  1. 透過 WebSocket 連線到 OpenParty 伺服器
  2. 阻塞等待伺服器送來 your_turn 訊號（最多 600 秒）
  3. 將 your_turn payload 轉換為 Claude 可理解的 prompt
  4. 呼叫 Claude Agent SDK（或 opencode serve HTTP API）取得回覆
  5. 將回覆透過 WebSocket 送回房間
  6. 重複步驟 2–5

支援兩種 engine：
  - claude  : 使用 claude_agent_sdk 的 query() 函式，支援工具呼叫、思考流、圖片
  - opencode: 使用 opencode serve 的 HTTP API，透過 SSE 串流展示思考過程

Usage:
    .venv/bin/python bridge.py --room test-001 --name Claude01
    .venv/bin/python bridge.py --room test-001 --name Claude02 --max-turns 10
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import websockets
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage
from claude_agent_sdk import ThinkingConfigEnabled, ProcessError
from claude_agent_sdk import ThinkingBlock, TextBlock, ToolUseBlock

# ── Event schema dataclasses ────────────────────────────────────────────────────
# 以下 dataclass 定義了透過 WebSocket 廣播給觀察者（observer）的「agent_thinking」事件結構。
# 這些事件在 UI 端會以視覺化方式呈現 agent 的思考/工具呼叫過程。


@dataclass
class AgentThinkingBlock:
    """agent_thinking 事件中的「思考」內容區塊。

    用於展示 Claude 的 extended thinking（內部推理）文字。
    對應 Claude API 的 ThinkingBlock，但只保留 type 與 text 兩個欄位，
    方便序列化為 JSON 後傳給前端。
    """

    type: str = "thinking"
    text: str = ""


@dataclass
class AgentToolUseBlock:
    """agent_thinking 事件中的「工具呼叫」內容區塊。

    當 agent 呼叫某個工具（如 Bash、Read、WebSearch）時，
    橋接器會建立此 block 並廣播給觀察者，讓 UI 即時顯示工具名稱與輸入參數。
    """

    type: str = "tool_use"
    tool: str = ""  # 工具名稱，如 "Bash"、"Read"
    input: dict = field(default_factory=dict)  # 工具的輸入參數（dict 格式）


@dataclass
class AgentTextBlock:
    """agent_thinking 事件中的「文字」內容區塊。

    當 agent 在思考過程中累積出文字回應時（非最終結果），
    橋接器會用此 block 讓 UI 顯示「正在回應...」的狀態。
    """

    type: str = "text"
    text: str = ""


@dataclass
class AgentThinkingEvent:
    """透過 WebSocket 廣播給觀察者的 agent_thinking 事件 wire-format。

    欄位說明：
      type     : 固定為 "agent_thinking"，讓伺服器識別事件種類
      agent_id : 8 字元 UUID 前綴，用於區分同一房間內的不同 agent
      name     : agent 的顯示名稱（如 "claude-sonne"）
      turn     : 當前 turn 編號（橋接器送出時留 0；伺服器注入實際值後廣播）
      blocks   : 內容區塊列表，可包含 thinking / tool_use / text 等類型

    注意：橋接器送出時不填 turn，由伺服器在廣播前自動補上。
    """

    type: str = "agent_thinking"
    agent_id: str = ""
    name: str = ""
    turn: int = 0
    blocks: list = field(default_factory=list)


# ── 日誌設定 ────────────────────────────────────────────────────────────────────
# 統一格式：時:分:秒 [BRIDGE <name>] <訊息>
# 例：12:34:56 [BRIDGE claude-sonne] My turn!
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BRIDGE %(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("claude_agent_sdk._internal.transport.subprocess_cli").setLevel(logging.DEBUG)


def make_logger(name: str) -> logging.Logger:
    """建立以 agent name 為識別碼的 logger，方便在多 agent 場景下區分日誌來源。"""
    return logging.getLogger(name)


# opencode serve 預設監聽的本機位址與 port
OPENCODE_URL = "http://127.0.0.1:4096"

# ── OpenCode HTTP client ────────────────────────────────────────────────────────

# 全域變數：保存 opencode serve 子行程的參考，方便後續管理其生命週期
_opencode_server_proc: Optional[asyncio.subprocess.Process] = None


async def ensure_opencode_server(url: str = OPENCODE_URL) -> bool:
    """確保 opencode serve 正在執行；若尚未啟動則自動啟動。

    流程：
      第一次嘗試：直接 GET /global/health，若回傳 200 表示已在執行。
      若失敗：在背景啟動 `opencode serve --port 4096`，等待 3 秒後再次健康檢查。
      兩次嘗試均失敗則回傳 False，呼叫端應中止後續操作。

    Args:
        url: opencode serve 的基底 URL，預設為 http://127.0.0.1:4096

    Returns:
        True  → opencode serve 已就緒，可接受請求
        False → 無法啟動，呼叫端應視為致命錯誤並退出
    """
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
            pass  # 連線失敗或超時，繼續下一步

        if attempt == 0:
            # 第一次健康檢查失敗：嘗試在背景啟動 opencode serve
            log.info("Starting opencode serve on port 4096...")
            _opencode_server_proc = await asyncio.create_subprocess_exec(
                "opencode",
                "serve",
                "--port",
                "4096",
                stdout=asyncio.subprocess.DEVNULL,  # 忽略 stdout，避免干擾日誌
                stderr=asyncio.subprocess.DEVNULL,  # 忽略 stderr
            )
            await asyncio.sleep(3)  # 等待伺服器完成啟動

    log.error("opencode serve failed to start")
    return False


# opencode engine 完成時合法的 finish 值（白名單排除）
# "tool-calls" → engine 還在跑（需要執行工具）
# "error"       → engine 因錯誤中止，不應視為正常結果
_FINISH_BLOCKED: frozenset[str] = frozenset({"tool-calls", "error"})


class OpenCodeClient:
    """opencode serve HTTP API 的輕量非同步客戶端。

    opencode serve 是一個本機 HTTP 伺服器，提供以下主要端點：
      POST /session                    → 建立新的對話 session
      POST /session/{id}/message       → 傳送訊息並阻塞等待完整回覆（同步）
      POST /session/{id}/prompt_async  → 非阻塞提交，立刻返回 204，engine 在背景執行
      GET  /session/{id}/message       → 查詢 session 所有訊息（持久化儲存）
      GET  /provider                   → 列出可用的 provider 與 model
      GET  /event                      → SSE 串流，即時推送思考/工具呼叫事件

    本類別負責管理 session 的生命週期，並將 prompt 轉換為 opencode 的請求格式。
    """

    def __init__(self, url: str, model: str, name: str):
        """
        Args:
            url  : opencode serve 基底 URL（如 http://127.0.0.1:4096）
            model: 模型識別字串，格式為 "providerID/modelID"（如 "zen/mimo-v2-pro-free"）
            name : agent 顯示名稱，用於日誌識別
        """
        self.url = url
        self.model = model  # e.g. "zen/mimo-v2-pro-free"
        self.name = name
        self.session_id: Optional[str] = None  # None 表示尚未建立 session
        self.log = logging.getLogger(f"opencode:{name}")

    async def create_session(self) -> str:
        """在 opencode serve 建立新的對話 session，回傳 session ID。

        每次啟動橋接器或需要全新對話上下文時呼叫。
        Session ID 後續用於將訊息傳送到正確的對話串。
        """
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{self.url}/session", json={}) as r:
                data = await r.json()
                sid = data["id"]
                self.log.info(f"OpenCode session created: {sid}")
                return sid

    def _build_body(self, prompt: str) -> dict:
        """將純文字 prompt 轉換為 opencode /session/{id}/message 端點所需的 JSON body。

        格式說明：
          parts: 訊息內容，目前只支援單一 text part
          model: 可選，指定使用的 provider/model
                 格式為 {providerID: "...", modelID: "..."}
                 若 model 字串含有 "/" 則自動拆分；否則 providerID 預設為 "opencode"

        Args:
            prompt: 純文字 prompt 字串

        Returns:
            符合 opencode API 格式的 dict，可直接序列化為 JSON
        """
        body: dict = {
            "parts": [{"type": "text", "text": prompt}],
        }
        if self.model:
            parts = self.model.split("/", 1)
            if len(parts) == 2:
                # "zen/mimo-v2-pro-free" → providerID="zen", modelID="mimo-v2-pro-free"
                body["model"] = {"providerID": parts[0], "modelID": parts[1]}
            else:
                # 無斜線則使用 "opencode" 作為 providerID
                body["model"] = {"providerID": "opencode", "modelID": self.model}
        return body

    async def _post_message(self, body: dict) -> str:
        """實際執行 POST /session/{id}/message 請求，等待並回傳最終文字回覆。

        opencode serve 在模型生成完畢後才回傳整個回應（非串流），
        因此這是一個「阻塞式」呼叫，超時設定為 120 秒。

        回傳邏輯：
          - 從 response body 的 parts 陣列中，取出所有 type 為 "text" 或 "text-part" 的內容
          - 用換行符號合併後回傳

        Args:
            body: 由 _build_body() 建立的請求 body dict

        Returns:
            模型的文字回覆，失敗時回傳 "(opencode error ...)" 錯誤訊息
        """
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{self.url}/session/{self.session_id}/message",
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=120, sock_read=120),
                ) as r:
                    if r.status != 200:
                        text = await r.text()
                        self.log.error(f"HTTP {r.status}: {text}")
                        return f"(opencode error {r.status})"
                    data = await r.json()

            # 從回應的 parts 陣列提取文字內容
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

    async def submit_async(self, body: dict) -> bool:
        """非阻塞地提交 prompt 給 opencode serve。

        使用 POST /session/{id}/prompt_async 端點，伺服器立刻返回 204，
        engine 在背景執行。Client 應透過 SSE 事件或 GET /session/{id}/message 取得結果。

        Args:
            body: 由 _build_body() 建立的請求 body dict

        Returns:
            True → 提交成功（HTTP 204）；False → 提交失敗
        """
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{self.url}/session/{self.session_id}/prompt_async",
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status not in (200, 204):
                        text = await r.text()
                        self.log.error(f"prompt_async HTTP {r.status}: {text}")
                        return False
                    self.log.info(f"prompt_async accepted: HTTP {r.status}")
                    return True
        except Exception as e:
            self.log.error(f"prompt_async failed: {e}", exc_info=True)
            return False

    async def abort_session(self) -> None:
        """中止當前 session 中正在執行的 engine。

        呼叫 POST /session/{id}/abort，讓 opencode server 停止 AI 處理。
        通常在 SSE timeout 後呼叫，確保背景 engine 不繼續消耗資源，
        也防止殘留任務與下次提交的新任務並行執行。
        """
        if not self.session_id:
            return
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{self.url}/session/{self.session_id}/abort",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    self.log.info(f"abort_session: HTTP {r.status}")
        except Exception as e:
            self.log.warning(f"abort_session failed (non-fatal): {e}")

    async def get_messages(self) -> str:
        """從 GET /session/{id}/message 查詢最新已完成的 assistant 訊息文字。

        opencode session 是持久化的，即使 SSE/HTTP 連線中斷，
        engine 跑完的結果仍存在 database 中，可隨時查詢。

        回傳邏輯：
          - 找最後一筆 role=assistant 且 finish 已設定（且非 "tool-calls"）的訊息
          - 提取其 text parts 並合併回傳
          - 若無已完成的訊息（engine 仍在執行）則回傳空字串

        Returns:
            已完成 assistant 訊息的文字內容；若無則回傳空字串
        """
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"{self.url}/session/{self.session_id}/message",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        self.log.error(f"GET /message HTTP {r.status}")
                        return ""
                    messages = await r.json()

            # 找最後一筆 role=assistant 且有完成 finish（非 tool-calls / error）的訊息
            last_done = None
            for msg in messages:
                info = msg.get("info", {})
                finish = info.get("finish", "")
                if (
                    info.get("role") == "assistant"
                    and finish
                    and finish not in _FINISH_BLOCKED
                ):
                    last_done = msg

            if not last_done:
                self.log.info("GET /message: no completed assistant message found")
                return ""

            # 提取 text parts
            parts = last_done.get("parts", [])
            texts = [
                p.get("text", "")
                for p in parts
                if p.get("type") in ("text", "text-part")
            ]
            result = "\n".join(t for t in texts if t).strip()
            self.log.info(f"GET /message fallback: got {len(result)} chars")
            return result

        except Exception as e:
            self.log.error(f"GET /message failed: {e}", exc_info=True)
            return ""

    async def call(self, prompt: str) -> str:
        """高階介面：傳送 prompt 給 opencode serve，回傳最終文字回覆。

        若尚未建立 session，會先自動建立。
        這是「無思考串流」版本，適合簡單場景；
        需要思考串流時應改用 AgentBridge._call_opencode_with_thinking()。

        Args:
            prompt: 純文字 prompt

        Returns:
            模型的文字回覆字串
        """
        if not self.session_id:
            self.session_id = await self.create_session()
        body = self._build_body(prompt)
        return await self._post_message(body)

    @staticmethod
    async def list_models(url: str = OPENCODE_URL) -> list[dict]:
        """從 opencode serve 的 /provider 端點取得所有可用的 provider 與 model 清單。

        回傳格式（每個 model 一個 dict）：
          provider : provider 識別碼（如 "openai"、"anthropic"、"zen"）
          model    : model 識別碼（如 "gpt-4o"）
          display  : 人類可讀的顯示名稱（如 "openai - GPT-4o"）
          full_id  : 完整識別字串，格式 "provider/model"（如 "openai/gpt-4o"）

        網路失敗或 opencode 未啟動時安靜回傳空列表。

        Args:
            url: opencode serve 基底 URL

        Returns:
            model 資訊 dict 的列表，失敗時回傳 []
        """
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
                # models 可能是 dict（key 為 modelID）或 list，統一轉換為可迭代物件
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
    """不可恢復的 provider 錯誤（如速率限制、認證失敗、配額耗盡等）。

    拋出此例外時，agent 應主動離開房間而非無限重試，
    避免佔用 turn slot 並讓其他 agent 繼續運作。

    典型觸發情境：
      - Claude API 回傳 is_error=True 的 ResultMessage
      - 認證 token 失效
      - 模型配額超過
    """

    pass


# ── Prompt builder ─────────────────────────────────────────────────────────────


def build_room_rules_prompt(owner_name: str = "") -> str:
    """組裝 agent 在房間內必須遵守的角色規則，作為 system prompt 注入。

    這些規則定義 agent 在多人討論房間中的行為準則：
      - 對 owner 絕對服從
      - 對其他 agent 保持獨立立場
      - 回覆簡潔（2-4 句）
      - 不得空白回覆
      - 必須經 owner 批准才能離開

    Args:
        owner_name: 房間擁有者的顯示名稱；若為空則使用通用稱呼「the room owner」

    Returns:
        格式化好的角色規則字串，可直接傳入 ClaudeAgentOptions(system_prompt=...)
    """
    owner_ref = f"the room owner ({owner_name})" if owner_name else "the room owner"
    return (
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
        "5. You may only leave this room after the room owner explicitly approves. "
        "To confirm your exit, reply with the exact phrase 'I want to leave'.\n"
        "6. You MUST always produce a non-empty reply. Even if you have nothing substantive to add, "
        "reply with a brief acknowledgment or observation. Never return an empty response."
    )


def build_prompt(
    your_turn_payload: dict,
    agent_name: str,
    owner_name: str = "",
    session_id: str | None = None,
) -> "str | list[dict]":
    """將 WebSocket 的 your_turn payload 轉換為可傳給 Claude Agent SDK 的 prompt。

    歷史訊息策略：
      伺服器已在 payload 的 history 欄位中包含正確的「回合窗口」
      （上一回合 + 本回合目前為止的訊息），橋接器直接原樣使用，
      不做額外截斷或過濾，確保每個 agent 都能看到完整的討論上下文。

    Prompt 結構（由上到下）：
      1. agent 角色宣告（你是誰、在什麼房間）
      2. 參與者名單
      3. 討論主題
      4. 當前是第幾個 turn
      5. 最近的對話歷史（逐條列出 speaker: content）
      6. 額外指令提示（若有且不與主題重複）

    注意：角色規則（Your role and rules）已移至 build_room_rules_prompt()，
    由 _call_claude() 透過 ClaudeAgentOptions(system_prompt=...) 注入，
    不再放在 user prompt 中。

    圖片支援（multipart prompt）：
      若 your_turn_payload 包含 image_blocks（由 TUI 透過剪貼簿貼上並傳送），
      本函式會回傳 list[dict] 而非純字串。
      格式：[...image_blocks, {"type": "text", "text": text_prompt}]
      呼叫端（_call_claude）需識別此格式並以 multipart user message 方式傳給 SDK。

    Args:
        your_turn_payload : WebSocket 收到的 your_turn 訊息 dict
        agent_name        : 本 agent 的顯示名稱（用於 prompt 中的自我介紹）
        owner_name        : 房間擁有者的顯示名稱（保留參數，目前未使用）
        session_id        : 目前的 Claude session ID（預留欄位，目前未在 prompt 中使用）

    Returns:
        純文字 prompt（str）：無圖片時
        內容區塊列表（list[dict]）：有 image_blocks 時（multipart prompt）
    """
    # 從 payload 解包各欄位
    history = your_turn_payload.get("history", [])
    context = your_turn_payload.get("context", {})
    prompt_hint = your_turn_payload.get("prompt", "")

    topic = context.get("topic", "")
    participants = context.get("participants", [])
    total_turns = context.get("total_turns", 0)

    # 直接使用伺服器提供的完整歷史窗口（不截斷）
    history_window = history

    lines = []
    lines.append(
        f"You are {agent_name}, participating in a multi-agent discussion room."
    )
    lines.append("")

    # 列出所有參與者名稱
    if participants:
        names = [p["name"] for p in participants]
        lines.append(f"Participants in this room: {', '.join(names)}")

    if topic:
        lines.append(f"Discussion topic: {topic}")

    # turn 編號從 0 開始計算，顯示時加 1 變成人類可讀的序號
    lines.append(f"This is turn #{total_turns + 1}.")
    lines.append("")

    # 插入歷史訊息，格式：「  speaker: content」
    if history_window:
        lines.append("Recent conversation:")
        for entry in history_window:
            speaker = entry.get("name", "?")
            content = entry.get("content", "")
            lines.append(f"  {speaker}: {content}")
        lines.append("")

    # 若有額外指令且不與主題重複，則附上
    if prompt_hint and prompt_hint != topic:
        lines.append(f"Instruction: {prompt_hint}")
        lines.append("")

    text_prompt = "\n".join(lines)

    # ── 圖片支援：若 payload 含有 image_blocks，回傳 multipart 格式 ──
    # image_blocks 是一個 list[dict]，每個元素為 Anthropic image content block：
    #   {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}}
    # 這些 block 由伺服器端從上傳的圖片資料組裝，並附在 your_turn payload 中傳給橋接器。
    # 圖片放在文字之前，符合 Claude API 的最佳實踐（先看圖再看文字指令）。
    image_blocks = your_turn_payload.get("image_blocks", [])
    if image_blocks:
        # 回傳 multipart 列表：圖片區塊 + 文字區塊
        return image_blocks + [{"type": "text", "text": text_prompt}]

    return text_prompt


# ── Bridge ─────────────────────────────────────────────────────────────────────


class AgentBridge:
    """OpenParty Agent 橋接器的核心類別。

    負責管理整個 agent 的生命週期：
      - 連線與加入房間
      - 輪流等待（your_turn）
      - 呼叫 AI engine（Claude SDK 或 opencode）
      - 廣播思考串流給觀察者
      - 送出回覆
      - 處理錯誤與離開

    支援兩種 engine：
      claude   : 使用 claude_agent_sdk.query()，支援工具、extended thinking、圖片
      opencode : 使用 opencode serve HTTP API + SSE 思考串流
    """

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
        """
        Args:
            room_id       : 要加入的房間 ID（伺服器用來識別/路由訊息的鍵值）
            name          : agent 在房間中的顯示名稱（如 "claude-sonne"）
            model         : 向 Claude 要求的模型名稱（如 "claude-sonnet-4-5"）
                            若為 "claude" 或 "claude-sonnet" 則讓 SDK 自動選擇
            server_url    : OpenParty WebSocket 伺服器的 URL（如 ws://localhost:8765）
            max_turns     : 最大 turn 數（目前未強制執行，agent 靠 "i want leave" 離開）
            allowed_tools : Claude 可使用的工具名稱列表（如 ["Read", "Edit", "Bash"]）
            engine        : AI engine 種類，"claude" 或 "opencode"
            opencode_url  : opencode serve 的基底 URL（僅 engine="opencode" 時使用）
            opencode_model: opencode 使用的 model ID，格式 "provider/model"
            owner_name    : 房間擁有者的顯示名稱，用於 prompt 中的「絕對服從」規則
        """
        self.room_id = room_id
        self.name = name
        self.model = model
        self.server_url = server_url
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.engine = engine
        self.owner_name = owner_name
        # 8 字元 UUID 前綴：在同一房間中唯一識別本 agent 實例（重啟後會變更）
        self.agent_id = str(uuid.uuid4())[:8]
        # Claude Agent SDK 的 session ID；首次收到 SystemMessage 時由 SDK 回填
        self.session_id: Optional[str] = None
        self.log = make_logger(name)
        # opencode 客戶端實例（僅 engine="opencode" 時建立）
        self._opencode: Optional[OpenCodeClient] = None
        if engine == "opencode":
            self._opencode = OpenCodeClient(opencode_url, opencode_model, name)
        # WebSocket 連線物件；在 run() 建立連線後設置，用於 _send_agent_thinking()
        self.ws = None  # set in run() after WS connects

    async def run(self):
        """橋接器主入口：建立 WebSocket 連線並進入 turn 循環。

        流程：
          1. 若 engine 為 opencode，先確保 opencode serve 在執行
          2. 連線到 OpenParty WebSocket 伺服器
          3. 送出 join 訊息，等待 joined 確認
          4. 進入無限循環：等待 your_turn → 生成回覆 → 送出回覆
          5. 遇到 "i want leave"、致命錯誤或 WebSocket 關閉時退出

        WebSocket 連線設定：
          ping_interval=60  : 每 60 秒送一次 ping，維持連線活躍
          ping_timeout=300  : 等待 pong 最多 300 秒，容忍模型長時間思考
        """
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

            # ── 步驟 1：加入房間 ──
            # 傳送 join 訊息，讓伺服器將此 agent 加入指定房間
            await ws.send(
                json.dumps(
                    {
                        "type": "join",
                        "room_id": self.room_id,
                        "agent_id": self.agent_id,
                        "name": self.name,
                        "model": self.model,
                        "engine": self.engine,
                    }
                )
            )

            # ── 步驟 2：等待加入確認 ──
            # 伺服器應回傳 type="joined"，其中包含目前房間內的所有 agent 資訊
            joined_raw = await ws.recv()
            joined = json.loads(joined_raw)
            if joined.get("type") == "joined":
                agents = [a["name"] for a in joined.get("agents_in_room", [])]
                self.log.info(f"Joined room '{self.room_id}' | agents: {agents}")
            else:
                self.log.error(f"Unexpected first message: {joined}")
                return

            # ── 步驟 3：主循環 ──
            # agent 不依賴 max_turns 計數器退出，而是靠回覆中的 "i want leave" 信號。
            # 這讓 agent 可以自主決定何時離開，更符合多 agent 協作的設計哲學。
            while True:
                self.log.info("Waiting for turn...")

                your_turn_payload = None

                async def _drain_until_turn():
                    """非同步消耗 WebSocket 訊息，直到收到 your_turn 或需要退出。

                    訊息類型處理邏輯：
                      your_turn         → 儲存 payload 並回傳 "got_turn"
                      agent_left        → 記錄剩餘 agent 數；若為 0 則回傳 "exit"
                      waiting_for_owner → 記錄等待狀態（owner 尚未傳送指令）
                      system_message    → 記錄訊息；若含 "kicked" 則回傳 "exit"
                      其他資訊性訊息    → 忽略（如 turn_start、room_state 等）
                      WebSocket 關閉    → 回傳 None

                    Returns:
                        "got_turn" → 成功收到輪到本 agent 的訊號
                        "exit"     → 需要離開（被踢出或房間空了）
                        None       → WebSocket 已關閉
                    """
                    nonlocal your_turn_payload
                    async for raw in ws:
                        msg = json.loads(raw)
                        t = msg.get("type")

                        if t == "your_turn":
                            # 收到輪到本 agent 說話的訊號，儲存完整 payload
                            your_turn_payload = msg
                            return "got_turn"
                        elif t == "agent_left":
                            remaining = msg.get("agents_remaining", 0)
                            self.log.info(f"Agent left, {remaining} remaining")
                            if remaining < 1:
                                # 房間內已無其他 agent，繼續等待沒有意義
                                self.log.info("No agents left, exiting")
                                return "exit"
                        elif t == "waiting_for_owner":
                            # owner 尚未傳送下一個指令，agent 繼續等待
                            self.log.info(
                                f"Waiting for owner: {msg.get('message', '')}"
                            )
                        elif t == "system_message":
                            sys_msg = msg.get("message", "")
                            self.log.info(f"System message: {sys_msg}")
                            # 若系統訊息表示被踢出，優雅退出
                            if "kicked" in sys_msg.lower():
                                self.log.info("Kicked from room, exiting")
                                return "exit"
                        elif t in (
                            "turn_start",
                            "turn_end",
                            "room_state",
                            "message",
                            "agent_joined",
                        ):
                            pass  # 純資訊性事件，不需處理
                        else:
                            self.log.debug(f"Unhandled msg type: {t}")
                    return None  # WebSocket 已關閉（伺服器斷線或主動關閉）

                result = await _drain_until_turn()

                if result == "exit":
                    return
                # result 為 None → WebSocket 已關閉；"got_turn" → 繼續執行

                if your_turn_payload is None:
                    self.log.info("WebSocket closed while waiting for turn")
                    break

                self.log.info("My turn!")

                # ── 步驟 4：建立 prompt 並呼叫 AI engine ──
                # build_prompt() 將 your_turn_payload 轉換為結構化的 prompt 字串
                # （或包含圖片的 multipart list）
                prompt = build_prompt(
                    your_turn_payload, self.name, self.owner_name, self.session_id
                )

                # 根據 engine 類型呼叫對應的生成函式
                try:
                    if self.engine == "opencode":
                        assert self._opencode is not None
                        # opencode 不支援 image content blocks，若 prompt 為 list
                        # 則提取其中的文字部分，忽略圖片（目前的限制）
                        prompt_str = (
                            next(
                                (b["text"] for b in prompt if b.get("type") == "text"),
                                "",
                            )
                            if isinstance(prompt, list)
                            else prompt
                        )
                        # 無論 WebSocket 是否存在，統一走 prompt_async + SSE + GET fallback 架構。
                        # _call_opencode_with_thinking 內部的 _send_agent_thinking() 在
                        # self.ws 為 None 時會自動靜默略過廣播，不會拋出例外。
                        reply = await self._call_opencode_with_thinking(prompt_str)
                    else:
                        # claude engine：呼叫 Claude Agent SDK
                        reply, actual_model = await self._call_claude(prompt)
                        # 第一次成功回應後，取得 SDK 實際使用的模型版本並通知伺服器。
                        # 伺服器會更新 UI 顯示（讓觀察者看到完整的模型識別字串）。
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
                    # 致命錯誤：先告知房間成員，再送出 leave 訊號後退出
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

                # ── 步驟 5：處理空回覆（重試一次）──
                # 空回覆通常是暫時性問題（如模型暖機、短暫網路抖動），
                # 重試一次可解決多數情況；若仍為空則使用預設占位字串。
                if not reply:
                    self.log.warning("Empty reply from engine, retrying once...")
                    try:
                        if self.engine == "opencode":
                            assert self._opencode is not None
                            prompt_str = (
                                next(
                                    (
                                        b["text"]
                                        for b in prompt
                                        if b.get("type") == "text"
                                    ),
                                    "",
                                )
                                if isinstance(prompt, list)
                                else prompt
                            )
                            reply = await self._call_opencode_with_thinking(prompt_str)
                        else:
                            reply, _ = await self._call_claude(prompt)
                    except FatalAgentError:
                        pass  # 致命錯誤不重試，讓下方 fallback 處理
                    except Exception as e:
                        self.log.error(f"Retry also failed: {e}")

                # 兩次嘗試均失敗，使用預設占位字串避免空訊息進入房間
                if not reply:
                    reply = "(no response generated)"

                self.log.info(f"Sending reply: {reply[:80]}...")

                # ── 步驟 6：檢查 agent 是否要求離開 ──
                # 若回覆中包含 "i want leave"（不區分大小寫），
                # 先將訊息送入房間（讓其他人看到離開原因），再送出 leave 事件。
                # 這是 agent 自主離開的唯一機制（不依賴計數器）。
                if "i want to leave" in reply.lower():
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

                # ── 步驟 7：送出回覆到房間 ──
                await ws.send(
                    json.dumps(
                        {
                            "type": "message",
                            "content": reply,
                        }
                    )
                )
                # 回到循環頂端，繼續等待下一個 your_turn

        # WebSocket context manager 結束後清除參考
        self.ws = None

    async def _send_agent_thinking(self, blocks: list[dict]) -> None:
        """透過 WebSocket 廣播 agent_thinking 事件（fire-and-forget）。

        此函式由 _call_claude() 和 _call_opencode_with_thinking() 在生成過程中
        持續呼叫，讓觀察者（UI、其他 agent）能即時看到思考/工具呼叫的過程。

        fire-and-forget 設計：送出失敗不影響主流程（警告級日誌，不拋出例外）。

        Args:
            blocks: 內容區塊列表，每個元素為 dict，type 可為：
                    - "thinking"    : 推理文字
                    - "tool_use"    : 工具呼叫（含工具名稱與輸入）
                    - "tool_result" : 工具執行結果預覽
                    - "tool_error"  : 工具執行錯誤預覽
                    - "text"        : 正在生成的文字回應
        """
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
            self.log.warning(f"agent_thinking send failed: {e}")

    async def _call_opencode_with_thinking(self, prompt: str) -> str:
        """呼叫 OpenCode，並透過 SSE 串流即時廣播思考過程。

        架構設計（重構後）
        ---------
        1. 非阻塞提交（prompt_async）：
           - POST /session/{id}/prompt_async 立刻返回 204
           - Engine 在背景非同步執行，不佔用 HTTP 連線
           - 不再有 120 秒阻塞 POST timeout 的限制

        2. SSE 監聽（thinking 廣播 + 完成偵測）：
           - GET /event 訂閱 SSE 事件流，即時廣播推理/工具呼叫事件
           - 偵測 message.updated 事件（finish 已設定且非 tool-calls）表示 engine 完成
           - 最多等待 SSE_TIMEOUT 秒

        3. GET fallback（取得最終文字）：
           - SSE 自然完成或超時後，呼叫 GET /session/{id}/message 查詢持久化結果
           - 即使 SSE 超時，engine 可能已完成並存入 database

        Args:
            prompt: 純文字 prompt 字串（opencode 不支援圖片）

        Returns:
            模型的最終文字回覆
        """
        # SSE 最長等待秒數；engine 若超過此時間仍未完成，走 GET fallback
        SSE_TIMEOUT = 310  # seconds

        assert self._opencode is not None
        oc = self._opencode
        if not oc.session_id:
            oc.session_id = await oc.create_session()

        body = oc._build_body(prompt)

        # Step 1: 非阻塞提交（立刻返回 204，engine 在背景執行）
        if not await oc.submit_async(body):
            return "(opencode error: failed to submit prompt)"

        # Step 2: SSE 監聽器（廣播思考過程，並偵測 engine 完成事件）
        # 自然完成時 sse_task.result() 會回傳從 SSE 事件提取的文字（str | None）
        sse_task = asyncio.create_task(self._opencode_sse_listener())
        _sse_completed_naturally = False
        try:
            await asyncio.wait_for(sse_task, timeout=SSE_TIMEOUT)
            _sse_completed_naturally = True
            self.log.info("OpenCode SSE completed naturally")
        except asyncio.TimeoutError:
            self.log.warning(
                f"OpenCode SSE timeout after {SSE_TIMEOUT}s — "
                "aborting engine then fetching partial result via GET fallback"
            )
            # wait_for 已自動取消 sse_task；呼叫 abort 確保背景 engine 停止，
            # 防止殘留任務與下次提交並行執行
            await oc.abort_session()
        except asyncio.CancelledError:
            sse_task.cancel()
            await asyncio.gather(sse_task, return_exceptions=True)
            raise
        except Exception as e:
            self.log.error(f"OpenCode SSE error: {e}", exc_info=True)
            sse_task.cancel()
            await asyncio.gather(sse_task, return_exceptions=True)
            # SSE 異常退出也 abort，確保 engine 不繼續跑
            await oc.abort_session()

        # Step 3a: SSE 自然完成時，優先使用 SSE 事件中已提取的文字
        # 避免不必要的 GET request，也消除對 opencode DB 寫入順序的隱性依賴
        if _sse_completed_naturally:
            sse_result: str | None = sse_task.result()
            if sse_result:
                self.log.info(
                    f"[SSE path] Using SSE result directly ({len(sse_result)} chars)"
                )
                return sse_result
            self.log.info("[SSE path] SSE result empty, falling back to GET /session/message")

        # Step 3b: GET fallback — 從持久化 session 取得最終文字答案
        # 適用於：(a) SSE timeout/error，(b) SSE 完成但未能從事件中提取文字
        # abort 後 engine 已停止，GET 查到的是最終狀態（非 mid-execution 中間狀態）
        self.log.info("[GET path] Fetching final result via GET /session/message")
        reply = await oc.get_messages()
        if reply:
            return reply

        return "(opencode error: no result available after SSE + GET fallback)"

    async def _opencode_sse_listener(self) -> str | None:
        """訂閱 OpenCode SSE 事件流，將推理/工具呼叫事件廣播為 agent_thinking。

        設計重點
        ---------
        1. 廣播思考過程：
           即時廣播推理文字增量、工具呼叫/結果事件給 OpenParty 觀察者。

        2. Engine 完成偵測（message.updated）：
           監聽 message.updated 事件：當 info.finish 已設定且非 "tool-calls"，
           表示 engine 的 while(true) loop 已退出，設定 _engine_done 旗標並沖刷緩衝。
           後續短暫等待剩餘 SSE 事件後退出。

        3. reasoning_buf 在 finally 中沖刷：
           無論取消、超時或錯誤，reasoning_buf 中的部分推理文字都不會靜默丟棄。

        4. idle 超時機制（engine 完成後）：
           engine 完成後，若超過 _SSE_IDLE_AFTER_DONE 秒無新事件則提前退出，
           避免因 SSE 流不正常關閉而永遠卡住。
           整體 SSE 超時由呼叫端的 asyncio.wait_for 控制。

        支援的 SSE 事件類型：
          message.updated                      → engine 完成偵測（finish 已設定且非 tool-calls）
          message.part.delta (field=reasoning) → 推理文字增量，即時廣播
          reasoning-delta                       → 同上（替代格式）
          reasoning-end                         → 推理結束，沖刷 reasoning_buf
          message.part.stop (field=reasoning)   → 同上
          message.part.updated (ToolPart)       → 工具狀態更新（pending/running/completed/error）
          tool-call                             → 工具呼叫（舊版格式，向下相容）
          text-delta                            → 文字生成增量
          message.part.delta (field=text)       → 同上（替代格式）
          text-end / finish-step / message.stop → 終止事件，engine 完成後立即退出
        """
        assert self._opencode is not None
        oc = self._opencode
        # reasoning_buf: 累積推理文字的緩衝區，用於合併多個 delta 事件
        reasoning_buf: list[str] = []
        # text_buf: 累積生成文字的緩衝區，用於顯示「正在回應...」狀態
        text_buf: list[str] = []
        # engine 完成旗標：收到 message.updated（finish 非 tool-calls/error）後設為 True
        _engine_done: bool = False
        # engine 完成後，若超過此秒數無新 SSE 事件則提前退出
        _SSE_IDLE_AFTER_DONE = 10.0  # seconds of inactivity after engine done
        # SSE 自然完成時從事件中提取的結果文字（供 caller 直接使用，跳過 GET fallback）
        _sse_result: str | None = None

        async def _flush_reasoning() -> None:
            """將 reasoning_buf 中的推理文字作為 thinking block 廣播出去，並清空緩衝區。

            在以下時機呼叫：
              - 收到 reasoning-end 或 message.part.stop (reasoning) 事件
              - SSE 監聽器 finally 區塊（確保不丟棄部分推理文字）
            """
            nonlocal reasoning_buf
            if reasoning_buf:
                text = "".join(reasoning_buf)
                reasoning_buf = []
                await self._send_agent_thinking([{"type": "thinking", "text": text}])

        try:
            # 記錄最後一次收到有效事件的時間（用於 idle 超時檢測）
            _last_event_ts: float = time.monotonic()

            async with aiohttp.ClientSession() as http:
                # 訂閱 SSE 事件流；使用 None 表示不限制總時長，由呼叫端 wait_for 控制
                async with http.get(
                    f"{oc.url}/event",
                    # total=None：整體超時由呼叫端 asyncio.wait_for(SSE_TIMEOUT) 控制
                    # sock_read=None：LLM 長時間思考時不會因無封包而誤判超時
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                        if not line.startswith("data:"):
                            # 非 data 行（如空行、: heartbeat）：engine 完成後檢查 idle 超時
                            if (
                                _engine_done
                                and (time.monotonic() - _last_event_ts)
                                > _SSE_IDLE_AFTER_DONE
                            ):
                                break  # engine 已完成，且超過 idle 超時，提前退出
                            continue

                        # 解析 data: 後的 JSON payload
                        payload = line[5:].strip()
                        if not payload:
                            continue
                        try:
                            data = json.loads(payload)
                        except Exception:
                            continue  # 非法 JSON，忽略

                        props = data.get("properties", {})
                        # 過濾：只處理屬於當前 session 的事件，忽略其他 session 的廣播
                        if props.get("sessionID") != oc.session_id:
                            continue

                        # 只有屬於本 session 的事件才更新時間戳（避免其他 session 的廣播重置 idle 計時器）
                        _last_event_ts = time.monotonic()

                        event_type = data.get("type", "")
                        field = props.get("field", "")
                        delta = props.get("delta", "")

                        self.log.debug(f"OpenCode SSE: type={event_type} field={field}")

                        # ── Engine 完成偵測（message.updated）──
                        # opencode engine 的 while(true) loop 退出條件：
                        # lastAssistant.finish 存在且非 "tool-calls"
                        # 這個 SSE 事件是最可靠的完成信號
                        if event_type == "message.updated":
                            info = props.get("info", {})
                            finish = info.get("finish", "")
                            if (
                                info.get("role") == "assistant"
                                and finish
                                and finish not in _FINISH_BLOCKED
                            ):
                                self.log.info(
                                    f"OpenCode engine finished: finish={finish}"
                                )
                                _engine_done = True
                                await _flush_reasoning()
                                # 優先從 text_buf 取結果（SSE streaming 已累積）；
                                # 次選從 message.updated 事件的 parts 提取（避免依賴 GET）
                                if text_buf:
                                    _sse_result = "".join(text_buf)
                                else:
                                    parts = info.get("parts", [])
                                    texts = [
                                        p.get("text", "")
                                        for p in parts
                                        if p.get("type") in ("text", "text-part")
                                    ]
                                    result_text = "\n".join(t for t in texts if t).strip()
                                    _sse_result = result_text if result_text else None
                                text_buf = []
                                break  # engine 完成，直接退出 SSE 循環，不再依賴 idle timeout
                            continue

                        # ── 推理文字增量（即時廣播）──
                        # 每次 delta 到來就立刻廣播完整的累積推理文字，
                        # 讓 UI 呈現「流式打字」效果
                        if event_type == "message.part.delta" and field == "reasoning":
                            reasoning_buf.append(delta)
                            await self._send_agent_thinking(
                                [{"type": "thinking", "text": "".join(reasoning_buf)}]
                            )

                        # 替代格式的推理文字增量（邏輯同上）
                        elif event_type == "reasoning-delta":
                            reasoning_buf.append(delta)
                            await self._send_agent_thinking(
                                [{"type": "thinking", "text": "".join(reasoning_buf)}]
                            )

                        # 推理結束：沖刷 reasoning_buf（finalizes 最後一塊推理內容）
                        elif event_type in ("reasoning-end",) or (
                            event_type == "message.part.stop" and field == "reasoning"
                        ):
                            await _flush_reasoning()

                        # ── 工具生命週期事件（message.part.updated with ToolPart）──
                        # OpenCode 用 ToolPart 的 state.status 表達工具的生命週期：
                        #   pending/running → 工具啟動中/執行中，廣播 tool_use block
                        #   completed       → 工具完成，廣播 tool_result block（含結果預覽）
                        #   error           → 工具失敗，廣播 tool_error block（含錯誤預覽）
                        elif event_type == "message.part.updated":
                            part = props.get("part", {})
                            if part.get("type") == "tool":
                                tool_name = part.get("tool", "tool")
                                state = part.get("state", {})
                                status = state.get("status", "")
                                tool_input = state.get("input", {})
                                # tool_input 有時會是 JSON 字串，需要反序列化
                                if isinstance(tool_input, str):
                                    try:
                                        tool_input = json.loads(tool_input)
                                    except Exception:
                                        tool_input = {"raw": tool_input}

                                if status in ("pending", "running"):
                                    # 工具啟動中或執行中：廣播工具呼叫資訊
                                    await self._send_agent_thinking(
                                        [
                                            {
                                                "type": "tool_use",
                                                "tool": tool_name,
                                                "input": tool_input,
                                            }
                                        ]
                                    )
                                elif status == "completed":
                                    # 工具完成：廣播結果預覽（最多 40 字元）
                                    output = state.get("output", "")
                                    result_preview = str(output)[:40] if output else ""
                                    title = state.get("title", "")
                                    display = title or result_preview
                                    await self._send_agent_thinking(
                                        [
                                            {
                                                "type": "tool_result",
                                                "tool": tool_name,
                                                "result": display[:40],
                                            }
                                        ]
                                    )
                                elif status == "error":
                                    # 工具失敗：廣播錯誤預覽（最多 40 字元）
                                    error = state.get("error", "error")
                                    error_preview = (
                                        str(error)[:40] if error else "error"
                                    )
                                    await self._send_agent_thinking(
                                        [
                                            {
                                                "type": "tool_error",
                                                "tool": tool_name,
                                                "error": error_preview,
                                            }
                                        ]
                                    )

                        # ── 舊版工具呼叫事件（向下相容）──
                        # 較舊版本的 opencode 使用 tool-call 事件而非 message.part.updated，
                        # 保留此分支確保與舊版 opencode 的相容性
                        elif event_type == "tool-call":
                            tool_name = props.get("tool", props.get("name", "tool"))
                            tool_input = props.get("input", {})
                            if isinstance(tool_input, str):
                                try:
                                    tool_input = json.loads(tool_input)
                                except Exception:
                                    tool_input = {"raw": tool_input}
                            await self._send_agent_thinking(
                                [
                                    {
                                        "type": "tool_use",
                                        "tool": tool_name,
                                        "input": tool_input,
                                    }
                                ]
                            )

                        # ── 文字生成增量 ──
                        # 將生成中的文字以 "text" block 廣播，讓 UI 顯示「正在回應...」
                        # 而非「正在思考...」，區分推理階段與輸出階段
                        elif event_type in ("text-delta",) or (
                            event_type == "message.part.delta" and field == "text"
                        ):
                            text_buf.append(delta)
                            await self._send_agent_thinking(
                                [{"type": "text", "text": "".join(text_buf)}]
                            )

                        # ── 終止事件 ──
                        # 沖刷推理緩衝並清空文字緩衝
                        # 若 engine 已完成（_engine_done），立即退出 SSE 循環
                        elif event_type in ("text-end", "finish-step", "message.stop"):
                            text_buf = []
                            await _flush_reasoning()
                            if _engine_done:
                                break

        except asyncio.CancelledError:
            # 被 _call_opencode_with_thinking() 取消（超過 SSE_TIMEOUT）
            # 靜默處理，不影響主流程
            pass
        except Exception as e:
            self.log.debug(f"OpenCode SSE listener error: {e}")
        finally:
            # 確保即使在取消、超時或未預期退出時，reasoning_buf 中的內容也能被廣播。
            # 使用 asyncio.shield() 防止 CancelledError 在 cancelled task context 中
            # 再次中斷 _flush_reasoning()（CancelledError 繼承自 BaseException，
            # 不被 except Exception 攔截，shield 讓 inner coroutine 繼續執行至完成）。
            try:
                await asyncio.shield(_flush_reasoning())
            except BaseException:
                pass
        return _sse_result

    async def _call_claude(
        self, prompt: "str | list[dict]"
    ) -> tuple[str, Optional[str]]:
        """呼叫 Claude Agent SDK，取得回覆文字與實際使用的模型版本。

        功能特點：
          - 支援 extended thinking（透過 ThinkingConfigEnabled）
          - 支援工具呼叫（Bash、Read、Edit 等）
          - 支援圖片輸入（prompt 為 multipart list 時）
          - 將思考/工具呼叫過程即時廣播為 agent_thinking 事件
          - Session 連續性：首次呼叫後保存 session_id，後續呼叫 resume 同一 session

        Session 管理：
          Claude Agent SDK 在每次 query() 時維護一個 session（對話歷史）。
          若提供 resume=session_id，SDK 會在同一上下文中繼續對話。
          首次呼叫時 session_id=None，SDK 在 SystemMessage 中回傳新的 session_id。
          但注意：prompt 已包含完整歷史，session 主要用於 agent 的工具呼叫記憶。

        圖片支援（multipart prompt）：
          當 prompt 為 list[dict]（由 build_prompt() 在有 image_blocks 時回傳）時，
          本函式將其包裝成 AsyncIterable user message 傳給 SDK：
            {type: "user", message: {role: "user", content: [image_blocks..., text_block]}}
          SDK 會將此 content list 直接傳給 Claude API，實現多模態輸入。

        錯誤處理：
          ProcessError    → CLI 進程異常退出（如 token 耗盡、CLI bug）
                           有部分結果時回傳部分結果；否則回傳錯誤描述字串
          其他 Exception  → SDK 層面的錯誤
                           有部分結果時忽略（避免截斷已生成的內容）；否則回傳錯誤描述
          FatalAgentError → 從 is_error=True 的 ResultMessage 拋出，表示 Claude API 拒絕服務

        Args:
            prompt: 純文字 prompt（str）或 multipart content block 列表（list[dict]）

        Returns:
            tuple[str, Optional[str]]：
              - str           : 回覆文字（失敗時為錯誤描述字串）
              - Optional[str] : 實際使用的模型版本（如 "claude-sonnet-4-5"），
                                無法偵測時為 None

        Raises:
            FatalAgentError: 當 SDK 回傳 is_error=True 的 ResultMessage 時
        """

        # stderr_buffer 收集 Claude CLI 的 stderr 輸出，用於錯誤診斷
        stderr_buffer: list[str] = []

        def _stderr_callback(line: str) -> None:
            """SDK 的 stderr 回調：將每行 stderr 記錄到緩衝區並以 WARNING 級別日誌輸出。"""
            stderr_buffer.append(line)
            self.log.warning(f"[CLI stderr] {line}")

        # 配置 Claude Agent SDK 選項
        options = ClaudeAgentOptions(
            allowed_tools=self.allowed_tools,  # 允許的工具列表
            permission_mode="bypassPermissions",  # 跳過權限確認對話，避免 agent 卡住
            # 若 model 為通用別名（"claude" 或 "claude-sonnet"），讓 SDK 選擇預設模型
            model=self.model if self.model not in ("claude", "claude-sonnet") else None,
            resume=self.session_id,  # 延續上一個 session（None 表示新建 session）
            # max_turns 不設定，由 binary 自行決定何時完成（自然終止）
            thinking=ThinkingConfigEnabled(
                type="enabled", budget_tokens=8000
            ),  # 開啟 8K token 的 extended thinking
            stderr=_stderr_callback,  # 設定 stderr 回調以收集錯誤資訊
            # 角色規則透過 system_prompt 注入，確保以最高權威層級生效，
            # 不隨對話歷史增長而被稀釋
            system_prompt=build_room_rules_prompt(self.owner_name),
        )

        result_text = ""  # 最終回覆文字（由 ResultMessage 填入）
        result_is_error = (
            False  # 是否為錯誤結果（is_error=True 時需要拋出 FatalAgentError）
        )
        actual_model: Optional[str] = (
            None  # 實際使用的模型版本（從 AssistantMessage 取得）
        )

        # ── 圖片支援：將 multipart prompt 包裝為 AsyncIterable ──
        # SDK 的 query() 函式的 prompt 參數可接受：
        #   1. str：純文字 prompt
        #   2. AsyncIterable[dict]：串流訊息，每個 dict 是一個 user/assistant 訊息
        # 當 prompt 為 list（含圖片 block）時，使用 async generator 包裝，
        # 符合 SDK 的 multipart 輸入介面。
        async def _content_blocks_iter(blocks: list[dict]):
            """將 content blocks 列表包裝為 AsyncIterable，供 SDK 的串流介面使用。"""
            yield {
                "type": "user",
                "message": {"role": "user", "content": blocks},
                "parent_tool_use_id": None,
                "session_id": self.session_id,
            }

        if isinstance(prompt, list):
            prompt_arg = _content_blocks_iter(prompt)
        else:
            prompt_arg = prompt

        try:
            # ── 主要生成循環 ──
            # SDK 的 query() 為非同步生成器，依序 yield 以下類型的訊息：
            #   SystemMessage   → 包含 session_id（首次呼叫時）
            #   AssistantMessage → 包含思考/工具呼叫/文字的 content blocks
            #   ResultMessage   → 最終結果（包含完整回覆文字和 is_error 標誌）
            async for message in query(prompt=prompt_arg, options=options):  # type: ignore[arg-type]
                if isinstance(message, SystemMessage):
                    # 從 SystemMessage 取得 session_id，用於後續 resume
                    sid = getattr(message, "session_id", None)
                    if sid and self.session_id is None:
                        self.session_id = sid
                        self.log.info(f"Session established: {self.session_id}")

                elif isinstance(message, AssistantMessage):
                    # 取得實際使用的模型版本（只需取第一次出現的值）
                    if actual_model is None:
                        actual_model = getattr(message, "model", None) or None

                    # 解析 content blocks 並廣播為 agent_thinking 事件
                    # 讓觀察者（UI、其他 agent）即時看到思考/工具呼叫過程
                    blocks = []
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            # Extended thinking block：推理文字
                            blocks.append({"type": "thinking", "text": block.thinking})
                        elif isinstance(block, ToolUseBlock):
                            # 工具呼叫 block：工具名稱與輸入參數
                            blocks.append(
                                {
                                    "type": "tool_use",
                                    "tool": block.name,
                                    "input": block.input,
                                }
                            )
                        elif isinstance(block, TextBlock):
                            # 文字 block：agent 在工具呼叫間或最終回覆的文字
                            blocks.append({"type": "text", "text": block.text})
                    if blocks:
                        await self._send_agent_thinking(blocks)

                elif isinstance(message, ResultMessage):
                    # 最終結果：提取回覆文字與錯誤標誌
                    result_text = getattr(message, "result", "") or ""
                    result_is_error = bool(getattr(message, "is_error", False))
                    if result_is_error:
                        self.log.warning(f"ResultMessage is_error=True: {message.__dict__}")

        except ProcessError as e:
            # CLI 進程異常退出（如 token 超過限制、CLI 崩潰）
            exit_code = getattr(e, "exit_code", None)
            stderr_text = getattr(e, "stderr", None)
            collected_stderr = (
                "\n".join(stderr_buffer)
                if stderr_buffer
                else "(no stderr lines collected)"
            )
            self.log.error(
                f"CLI ProcessError: exit_code={exit_code}, "
                f"session_id={self.session_id!r}, "
                f"stderr={stderr_text!r}, msg={e}, "
                f"repr={repr(e)}, attrs={e.__dict__}"
            )
            self.log.error(
                f"=== COLLECTED STDERR ({len(stderr_buffer)} lines) ===\n"
                f"{collected_stderr}\n"
                f"=== END COLLECTED STDERR ==="
            )
            if result_text:
                # 有部分結果：雖然進程以錯誤退出，但已生成了有用的內容，回傳部分結果
                self.log.warning(
                    "ProcessError after partial result — returning partial"
                )
            elif result_is_error:
                # ResultMessage 已收到 is_error=True（如 error_max_turns），直接回傳錯誤說明
                return (
                    f"(error: {exit_code}, max turns or similar limit reached)",
                    actual_model,
                )
            else:
                # 無任何結果：回傳錯誤描述字串，讓 agent 能夠回應並說明狀況
                return (
                    f"(error: CLI exit {exit_code}: {collected_stderr})",
                    actual_model,
                )
        except Exception as e:
            # SDK 層面的其他例外
            collected_stderr = (
                "\n".join(stderr_buffer)
                if stderr_buffer
                else "(no stderr lines collected)"
            )
            if result_text:
                # 有部分結果：ResultMessage 之後的例外通常是無害的清理錯誤，忽略
                self.log.debug(f"SDK post-result exception (ignored): {e}")
            else:
                # 無任何結果：記錄完整錯誤資訊以利診斷，並回傳錯誤描述
                self.log.error(
                    f"SDK error: {e}, type={type(e).__name__}, "
                    f"repr={repr(e)}, attrs={getattr(e, '__dict__', {})}"
                )
                self.log.error(
                    f"=== COLLECTED STDERR ({len(stderr_buffer)} lines) ===\n"
                    f"{collected_stderr}\n"
                    f"=== END COLLECTED STDERR ==="
                )
                return (
                    f"(error: CLI exit (generic): {collected_stderr})",
                    actual_model,
                )

        # is_error=True 表示 Claude API 拒絕服務（如超過配額、內容過濾等）
        # 拋出 FatalAgentError 讓呼叫端（run()）優雅離開房間
        if result_is_error and result_text:
            raise FatalAgentError(result_text)

        return result_text.strip(), actual_model


# ── Entry point ────────────────────────────────────────────────────────────────


def parse_args():
    """解析命令列引數，回傳 argparse.Namespace 物件。

    所有引數說明：
      --room         : 必填，要加入的房間 ID（伺服器用來路由訊息的唯一鍵值）
      --name         : 必填，agent 在房間中的顯示名稱（如 "claude-sonne"）
      --model        : 選填，向 Claude API 要求的模型（如 "claude-sonnet-4-5"）
                       預設 "claude" 讓 SDK 自動選擇最新穩定版本
      --server       : 選填，OpenParty WebSocket 伺服器 URL
      --max-turns    : 選填，最大 turn 數（目前為參考值，實際靠 "i want leave" 離開）
      --tools        : 選填，逗號分隔的允許工具列表（如 "Read,Edit,Bash,Glob,Grep,WebSearch"）
      --engine       : 選填，AI 引擎類型："claude"（預設）或 "opencode"
      --opencode-url : 選填，opencode serve 基底 URL（僅 engine=opencode 時使用）
      --opencode-model: 選填，opencode 使用的 model ID（格式 "provider/model"）
      --owner-name   : 選填，房間擁有者顯示名稱（用於「絕對服從」規則）
    """
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
    """橋接器的非同步主函式。

    流程：
      1. 解析命令列引數
      2. 將 tools 字串拆分為列表
      3. 建立 AgentBridge 實例
      4. 呼叫 bridge.run() 開始連線並進入 turn 循環
      5. 捕獲 KeyboardInterrupt（Ctrl+C）讓使用者可以乾淨地中止
      6. 其他例外以 exit code 1 退出，便於 shell 腳本偵測失敗
    """
    args = parse_args()
    # 將逗號分隔的工具字串轉換為列表，過濾空字串
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
        # 使用者按 Ctrl+C，正常退出（不視為錯誤）
        print(f"\n[{args.name}] Interrupted, exiting.")
    except Exception as e:
        # 未預期的例外：記錄完整 traceback 並以 exit code 1 退出
        logging.error(f"Bridge crashed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
