# OpenParty Identity/Role 系統重構計畫

> 作者：claude-sonne（獨立分析，2026-04-07）
> 針對 `server.py` 現有身份與角色處理邏輯的完整重構方案

---

## 1. 現有問題

### 1.1 身份驗證完全依賴客戶端自報（無伺服器端驗證）

**問題位置：`server.py` lines 620–629**

```python
role = msg.get("role", "agent")     # 客戶端說什麼，伺服器就信什麼
name = msg.get("name", "unknown")
observer_id = msg.get("observer_id", str(uuid.uuid4())[:8])
is_owner = msg.get("owner", False)  # ← 任何人都可以宣稱自己是 owner
```

任意 WebSocket 客戶端只要傳入 `{"type": "join", "role": "observer", "owner": true}` 就能取得 owner 權限。沒有任何 token、密碼、或 server-side 認證。

---

### 1.2 `handle_connection` 函式過於龐大，職責混雜

**問題位置：`server.py` lines 601–963（共 363 行的單一函式）**

`handle_connection` 目前承擔了以下全部責任：
- 解析初始 join 訊息（lines 607–621）
- 決定 role（observer vs agent）（line 620）
- Observer 的初始化與 joined 回應（lines 625–689）
- Owner 指令分發（lines 692–963）：包含 `spawn_agent`、`kick_all`、`kick_agent`、`broadcast`、一般訊息
- Agent 的初始化（lines 965–1037）
- Agent 主訊息迴圈（lines 1039–end）

這導致整個函式難以測試、難以閱讀、且因為 Python 的 closure 範圍，局部變數（如 `name`、`observer_id`）會在不同 role 的路徑中出現命名衝突的風險。

---

### 1.3 Owner 與 Observer 身份混在同一個 `Observer` dataclass

**問題位置：`server.py` lines 109–114**

```python
@dataclass
class Observer:
    ws: WebSocketServerProtocol
    observer_id: str
    name: str
    is_owner: bool = False  # 用布林值區分兩種截然不同的角色
```

Owner 和 Observer 本質上是不同角色，卻用同一個 dataclass 加上 boolean flag 來區分。這讓 room 狀態查詢（例如找出目前 owner）必須迭代整個 `observers` dict 並過濾，而非直接存取。

**問題位置：`server.py` lines 665–674**

```python
owner_name_for_all = ""
if is_owner:
    owner_name_for_all = name
else:
    for old_obs in room.observers.values():  # ← 每次都要 O(n) 搜尋 owner
        if old_obs.is_owner:
            owner_name_for_all = old_obs.name
            break
```

---

### 1.4 Agent 的 `agent_id` 完全由客戶端控制

**問題位置：`server.py` lines 966–978**

```python
agent_id = msg.get("agent_id", name)  # 客戶端傳什麼就用什麼
agent = Agent(ws=ws, agent_id=agent_id, ...)
room.agents[agent_id] = agent  # 可以蓄意覆蓋已存在的 agent
```

惡意客戶端可以指定一個已存在的 `agent_id` 來覆蓋（hijack）現有 agent 的 WebSocket 連線，伺服器沒有任何防護。

---

### 1.5 `is_owner` 欄位用字串 `"owner_kicked_off"` 等狀態名稱散落在 Room 中

**問題位置：`server.py` lines 141–155**

Owner 在 `Room` 中的狀態（`owner_kicked_off`、`current_speaker`、`turn_pending` 等）以多個鬆散的布林值/字串欄位維護，沒有統一的狀態機管理，容易造成狀態不一致。

---

## 2. 建議架構

### 2.1 Token-based Server-side Auth

伺服器在啟動時（或 room 建立時）為 owner 產生一次性 `owner_token`，join 訊息必須附帶此 token 才能取得 owner 身份。Agent 同樣需要帶 `spawn_token`（由 server 的 `_spawn_agent_process` 在 subprocess 啟動時注入）。

```python
import secrets

class RoomTokenRegistry:
    """管理每個房間的 owner token 與 agent spawn token。"""

    def __init__(self):
        self._owner_tokens: dict[str, str] = {}    # room_id -> token
        self._agent_tokens: dict[str, str] = {}    # token -> room_id

    def issue_owner_token(self, room_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._owner_tokens[room_id] = token
        return token

    def verify_owner_token(self, room_id: str, token: str) -> bool:
        return self._owner_tokens.get(room_id) == token

    def issue_agent_spawn_token(self, room_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._agent_tokens[token] = room_id
        return token

    def consume_agent_spawn_token(self, token: str) -> str | None:
        """回傳 room_id 並刪除 token（一次性使用）。"""
        return self._agent_tokens.pop(token, None)
```

Join 訊息格式變更（新增 `auth_token` 欄位）：

```python
# Observer/Owner join
{
    "type": "join",
    "role": "observer",
    "room_id": "room-1",
    "name": "Alice",
    "auth_token": "<owner_token>"   # 有 token 才授予 owner 身份
}

# Agent join（由 bridge.py 帶入 spawn token）
{
    "type": "join",
    "role": "agent",
    "room_id": "room-1",
    "name": "Claude",
    "spawn_token": "<one-time-spawn-token>"
}
```

