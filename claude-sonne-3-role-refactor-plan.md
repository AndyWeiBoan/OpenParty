# claude-sonne-3 — Role Refactor Plan

> **核心立場**：先修信任根（trust root），再整理結構。把 handle_connection() 拆開但不驗證 token，
> 只是把污染點往上移一層，沒有解決問題。

---

## 現狀診斷

### 信任根問題（Root Cause）

```python
# server.py line 620-629 — 現在的全部「驗證」
role     = msg.get("role", "agent")       # 完全由客戶端自稱
is_owner = msg.get("owner", False)        # 任何人送 true 就是 owner
```

這不是 code style 問題，是設計缺陷：
- **任何人** 連進來送 `{"role":"observer","owner":true}` 就成為 room owner
- Owner 可以 broadcast、kick agent、spawn agent —— 全部指令毫無保護
- 即使把 `handle_connection()` 拆成兩個函式，只要路由決策仍依賴這個 `role` 字串，攻擊面不變

---

## 重構目標

| 優先度 | 目標 |
|--------|------|
| P0 | 建立 server-side 身份驗證，消除 client-self-declare 信任 |
| P1 | 以驗證結果決定 handler dispatch，而非以 client 欄位決定 |
| P2 | 引入型別安全的 identity 模型，消除 magic string |
| P3 | 將 connection handler 按角色分離，提高可讀性與可測試性 |

---

## 實作計畫

### Phase 0 — 確立信任根（必須先做，其餘都依賴這步）

**新增 `RoomCredentials` 機制**：

```python
# server.py — 新增
@dataclass
class RoomCredentials:
    owner_token: str          # 建立 room 時 server 產生，回傳給 TUI
    agent_token: str          # bridge 連線時使用（較寬鬆）
    room_id: str
```

**Room 建立流程**：
1. TUI 啟動時，向 server 發送 `{"type": "create_room", "room_id": "..."}`
2. Server 產生 `owner_token = secrets.token_urlsafe(32)` 並回傳
3. TUI 將 token 存入 session，後續每次 handshake 帶上

**驗證 handshake**：
```python
# 修改後的 handle_connection() 頂部
raw = await ws.recv()
msg = json.loads(raw)

presented_token = msg.get("token", "")
room_id = msg.get("room_id", "")
creds = self.room_credentials.get(room_id)

if creds and presented_token == creds.owner_token:
    identity = Identity(role=Role.OWNER, room_id=room_id)
elif creds and presented_token == creds.agent_token:
    identity = Identity(role=Role.AGENT, room_id=room_id)
else:
    identity = Identity(role=Role.OBSERVER, room_id=room_id)  # 無 token = 唯讀觀察者

# 之後 dispatch 完全依賴 identity.role，不再信任 msg 內容
```

---

### Phase 1 — 引入 `Role` enum，消除 magic string

```python
# server.py — 新增
from enum import Enum, auto

class Role(Enum):
    OWNER    = auto()   # 唯一，可下指令
    AGENT    = auto()   # AI agent，可回應訊息
    OBSERVER = auto()   # 唯讀，只收 broadcast

@dataclass
class Identity:
    role: Role
    room_id: str
    name: str = ""
    entity_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
```

**影響範圍**：
- 所有 `if role == "observer"` → `if identity.role == Role.OBSERVER`
- 所有 `if is_owner` → `if identity.role == Role.OWNER`
- `Observer` dataclass 的 `is_owner: bool` 欄位移除，由 `Role` 取代

---

### Phase 2 — 拆分 handler（在 Phase 0/1 完成後才有意義）

`handle_connection()` 在完成身份驗證後，dispatch 到三個獨立函式：

```python
async def handle_connection(self, ws, path):
    identity = await self._authenticate(ws)   # Phase 0 的驗證邏輯
    
    match identity.role:
        case Role.OWNER:
            await self._handle_owner(ws, identity)
        case Role.AGENT:
            await self._handle_agent(ws, identity)
        case Role.OBSERVER:
            await self._handle_observer(ws, identity)
```

**各 handler 職責**：

| Handler | 職責 | 可寫入 |
|---------|------|--------|
| `_handle_owner` | broadcast、kick、spawn、私訊 | 是 |
| `_handle_agent` | 接收任務、回傳 thinking/message | 是（自己的 turn） |
| `_handle_observer` | 只接收 room 廣播 | 否 |

---

### Phase 3 — `Observer` / `Agent` dataclass 清理

```python
# 移除 Observer.is_owner（由 Role 取代）
@dataclass
class Observer:
    ws: WebSocketServerProtocol
    observer_id: str
    name: str
    # is_owner: bool = False  ← 刪除

# Owner 單獨建模（語意更清晰）
@dataclass  
class RoomOwner:
    ws: WebSocketServerProtocol
    owner_id: str
    name: str
```

---

## 遷移影響評估

| 元件 | 需要修改 | 說明 |
|------|----------|------|
| `server.py` | ✅ 大幅修改 | 驗證邏輯、handler 拆分、dataclass 更新 |
| `openparty_tui.py` | ✅ 需修改 | join message 加入 `token` 欄位 |
| `bridge.py` | ✅ 需修改 | agent join handshake 加入 `token` 欄位 |
| `openparty_join.py` | ⚠️ 小幅修改 | join 流程加入 token 取得步驟 |
| `observer_cli.py` | ❌ 不需（廢棄） | 忽略 |

---

## 與其他方案的差異

- **claude-sonne** 的方案：著重 file descriptor leak 和 async 問題，正確但未觸及信任根
- **claude-sonne-2** 的方案：建議拆成兩個 handler，結構改善正確，但若不先解信任根，拆了也沒用
- **本方案**：先解 `msg.get("owner", False)` 這個信任根，再以驗證結果驅動結構拆分 — 順序不同，效果根本不同

---

## 最小可行版本（MVP）

如果只能做一件事：

```python
# server.py — 在 handle_connection() 最頂部加入
OWNER_SECRET = os.environ.get("OPENPARTY_OWNER_SECRET", "")

# 驗證時
if OWNER_SECRET and msg.get("token") == OWNER_SECRET:
    is_owner = True
else:
    is_owner = False  # 完全不信任 msg.get("owner", False)
```

這一個改動就封住了最嚴重的安全漏洞，其餘的結構重構可以漸進進行。
