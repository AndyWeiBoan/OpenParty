"""
OpenParty Room Server — M3 多代理討論系統核心
==============================================

【系統概述】
本伺服器作為 WebSocket hub，負責管理多代理線上討論空間。主要功能包括：
- 房間管理：依 room_id 建立/維護討論房間，支援多人同時參與
- 訊息轉發：將任一 agent 的回覆廣播給房間內所有其他人
- 發言權控制：實現 round-robin 輪詢機制，管理發言順序
- Observer 模式：支援唯讀觀察者連線，可即時查看討論過程但無法發言
- 事件發送：向 Observer 發送結構化事件（turn_start/turn_end/room_state），供 UI 展示
- Owner 啟動機制：討論必須由房間 owner 發出第一則訊息才正式開始

【三種發言模式】
1. 循序模式（sequential）：一次只有一個 agent 發言，發言完畢後輪到下一個
2. 廣播模式（broadcast）：owner 發送廣播訊息時，所有 agent 同時收到 your_turn
3. 私訊模式（private）：owner 使用 #AgentName 語法指定特定 agent 单独回覆

【M3 相較 M2 的變更】
  - Owner kickoff：討論必須等 owner 發送第一則訊息才開始
  - Owner's first message：設定本 session 的討論主題（topic）
  - Agents are notified：agent 在 owner 發言前會收到 waiting_for_owner 通知
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 用於解析訊息中的 #mention 語法（例如 "#Alice"），擷取提及的名稱
# 規則：名稱以字母或底線開頭，後續可接字母、數字、底線或連字號
_MENTION_RE = re.compile(r"#([\w][\w\-]*)")

import aiohttp
import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

# 伺服器腳本所在目錄，用於定位 bridge.py 及各種 log 檔路徑
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
OPENCODE_PORT = 4096
OPENCODE_URL = f"http://127.0.0.1:{OPENCODE_PORT}"


def _check_opencode_installed() -> bool:
    """檢查 opencode CLI 是否已安裝並存在於 PATH 中。"""
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
    """向 opencode serve 的健康檢查端點發送請求，確認服務是否已就緒。

    回傳 True 代表服務正常運行，回傳 False 代表尚未啟動或發生錯誤。
    """
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

# 每輪傳給 agent 的最大歷史條目數，避免 context window 超出 token 限制
SLIDING_WINDOW_SIZE = 20


@dataclass
class Agent:
    ws: WebSocketServerProtocol  # 與該 agent 保持連線的活躍 WebSocket 物件
    agent_id: str  # 房間內唯一識別字串（通常由 bridge 或外部指定）
    name: str  # 顯示名稱，用於 UI 及 mention 語法
    model: str  # 使用的 LLM 模型字串（例如 "claude-3-5-sonnet"）
    room_id: str  # 此 agent 所在的房間 ID
    engine: str = ""  # 推論後端引擎，目前支援 "opencode" 或 "claude"


@dataclass
class Observer:
    ws: WebSocketServerProtocol  # 觀察者的 WebSocket 連線
    observer_id: str  # 觀察者的唯一識別字串
    name: str  # 觀察者顯示名稱
    is_owner: bool = False  # 是否為房間擁有者；只有 owner 可以發送指令與訊息


@dataclass
class Room:
    room_id: str
    agents: dict[str, Agent] = field(default_factory=dict)
    # agents：以 agent_id 為鍵，存放目前在房間內的所有 Agent 物件

    observers: dict[str, Observer] = field(default_factory=dict)
    # observers：以 observer_id 為鍵，存放所有觀察者（含 owner）

    history: list[dict] = field(default_factory=list)
    # history：按時間順序追加的所有訊息記錄，不做修改或刪除（append-only）

    current_speaker: Optional[str] = None
    # current_speaker：目前持有發言權的 agent_id；None 代表無人發言中

    topic: str = ""  # Set by owner's first message
    # topic：由 owner 第一則訊息所設定的討論主題

    rolling_summary: str = ""  # Phase 2: filled by async summariser
    # rolling_summary：預留給第二階段非同步摘要器使用，目前恆為空字串

    turn_started_at: float = 0.0  # monotonic timestamp when current turn began
    # turn_started_at：使用 time.monotonic() 記錄當前輪次開始時刻，用於計算延遲

    owner_kicked_off: bool = False  # True after owner sends first message
    # owner_kicked_off：owner 尚未發送第一則訊息前為 False，阻止 agent 開始發言

    turn_pending: bool = False  # True while an agent is actively thinking
    # turn_pending：某 agent 正在計算回覆期間為 True，防止重複觸發新的輪次

    current_round: int = (
        0  # increments on each owner message; used for history windowing
    )
    # current_round：每次 owner 發送訊息時遞增，用於將歷史分組到回合中

    round_speakers: set = field(default_factory=set)  # agents who spoke this round
    # round_speakers：記錄本回合已發言的 agent_id 集合，避免同一回合重複觸發

    broadcast_pending: Optional[set] = (
        None  # None = sequential mode; set = agent IDs yet to respond in broadcast
    )
    # broadcast_pending：
    #   None  → 循序模式（sequential），一次只有一個 agent 發言
    #   set   → 廣播模式（broadcast），集合內為尚未回覆的 agent_id

    # Private message support: maps history index → set of agent_ids allowed to see that entry
    private_visibility: dict = field(default_factory=dict)
    # private_visibility：history 索引 → 允許讀取該條目的 agent_id 集合
    # 沒有出現在此 dict 中的 history 索引對所有 agent 可見

    # Set to the target agent_id(s) while a private turn is in progress; None otherwise
    current_private_for: Optional[set] = None
    # current_private_for：私訊輪次進行中時為目標 agent 的 agent_id 集合；否則為 None

    thinking_log: dict[str, list[dict]] = field(default_factory=dict)
    # thinking_log：以 agent_id 為鍵，存放最近 20 條 agent_thinking 事件的 FIFO 緩衝，
    # 供 Observer UI 顯示思考過程使用

    def context_window(self, agent_id: Optional[str] = None) -> list[dict]:
        """Return recent history window, filtering out private entries invisible to agent_id.

        【中文說明】計算滑動視窗邏輯：
        1. 找出當前回合（current_round）在 history 中的起始索引
        2. 以「完整包含當前回合」為基礎，往前補充最多 SLIDING_WINDOW_SIZE 條記錄
        3. 若指定了 agent_id，過濾掉 private_visibility 中該 agent 無權讀取的條目
        """
        # Find where the current round starts in history
        current_round_start = next(
            (
                i
                for i, e in enumerate(self.history)
                if e.get("round", 0) >= self.current_round
            ),
            len(self.history),
        )
        # Take at least the full current round, extended back to SLIDING_WINDOW_SIZE
        start_idx = min(
            current_round_start, max(0, len(self.history) - SLIDING_WINDOW_SIZE)
        )
        window = self.history[start_idx:]
        if agent_id is None or not self.private_visibility:
            return window
        # 過濾掉此 agent 無閱覽權限的私密記錄
        return [
            entry
            for i, entry in enumerate(window)
            if (start_idx + i) not in self.private_visibility
            or agent_id in self.private_visibility[start_idx + i]
        ]

    def next_speaker(self, exclude_id: str) -> Optional[Agent]:
        """Return next agent who hasn't spoken this round. None if all have spoken.

        【中文說明】按加入順序（round-robin）尋找下一位尚未在本回合發言的 agent，
        跳過 exclude_id 本身以及已出現在 round_speakers 中的 agent。
        若所有 agent 均已發言，回傳 None。
        """
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
        """Snapshot of room state — sent to observers on each turn boundary.

        【中文說明】產生房間狀態快照，在每個輪次邊界廣播給所有 observer，
        讓 UI 能即時更新參與者列表、當前發言者及歷史條目數。
        """
        # 找出 owner 的名字
        owner_name = ""
        for o in self.observers.values():
            if o.is_owner:
                owner_name = o.name
                break
        return {
            "type": "room_state",
            "room_id": self.room_id,
            "topic": self.topic,
            "turn_count": len(self.history),
            "current_speaker": self.current_speaker,
            "owner_name": owner_name,
            "participants": [
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "model": a.model,
                    "engine": a.engine,
                }
                for a in self.agents.values()
            ],
            "observers": [
                {
                    "observer_id": o.observer_id,
                    "name": o.name,
                    "is_owner": o.is_owner,
                }
                for o in self.observers.values()
            ],
        }


class RoomServer:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.spawned_procs: list[asyncio.subprocess.Process] = []
        self.opencode_proc: Optional[asyncio.subprocess.Process] = None
        self.available_engines: list[str] = []  # ["opencode", "claude"]

    async def startup(self):
        """Check installed tools and start opencode serve if available.

        【中文說明】兩階段引擎偵測：
        1. 先嘗試 opencode：若已安裝則檢查是否已在執行（健康檢查），
           尚未執行則自動啟動 `opencode serve` 子程序，等待最多 5 秒確認就緒後
           將 "opencode" 加入 available_engines。
        2. 再嘗試 claude：檢查 claude_agent_sdk 的捆綁二進位或系統 PATH 中的 claude，
           若可用則將 "claude" 加入 available_engines。
        """
        if _check_opencode_installed():
            if await _opencode_healthy():
                log.info("opencode serve already running — reusing")
                self.available_engines.append("opencode")
            else:
                log.info("Starting opencode serve...")
                log_path = os.path.join(_SERVER_DIR, "opencode_serve.log")
                lf = open(log_path, "w")
                self.opencode_proc = await asyncio.create_subprocess_exec(
                    "opencode",
                    "serve",
                    "--port",
                    str(OPENCODE_PORT),
                    stdout=lf,
                    stderr=lf,
                )
                # Wait up to 5 s for it to become healthy
                for _ in range(10):
                    await asyncio.sleep(0.5)
                    if await _opencode_healthy():
                        log.info(
                            f"opencode serve started (pid={self.opencode_proc.pid})"
                        )
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

    async def _spawn_agent_process(
        self,
        room: Room,
        name: str,
        model_id: str,
        engine: str = "opencode",
        owner_name: str = "",
    ) -> bool:
        """Spawn a bridge.py subprocess and track it. Returns True on success.

        【中文說明】組裝 bridge.py 的 CLI 命令列並啟動子程序：
        - opencode 引擎：需同時傳遞 --opencode-model 和 --model（兩者值相同）
        - claude 引擎：model_id 格式為 "claude/<model-name>"，需去掉前綴後傳給 --model
        子程序的 stdout/stderr 均導向以 agent 名稱命名的 log 檔。
        """
        bridge_path = os.path.join(_SERVER_DIR, "bridge.py")
        log_path = os.path.join(_SERVER_DIR, f"agent_{name}.log")

        cmd = [
            sys.executable,
            bridge_path,
            "--room",
            room.room_id,
            "--name",
            name,
            "--engine",
            engine,
        ]
        if engine == "opencode":
            # opencode 引擎需要同時指定 --opencode-model 與 --model
            cmd += ["--opencode-model", model_id, "--model", model_id]
        else:
            # claude engine: model_id is "claude/<model-name>", extract the model name
            # claude 引擎的 model_id 帶有 "claude/" 前綴，取 "/" 後的部分傳給 --model
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
            log.info(
                f"Spawned agent '{name}' ({engine}/{model_id}) | pid={proc.pid} | room={room.room_id}"
            )
            return True
        except Exception as e:
            log.error(f"Failed to spawn agent '{name}': {e}")
            return False

    async def shutdown(self):
        """Terminate all spawned agent and opencode serve processes.

        【中文說明】伺服器關閉時，終止所有由 _spawn_agent_process() 產生的 bridge 子程序，
        以及若由本伺服器自行啟動的 opencode serve 子程序（returncode 仍為 None 表示尚在執行）。
        """
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
        """惰性初始化（lazy initialization）：首次存取時建立 Room，後續重複使用同一物件。

        這樣設計讓任何 agent 或 observer 都可以通過同一個 room_id 加入同一個房間，
        無需事先手動建立房間。
        """
        # 若房間尚不存在，則惰性建立一個新的 Room 物件；已存在則直接回傳
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
        observers_only=True sends only to observers, skipping agents entirely.

        【中文說明】三種廣播模式：
        - observers_only=True：僅送給所有 observer（例如轉發私密思考內容）
        - agents_only=True：僅送給 agents（排除 exclude_id），不送給 observer
        - 預設（兩者皆 False）：送給所有 agents（排除 exclude_id）及所有 observers
        使用 asyncio.gather 並行發送，return_exceptions=True 確保單一失敗不中斷其他發送。
        """
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

    def _build_image_blocks_from_history(self, history: list[dict]) -> list[dict]:
        """Scan history for image attachments and return base64 image blocks.

        Reads image files from disk, base64-encodes them, and returns Anthropic
        image content blocks. Missing files are silently skipped (warn only).

        【中文說明】遍歷歷史記錄中所有條目的 "images" 欄位，對每張圖片：
        1. 先進行沙盒路徑驗證：確保解析後的實際路徑位於 /tmp/openparty/images 目錄內，
           拒絕任何路徑穿越（path traversal）攻擊的圖片。
        2. 讀取檔案並以 base64 編碼，組裝成 Anthropic API 的 image content block 格式回傳。
        """
        image_blocks: list[dict] = []
        for entry in history:
            for img in entry.get("images", []):
                img_path = img.get("path", "")
                mime = img.get("mime", "image/jpeg")
                if not img_path:
                    continue
                # Security: validate path is within the expected image sandbox
                # 安全性驗證：確認圖片路徑在允許的沙盒目錄內，防止路徑穿越攻擊
                _IMAGE_SANDBOX = os.path.realpath("/tmp/openparty/images")
                resolved = os.path.realpath(img_path)
                if (
                    not resolved.startswith(_IMAGE_SANDBOX + os.sep)
                    and resolved != _IMAGE_SANDBOX
                ):
                    log.warning(
                        f"Rejected image path outside sandbox: {img_path} -> {resolved}"
                    )
                    continue
                try:
                    data = Path(resolved).read_bytes()
                    b64 = base64.b64encode(data).decode()
                    # 組裝 Anthropic API 所需的 base64 圖片 content block
                    image_blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": b64,
                            },
                        }
                    )
                except FileNotFoundError:
                    log.warning(f"Image file not found, skipping: {img_path}")
                except Exception as e:
                    log.warning(f"Failed to read image {img_path}: {e}")
        return image_blocks

    async def _send_broadcast_turn(self, room: Room):
        """Send your_turn to ALL agents simultaneously (broadcast mode).

        【中文說明】廣播模式下的輪次啟動邏輯：
        1. 將 broadcast_pending 設為所有 agent_id 的集合，追蹤尚未回覆的 agent
        2. 對每個 agent 分別過濾私密歷史記錄（context_window），組裝各自的 your_turn payload
        3. 使用 asyncio.gather 同時並行發送給所有 agent（fan-out）
        4. 為每個 agent 各自向 observer 發送一個 turn_start 事件，讓 UI 顯示多人同時發言
        """
        agents = list(room.agents.values())
        if not agents:
            return

        # 設定廣播模式：broadcast_pending 集合記錄所有尚未完成回覆的 agent
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
        # 為每個 agent 個別過濾私密歷史記錄後再發送，確保隱私不洩漏
        async def _send_one(a: Agent) -> None:
            history_window = room.context_window(a.agent_id)
            image_blocks = self._build_image_blocks_from_history(history_window)
            payload: dict = {
                "type": "your_turn",
                "broadcast": True,
                "history": history_window,
                "summary": room.rolling_summary,
                "context": context_base,
            }
            if image_blocks:
                payload["image_blocks"] = image_blocks
            await a.ws.send(json.dumps(payload))

        # 並行發送給所有 agent
        await asyncio.gather(
            *[_send_one(a) for a in agents],
            return_exceptions=True,
        )

        # Notify observers: one turn_start per agent
        # 每個 agent 各發送一個 turn_start 事件，讓 observer UI 知道多人同時發言
        for agent in agents:
            await self._broadcast(
                room,
                {
                    "type": "turn_start",
                    "agent_id": agent.agent_id,
                    "name": agent.name,
                    "model": agent.model,
                    "broadcast": True,
                    "turn_number": len(room.history) + 1,
                },
                agents_only=False,
            )

        log.info(f"[{room.room_id}] broadcast → {[a.name for a in agents]}")

    async def _send_your_turn(self, room: Room, agent: Agent, kickoff: bool = False):
        """Send your_turn to an agent and emit turn_start to observers.

        【中文說明】循序模式下將發言權交給單一 agent：
        1. 更新 current_speaker 與 turn_pending 狀態，記錄輪次開始時刻
        2. 透過 context_window() 過濾歷史記錄後，組裝含圖片 blocks 的 your_turn payload
        3. 若為 kickoff（討論開始），額外附上 topic 作為 prompt 欄位
        4. 發送 your_turn 給 agent 後，廣播 turn_start 事件給所有 observer
        """
        room.current_speaker = agent.agent_id
        room.turn_started_at = time.monotonic()
        room.turn_pending = True

        history_window = room.context_window(agent.agent_id)
        image_blocks = self._build_image_blocks_from_history(history_window)

        your_turn_payload = {
            "type": "your_turn",
            "history": history_window,
            "summary": room.rolling_summary,
            "context": {
                "topic": room.topic,
                "participants": [
                    {"name": a.name, "model": a.model} for a in room.agents.values()
                ],
                "total_turns": len(room.history),
            },
        }
        if image_blocks:
            your_turn_payload["image_blocks"] = image_blocks
        if kickoff:
            # 討論開始時額外附上主題作為初始提示
            your_turn_payload["prompt"] = room.topic

        await agent.ws.send(json.dumps(your_turn_payload))

        # Notify observers
        # 通知所有 observer 輪次已開始，提供 agent 資訊與輪次編號
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
            # ── 初始握手：等待第一則訊息並驗證 join 格式 ──────────────────────
            raw = await ws.recv()
            msg = json.loads(raw)

            if msg.get("type") != "join":
                # 第一則訊息必須是 join，否則拒絕連線
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

            # ── Observer 路徑：註冊觀察者並進入 owner 指令迴圈 ───────────────
            if role == "observer":
                import uuid

                observer_id = msg.get("observer_id", str(uuid.uuid4())[:8])
                is_owner = msg.get("owner", False)
                obs = Observer(
                    ws=ws, observer_id=observer_id, name=name, is_owner=is_owner
                )

                # Kick out any existing owner if a new owner joins
                # 若新 owner 加入，踢出舊的 owner 並關閉其連線（每個房間只能有一個 owner）
                if is_owner:
                    for old_id, old_obs in list(room.observers.items()):
                        if old_obs.is_owner and old_id != observer_id:
                            log.info(
                                f"Replacing old owner '{old_obs.name}' with '{name}'"
                            )
                            try:
                                await old_obs.ws.send(
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "message": f"你已被新的 owner '{name}' 取代，連線關閉。",
                                        }
                                    )
                                )
                                await old_obs.ws.close()
                            except Exception:
                                pass
                            room.observers.pop(old_id, None)

                room.observers[observer_id] = obs
                identity = obs

                log.info(
                    f"Observer joined | room={room_id} | name={name} | owner={is_owner}"
                )

                # 發送 joined 確認，附帶當前房間狀態快照、歷史記錄及可用引擎清單
                # 注意：owner_name 會傳給所有 observer，讓大家都能看到 room header
                owner_name_for_all = ""
                # 找出其他 observer 中是否有 owner，傳遞給新加入的 observer
                if is_owner:
                    # owner 加入時，自己就是 owner，直接用自己名字
                    owner_name_for_all = name
                else:
                    for old_obs in room.observers.values():
                        if old_obs.is_owner:
                            owner_name_for_all = old_obs.name
                            break
                await ws.send(
                    json.dumps(
                        {
                            "type": "joined",
                            "role": "observer",
                            "room_id": room_id,
                            "observer_id": observer_id,
                            "is_owner": is_owner,
                            "owner_name": owner_name_for_all,
                            "room_state": room.room_state_payload(),
                            "history": room.context_window(),
                            "available_engines": self.available_engines,
                        }
                    )
                )

                # ── Owner 指令迴圈（非 owner 的訊息直接忽略）────────────────
                async for raw_msg in ws:
                    if not is_owner:
                        # 非 owner 的 observer 為唯讀，忽略所有傳入訊息
                        continue
                    try:
                        owner_msg = json.loads(raw_msg)
                    except Exception:
                        continue
                    msg_type = owner_msg.get("type")

                    # ── spawn_agent: server spawns a bridge subprocess ────────
                    # 由 owner 請求伺服器在本機啟動一個 bridge.py agent 子程序
                    if msg_type == "spawn_agent":
                        agent_name = owner_msg.get(
                            "name", "agent"
                        )  # different var from observer name
                        model_id = owner_msg.get("model", "")
                        engine = owner_msg.get("engine", "opencode")
                        # 檢查所要求的引擎是否在本伺服器上可用
                        if engine not in self.available_engines:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "spawn_result",
                                        "name": agent_name,
                                        "model": model_id,
                                        "success": False,
                                        "reason": f"engine '{engine}' not available on this server",
                                    }
                                )
                            )
                            continue
                        ok = await self._spawn_agent_process(
                            room, agent_name, model_id, engine, owner_name=name
                        )
                        # 回報 spawn 結果給 owner
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "spawn_result",
                                    "name": agent_name,
                                    "model": model_id,
                                    "engine": engine,
                                    "success": ok,
                                }
                            )
                        )
                        continue

                    # ── kick_all: owner removes every agent from the room ─────
                    # owner 將房間內所有 agent 一次全部踢除
                    if msg_type == "kick_all":
                        targets = list(room.agents.values())
                        if targets:
                            # Remove from room FIRST so subsequent owner messages
                            # don't see stale agents and send them your_turn.
                            # 先從 room.agents 移除，避免後續 owner 訊息誤觸發舊 agent
                            for t in targets:
                                room.agents.pop(t.agent_id, None)
                            room.current_speaker = None
                            room.turn_pending = False
                            room.round_speakers = set()

                            await self._broadcast(
                                room,
                                {
                                    "type": "system_message",
                                    "text": "All agents were kicked from the room",
                                },
                            )

                            # Close WebSockets fire-and-forget (they're already removed)
                            # 以 fire-and-forget 方式非同步關閉所有 WebSocket 連線
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
                    # owner 按名稱踢除單一 agent
                    if msg_type == "kick_agent":
                        kick_name = owner_msg.get("agent_name", "")
                        target = next(
                            (a for a in room.agents.values() if a.name == kick_name),
                            None,
                        )
                        if target:
                            log.info(f"[{room_id}] Owner kicked agent '{kick_name}'")
                            # Remove immediately so next owner message doesn't route to it
                            # 立即從房間移除，防止後續訊息還被路由到已踢除的 agent
                            room.agents.pop(target.agent_id, None)
                            if room.current_speaker == target.agent_id:
                                room.current_speaker = None
                                room.turn_pending = False
                            room.round_speakers.discard(target.agent_id)
                            await self._broadcast(
                                room,
                                {
                                    "type": "system_message",
                                    "text": f"{kick_name} was kicked from the room",
                                },
                            )
                            try:
                                await target.ws.close()
                            except Exception:
                                pass
                        else:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "system_message",
                                        "text": f"找不到成員 '{kick_name}'",
                                    }
                                )
                            )
                        continue

                    # ── broadcast: owner fires message to all agents at once ───
                    # owner 以廣播模式發送訊息，所有 agent 同時收到 your_turn
                    if msg_type == "broadcast":
                        content = owner_msg.get("content", "").strip()
                        if not content:
                            continue
                        if not room.agents:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "system_message",
                                        "text": "目前沒有 agent 可以廣播，請先用 /add-agent 加入。",
                                    }
                                )
                            )
                            continue
                        timestamp = datetime.now(timezone.utc).isoformat()
                        room.current_round += 1
                        entry = {
                            "agent_id": observer_id,
                            "name": name,
                            "model": "human",
                            "content": f"[broadcast] {content}",
                            "timestamp": timestamp,
                            "round": room.current_round,
                        }
                        files = owner_msg.get("files")
                        if files:
                            entry["files"] = files
                        images = owner_msg.get("images")
                        if images:
                            entry["images"] = images
                        room.history.append(entry)
                        # 重置本回合發言紀錄，讓所有 agent 均可在此廣播回合中發言
                        room.round_speakers = set()
                        if not room.owner_kicked_off:
                            room.owner_kicked_off = True
                            room.topic = content
                        log.info(f"[{room_id}] [broadcast] {name}: {content[:80]}")
                        await self._broadcast(room, {"type": "message", **entry})
                        # 觸發廣播模式輪次，同時通知所有 agent
                        await self._send_broadcast_turn(room)
                        continue

                    # 非以上指令的 owner 訊息，只處理 type == "message" 的情況
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
                    files = owner_msg.get("files")
                    if files:
                        entry["files"] = files
                    images = owner_msg.get("images")
                    if images:
                        entry["images"] = images

                    # ── 私訊路徑：偵測到 #mention 語法 ──────────────────────
                    # 若訊息中含有 #AgentName，將此訊息路由給特定 agent（私訊模式）
                    mentions = _MENTION_RE.findall(content)
                    if mentions:
                        mention_set = set(mentions)
                        private_targets = [
                            a for a in room.agents.values() if a.name in mention_set
                        ]
                        if private_targets:
                            target = private_targets[0]
                            room.current_round += 1
                            entry["round"] = room.current_round
                            hist_idx = len(room.history)
                            room.history.append(entry)
                            # 標記此歷史條目為私密，只有目標 agent 可以看到
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
                            # 只廣播給 observer，其他 agent 不應收到私訊內容
                            await self._broadcast(
                                room,
                                {
                                    "type": "message",
                                    "is_private": True,
                                    "private_to": [target.name],
                                    **entry,
                                },
                                observers_only=True,
                            )
                            if not room.turn_pending:
                                await self._send_your_turn(room, target)
                            continue

                    # ── 一般（公開）訊息路徑 ─────────────────────────────────
                    room.current_round += 1
                    entry["round"] = room.current_round
                    room.history.append(entry)
                    log.info(f"[{room_id}] [owner] {name}: {content[:80]}")
                    await self._broadcast(room, {"type": "message", **entry})

                    # Each owner message starts a new round (also cancels any ongoing broadcast)
                    # owner 每則訊息開啟新回合，同時取消任何進行中的廣播模式
                    room.round_speakers = set()
                    room.broadcast_pending = None

                    # owner 第一則訊息觸發 kickoff，設定討論主題並允許 agent 開始發言
                    if not room.owner_kicked_off and len(room.agents) >= 1:
                        room.owner_kicked_off = True
                        room.topic = content
                        log.info(
                            f"[{room_id}] Owner kickoff! Topic set: {content[:60]}"
                        )

                    # Give first unspoken agent a turn (kickoff or new round)
                    # 將輪次交給第一個尚未發言的 agent，開啟本回合討論
                    if (
                        room.owner_kicked_off
                        and len(room.agents) >= 1
                        and not room.turn_pending
                    ):
                        first_agent = next(iter(room.agents.values()))
                        await self._send_your_turn(
                            room, first_agent, kickoff=not room.owner_kicked_off
                        )
                    elif room.owner_kicked_off and len(room.agents) == 0:
                        # 已 kickoff 但房間中沒有 agent，通知 owner 需要先加入 agent
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "waiting_for_owner",
                                    "message": "目前房間沒有 agent，請用 /add-agent 加入。",
                                }
                            )
                        )

                return

            # ── Agent 路徑：註冊 agent 並進入訊息接收迴圈 ────────────────────
            agent_id = msg.get("agent_id", name)
            model = msg.get("model", "unknown")
            engine = msg.get("engine", "")

            agent = Agent(
                ws=ws,
                agent_id=agent_id,
                name=name,
                model=model,
                room_id=room_id,
                engine=engine,
            )
            room.agents[agent_id] = agent
            identity = agent

            log.info(f"Agent joined | room={room_id} | agent={name} ({model})")

            log.info(
                f"DEBUG: agents_in_room = {[(a.name, a.engine) for a in room.agents.values()]}"
            )
            # 發送 joined 確認給 agent，附帶當前房間內所有 agent 的資訊
            await ws.send(
                json.dumps(
                    {
                        "type": "joined",
                        "role": "agent",
                        "room_id": room_id,
                        "agent_id": agent_id,
                        "agents_in_room": [
                            {
                                "agent_id": a.agent_id,
                                "name": a.name,
                                "model": a.model,
                                "engine": a.engine,
                            }
                            for a in room.agents.values()
                        ],
                    }
                )
            )

            # 廣播 agent_joined 事件給其他所有人（排除自身），更新 UI 的參與者列表
            await self._broadcast(
                room,
                {
                    "type": "agent_joined",
                    "agent_id": agent_id,
                    "name": name,
                    "model": model,
                    "engine": engine,
                    "agents_in_room": len(room.agents),
                },
                exclude_id=agent_id,
            )

            # Notify agents to wait for owner kickoff (if owner hasn't spoken yet)
            # owner 尚未發言時，通知所有 agent 進入等待狀態，不得自行開始討論
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
            # owner 已 kickoff 且目前無輪次進行中，立即給新加入的 agent 發言機會，讓討論延續
            elif not room.turn_pending:
                await self._send_your_turn(room, agent, kickoff=True)

            # Main message loop
            # ── Agent 主訊息接收迴圈 ─────────────────────────────────────────
            async for raw_msg in ws:
                msg = json.loads(raw_msg)

                # ── agent_thinking：轉發思考過程給 observer，並記入 thinking_log ──
                if msg["type"] == "agent_thinking":
                    # Server adds the authoritative turn number
                    # 由伺服器補充權威性的回合編號，確保 observer UI 的顯示正確
                    msg["turn"] = room.current_round
                    # Store in thinking_log with FIFO (max 20 entries per agent)
                    # 以 FIFO 緩衝存入 thinking_log，超過 20 條時移除最舊的一條
                    agent_log = room.thinking_log.setdefault(agent_id, [])
                    agent_log.append(
                        {
                            "turn": room.current_round,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "blocks": msg.get("blocks", []),
                        }
                    )
                    if len(agent_log) > 20:
                        agent_log.pop(0)
                    # Broadcast to observers only (agents must NOT see each other's thinking)
                    # 只轉發給 observer，agent 之間不得互相看到對方的思考過程
                    await self._broadcast(room, msg, observers_only=True)
                    continue

                # ── update_model：agent 熱替換所使用的模型字串 ──────────────
                if msg["type"] == "update_model":
                    new_model = msg.get("model", "").strip()
                    if new_model and new_model != agent.model:
                        log.info(
                            f"[{room_id}] {name} model updated: {agent.model} → {new_model}"
                        )
                        agent.model = new_model
                        model = new_model  # keep local var in sync for entry dicts
                        # 廣播模型更新事件，讓其他人知道此 agent 切換了模型
                        await self._broadcast(
                            room,
                            {
                                "type": "model_updated",
                                "agent_id": agent_id,
                                "name": name,
                                "model": new_model,
                            },
                            exclude_id=agent_id,
                        )
                    continue

                # ── message：agent 完成回覆，處理歷史寫入與輪次推進 ──────────
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
                        "round": room.current_round,
                    }

                    # Check if this reply is part of a private turn
                    # 判斷此回覆是否為私訊輪次的一部分
                    is_private_reply = (
                        room.current_private_for is not None
                        and agent_id in room.current_private_for
                    )

                    hist_idx = len(room.history)
                    room.history.append(entry)
                    if is_private_reply and room.current_private_for is not None:
                        # 私密回覆：記錄可見性並清除私訊輪次狀態
                        room.private_visibility[hist_idx] = (
                            room.current_private_for.copy()
                        )
                        room.current_private_for = None

                    log.info(f"[{room_id}] {name} ({latency_ms}ms): {content[:80]}...")

                    # Private replies go only to observers; public replies go to everyone
                    # 私密回覆只廣播給 observer；公開回覆廣播給所有人
                    if is_private_reply:
                        await self._broadcast(
                            room,
                            {
                                "type": "message",
                                "is_private": True,
                                **entry,
                            },
                            observers_only=True,
                        )
                    else:
                        await self._broadcast(room, {"type": "message", **entry})

                    # Emit turn_end for observers
                    # 發送 turn_end 事件給所有人，附帶延遲毫秒數讓 observer UI 顯示延遲
                    await self._broadcast(
                        room,
                        {
                            "type": "turn_end",
                            "agent_id": agent_id,
                            "name": agent.name,
                            "latency_ms": latency_ms,
                            "turn_number": len(room.history),
                        },
                    )

                    # 廣播 room_state 讓所有 observer 更新 observers 列表
                    # 這讓 TUI 能持續看到最新的觀察者名單
                    await self._broadcast(room, room.room_state_payload())

                    # Mark this agent as having spoken this round
                    # 將此 agent 標記為本回合已發言
                    room.round_speakers.add(agent_id)

                    if is_private_reply:
                        # ── Private round complete: return control to owner ───
                        # 私訊回合結束，重置輪次狀態並等待 owner 下一則訊息
                        room.turn_pending = False
                        room.current_speaker = None
                        log.info(
                            f"[{room_id}] Private round complete. Waiting for owner."
                        )
                        await self._broadcast(
                            room,
                            {
                                "type": "waiting_for_owner",
                                "message": "私訊回覆完成，等待下一則訊息。",
                            },
                        )
                    elif room.broadcast_pending is not None:
                        # ── Broadcast mode: track who's still pending ────────
                        # 廣播模式：從待回覆集合中移除此 agent；
                        # 集合清空時代表所有 agent 均已回覆，通知 owner 可繼續
                        room.broadcast_pending.discard(agent_id)
                        if not room.broadcast_pending:
                            room.broadcast_pending = None
                            room.current_speaker = None
                            log.info(
                                f"[{room_id}] Broadcast round complete. Waiting for owner."
                            )
                            await self._broadcast(
                                room,
                                {
                                    "type": "waiting_for_owner",
                                    "message": "All agents have responded. Waiting for your next message.",
                                },
                            )
                    else:
                        # ── Sequential mode: pass turn to next agent ─────────
                        # 循序模式：尋找下一個尚未發言的 agent 並將輪次傳遞給它；
                        # 若本回合所有 agent 均已發言，通知 owner 等待其下一則訊息
                        room.turn_pending = False
                        next_agent = room.next_speaker(exclude_id=agent_id)
                        if next_agent:
                            await self._send_your_turn(room, next_agent)
                        else:
                            room.current_speaker = None
                            log.info(f"[{room_id}] Round complete. Waiting for owner.")
                            await self._broadcast(
                                room,
                                {
                                    "type": "waiting_for_owner",
                                    "message": "All agents have responded. Waiting for your next message.",
                                },
                            )

                # ── leave：agent 主動離開，跳出接收迴圈 ──────────────────────
                elif msg["type"] == "leave":
                    break

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
        finally:
            # ── 連線清理（finally 區塊，無論正常或異常都會執行）──────────────
            if room is None:
                return

            # Clean up observer
            # observer 斷線：從房間的 observers dict 中移除
            if isinstance(identity, Observer):
                room.observers.pop(identity.observer_id, None)
                log.info(f"Observer left | room={room.room_id} | name={identity.name}")
                return

            # Clean up agent
            # agent 斷線：從房間移除，廣播 agent_left 事件，並處理各種中斷狀態
            if isinstance(identity, Agent):
                agent = identity
                room.agents.pop(agent.agent_id, None)
                remaining = len(room.agents)
                log.info(
                    f"Agent left | room={room.room_id} | agent={agent.name} | remaining={remaining}"
                )

                # 廣播 agent_left 事件，讓 observer UI 更新參與者列表
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
                # 廣播模式清理：若此 agent 尚未回覆就斷線，從 broadcast_pending 移除；
                # 若集合因此清空，代表廣播回合已完成，通知 owner
                if room.broadcast_pending is not None:
                    room.broadcast_pending.discard(agent.agent_id)
                    if not room.broadcast_pending:
                        room.broadcast_pending = None
                        room.current_speaker = None
                        log.info(
                            f"[{room.room_id}] Broadcast round complete (agent left)."
                        )
                        await self._broadcast(
                            room,
                            {
                                "type": "waiting_for_owner",
                                "message": "All agents have responded. Waiting for your next message.",
                            },
                        )

                # If this agent was the target of a private turn, clear the private state
                # 若此 agent 是私訊輪次的目標，清除私訊狀態，防止房間永遠卡在私訊模式
                if (
                    room.current_private_for
                    and agent.agent_id in room.current_private_for
                ):
                    room.current_private_for = None

                # If agent disconnects while thinking, send turn_end so UI unblocks
                # 若 agent 在思考中途斷線，強制發出 turn_end 事件，解除 observer UI 的卡死狀態
                if room.turn_pending and room.current_speaker == agent.agent_id:
                    log.warning(
                        f"Agent {agent.name} disconnected mid-turn — forcing turn_end"
                    )
                    await self._broadcast(
                        room,
                        {
                            "type": "turn_end",
                            "agent_id": agent.agent_id,
                            "name": agent.name,
                            "latency_ms": 0,
                            "aborted": True,
                        },
                    )
                    room.turn_pending = False

                # Reassign turn if the speaker just left (sequential mode only)
                # 循序模式下，若正在發言的 agent 斷線，將輪次移交給下一個 agent；
                # 若已無任何 agent，重置所有輪次狀態
                if room.current_speaker == agent.agent_id:
                    if remaining >= 1:
                        next_agent = next(iter(room.agents.values()))
                        await self._send_your_turn(room, next_agent)
                        log.info(
                            f"Turn reassigned to {next_agent.name} after {agent.name} left"
                        )
                    else:
                        # No agents left — reset turn state entirely
                        # 房間內已無任何 agent，完整重置輪次狀態
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
        async with websockets.serve(
            server.handle_connection,
            host,
            port,
            ping_interval=60,  # 每 60 秒發送一次 WebSocket ping，保持長連線不被 NAT/防火牆切斷
            ping_timeout=300,  # 等待 pong 回應最多 300 秒，適應 agent 長時間計算的情境
        ):
            log.info(f"Server ready. Engines: {server.available_engines}")
            # asyncio.Future() 永遠不會 resolve，讓伺服器持續運行直到收到 Ctrl-C 中斷信號
            await asyncio.Future()
    finally:
        await server.shutdown()
        log.info("All spawned agents terminated.")


if __name__ == "__main__":
    asyncio.run(main())