---

### 2.2 分離 Handler 函式

`handle_connection` 重構為薄薄的 dispatcher，將具體邏輯委派給獨立的 handler 函式：

```python
async def handle_connection(self, ws: WebSocketServerProtocol):
    """只負責初始 join 解析與身份驗證，然後交棒給專責 handler。"""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await _send_error(ws, "Invalid or timeout on first message")
        return

    if msg.get("type") != "join":
        await _send_error(ws, "First message must be 'join'")
        return

    room_id = msg.get("room_id", "default")
    role = msg.get("role", "agent")
    room = self.get_or_create_room(room_id)

    if role == "observer":
        identity = self._auth_observer(msg, room)
        if identity is None:
            await _send_error(ws, "Observer auth failed")
            return
        await self._handle_observer_session(ws, identity, room)

    elif role == "agent":
        identity = self._auth_agent(msg, room)
        if identity is None:
            await _send_error(ws, "Agent spawn token invalid or expired")
            return
        await self._handle_agent_session(ws, identity, room)

    else:
        await _send_error(ws, f"Unknown role: {role!r}")
```

```python
async def _handle_observer_session(
    self,
    ws: WebSocketServerProtocol,
    obs: "Observer",
    room: Room,
):
    """純 observer 邏輯：發送 joined，進入唯讀迴圈。"""
    await self._send_joined_observer(ws, obs, room)
    async for raw_msg in ws:
        # 非 owner 靜默忽略
        pass

async def _handle_owner_session(
    self,
    ws: WebSocketServerProtocol,
    owner: "Owner",
    room: Room,
):
    """Owner 指令迴圈：獨立函式，接收 spawn/kick/broadcast/message。"""
    await self._send_joined_owner(ws, owner, room)
    async for raw_msg in ws:
        try:
            msg = json.loads(raw_msg)
        except Exception:
            continue
        await self._dispatch_owner_command(ws, owner, room, msg)

async def _handle_agent_session(
    self,
    ws: WebSocketServerProtocol,
    agent: Agent,
    room: Room,
):
    """Agent 訊息迴圈：thinking/update_model/message 的分發。"""
    await self._send_joined_agent(ws, agent, room)
    async for raw_msg in ws:
        try:
            msg = json.loads(raw_msg)
        except Exception:
            continue
        await self._dispatch_agent_message(ws, agent, room, msg)
```

---

## 3. 資料模型變更

### 3.1 新增 `Role` Enum

```python
from enum import Enum

class Role(str, Enum):
    OWNER = "owner"
    OBSERVER = "observer"
    AGENT = "agent"
```

### 3.2 拆分 `Owner` 與 `Observer` Dataclass

```python
@dataclass
class Owner:
    ws: WebSocketServerProtocol
    owner_id: str
    name: str
    room_id: str
    role: Role = Role.OWNER

@dataclass
class Observer:
    ws: WebSocketServerProtocol
    observer_id: str
    name: str
    room_id: str
    role: Role = Role.OBSERVER

@dataclass
class Agent:
    ws: WebSocketServerProtocol
    agent_id: str
    name: str
    model: str
    room_id: str
    engine: str = ""
    role: Role = Role.AGENT
```

### 3.3 `Room` dataclass 新增 `owner` 直接引用

```python
@dataclass
class Room:
    room_id: str
    owner: Optional["Owner"] = None          # ← 直接引用，不需迭代 observers
    observers: dict[str, Observer] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    current_speaker: Optional[str] = None
    topic: str = ""
    rolling_summary: str = ""
    turn_started_at: float = 0.0
    owner_kicked_off: bool = False
    turn_pending: bool = False
    current_round: int = 0
    # ... 其餘現有欄位不變
```

這樣 owner 查詢從 O(n) 變成 O(1)：

```python
# 舊方式（lines 665–674）
for old_obs in room.observers.values():
    if old_obs.is_owner:
        owner_name = old_obs.name
        break

# 新方式
owner_name = room.owner.name if room.owner else ""
```

---

## 4. 遷移步驟（漸進式，不破壞現有功能）

### Phase 1：防禦性加固（可立即合併，不改介面）

**目標：在不改動任何 WebSocket 協議的前提下，加入最基本的防護。**

1. **限制 `agent_id` 不能覆蓋現有 agent（`server.py` line 978）**

```python
# 現行
room.agents[agent_id] = agent

# 改為
if agent_id in room.agents:
    existing = room.agents[agent_id]
    if existing.ws != ws:
        log.warning(f"[{room_id}] Duplicate agent_id '{agent_id}', rejecting new connection")
        await _send_error(ws, f"agent_id '{agent_id}' already in use")
        return
```

2. **Owner 取代邏輯加入 join 超時（`server.py` line 607）**

```python
raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
```

3. **Owner 指令加入 enum 校驗，拒絕未知 type（`server.py` line 700）**

```python
VALID_OWNER_COMMANDS = {"spawn_agent", "kick_all", "kick_agent", "broadcast", "message"}
if msg_type not in VALID_OWNER_COMMANDS:
    log.warning(f"[{room_id}] Unknown owner command: {msg_type!r}")
    continue
```

---

### Phase 2：資料模型分離（可獨立 PR）

**目標：拆分 `Owner` / `Observer` dataclass，Room 加入直接的 `owner` 欄位。**

1. 新增 `Role` enum 及 `Owner` dataclass（不刪除舊 `Observer.is_owner` 欄位，維持向後相容）。
2. `Room` 加入 `owner: Optional[Owner] = None`。
3. 在 join 路徑中，若 `is_owner=True`，同時寫入 `room.owner`（雙寫過渡期）。
4. 將所有 `room.observers` 中搜尋 owner 的迭代改為 `room.owner`。
5. 確認測試全部通過後，移除 `Observer.is_owner` 欄位與舊的迭代邏輯。

---

### Phase 3：Handler 函式分離（獨立 PR）

**目標：將 363 行的 `handle_connection` 拆成多個函式。**

1. 新增 `_handle_observer_session`、`_handle_owner_session`、`_handle_agent_session` 三個 coroutine 函式，內容先從 `handle_connection` 直接搬移（行為不變）。
2. `handle_connection` 改為呼叫上述三個函式的 dispatcher。
3. 新增 `_dispatch_owner_command`、`_dispatch_agent_message` 兩個分發函式（從原本的 if-elif 鏈搬移）。
4. 每個函式加上 docstring 及型別標注。

---

### Phase 4：Token-based Auth（獨立 PR，需配合 bridge.py / openparty_tui.py 一起改）

**目標：owner token 及 agent spawn token 上線。**

1. `PartyServer.__init__` 建立 `self.token_registry = RoomTokenRegistry()`。
2. 提供一個 HTTP 端點（或 CLI 啟動參數）讓使用者取得 `owner_token`。
3. `_spawn_agent_process`（bridge 子程序啟動）在命令列注入 `--spawn-token`（`server.py` 中的 `_spawn_agent_process` 函式）。
4. `bridge.py` 在 join 訊息中帶入 `spawn_token`。
5. `handle_connection` 的 `_auth_agent` 呼叫 `consume_agent_spawn_token`，失敗則拒絕。
6. `openparty_tui.py` 在 connect 時帶入 `owner_token`。

---

## 5. 安全改善

### 5.1 Owner 身份偽冒防護

- **現狀**：任何人傳 `"owner": true` 即可獲得 owner 權限（line 629）。
- **改善**：Phase 4 的 token 機制。即使是同個 room_id，沒有正確 token 就無法成為 owner。
- **短期緩解**（Phase 1）：加入 IP 白名單檢查（`ws.remote_address[0]` 限制只允許 localhost），適合本機部署場景。

### 5.2 Agent Hijacking 防護

- **現狀**：`agent_id` 完全由客戶端控制，可覆蓋現有 agent（line 978）。
- **改善**：Phase 1 的重複 agent_id 拒絕邏輯 + Phase 4 的 spawn token（只有 server 發出的 token 才能成功 join 為 agent）。

### 5.3 訊息 Flooding 防護

- **現狀**：owner 訊息迴圈沒有任何速率限制（lines 692–963）。
- **改善**：在 `_dispatch_owner_command` 加入 per-connection 速率限制器。

```python
from collections import deque

class RateLimiter:
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._timestamps: deque[float] = deque()

    def is_allowed(self) -> bool:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > self.period:
            self._timestamps.popleft()
        if len(self._timestamps) < self.max_calls:
            self._timestamps.append(now)
            return True
        return False
```

### 5.4 Join 訊息欄位長度限制

- **現狀**：`name`、`room_id` 等欄位沒有任何長度或字符校驗。
- **改善**：在 dispatcher 入口加入校驗。

```python
def _validate_join(msg: dict) -> str | None:
    """回傳錯誤字串，或 None 代表通過。"""
    name = msg.get("name", "")
    if not isinstance(name, str) or not name or len(name) > 64:
        return "name must be a non-empty string ≤ 64 chars"
    room_id = msg.get("room_id", "")
    if not isinstance(room_id, str) or not re.match(r'^[\w\-]{1,64}$', room_id):
        return "room_id must match [\\w-]{1,64}"
    return None
```

### 5.5 WebSocket 連線數上限

- **現狀**：沒有任何單一 room 或全域的連線數限制。
- **改善**：`get_or_create_room` 加入 room 數上限；`handle_connection` 加入全域連線計數器（atomic counter）。

---

## 總結優先序

| 優先 | Phase | 目標 | 風險 |
|------|-------|------|------|
| P0 | Phase 1 | agent_id 重複拒絕、join 超時、指令白名單 | 極低，只加防護 |
| P1 | Phase 2 | Owner/Observer 資料模型分離 | 低，可雙寫過渡 |
| P2 | Phase 3 | Handler 函式拆分 | 中，需整體回歸測試 |
| P3 | Phase 4 | Token-based auth | 高，需同步修改 bridge.py + TUI |

Phase 1 可在不改任何協議的前提下立即上線，Phase 4 需要協調多個檔案的變更，建議安排在 Phase 2/3 完成並穩定後再進行。
