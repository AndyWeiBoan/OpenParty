# Feature: Identity & Authorization

> Status: Design finalized, implementation pending  
> Last updated: 2026-04-08

---

## 問題背景

目前 `server.py` 的身份識別完全依賴客戶端自報：

```python
is_owner = msg.get("owner", False)  # 任何人送 true 就是 owner
```

任何 WebSocket client 傳入 `{"owner": true}` 即可取得 owner 權限，沒有任何 server-side 驗證。

---

## 設計原則

**身份識別（Identity）與授權（Authorization）完全分離：**

- `session_token` → 解決「你是誰」（identity）
- `room.owner_client_id` → 解決「誰是 owner」（authorization，server 說了算）

這兩件事不互相依賴。拿到 session token 不代表任何權限，owner 資格由 server 根據 room 建立時的 client_id 決定。

---

## Session Token 設計

### 發放邏輯

```
client 連線時：
  沒帶 token / token 驗算失敗  → 當作新 client，發新 session_token（以 observer 加入）
  token 驗算通過               → 恢復對應身份（client_id 保持不變）

server 根據 room.owner_client_id 決定角色：
  client_id == room.owner_client_id → owner
  其他                               → observer
```

> **注意**：採用 HMAC stateless 驗證，server 不儲存 token，任何 token 都可以即時驗算
> （`HMAC(SERVER_SECRET, client_id) == token`），沒有「未知 token」的概念——只有
> 「驗算通過」或「驗算失敗」兩種結果。

### Token 格式

Server 使用 HMAC-SHA256 產生 session token，stateless 驗證（不需要 DB）：

```python
import hmac, hashlib, secrets, os

SERVER_SECRET = os.environ.get("OPENPARTY_SERVER_SECRET", secrets.token_hex(32))

def issue_session_token(client_id: str) -> str:
    return hmac.new(
        SERVER_SECRET.encode(),
        client_id.encode(),
        hashlib.sha256
    ).hexdigest()

def verify_session_token(client_id: str, token: str) -> bool:
    expected = issue_session_token(client_id)
    return secrets.compare_digest(expected, token)  # 防 timing attack
```

> **注意**：`SERVER_SECRET` 必須固定（存於環境變數），否則 server 重啟後所有 token 失效。

### Client 儲存位置

```
~/.config/openparty/token.json
```

```json
{
  "client_id": "a3f8c2d1",
  "session_token": "e3b0c44298fc1c149afb...",
  "name": "Alice",
  "rooms": {
    "room-abc": {
      "is_owner": true,
      "joined_at": "2026-04-08T10:00:00Z"
    }
  }
}
```

- `name` 存在 client 端，連線時帶給 server，可隨時由用戶修改（見 [Name 可修改](#name-可修改)）
- `session_token` 由 server 發放，下次連線帶回，讓 server 識別身份
- `rooms[room_id].is_owner` 是 server `joined` 回應的 cache，TUI 用來決定是否顯示 owner 控制項；每次 join 後以 server 回傳的 `joined.is_owner` 覆寫，實際授權仍以 server 的 `room.owner_client_id` 為準

---

## Owner 授權設計

### Room 建立

建立 room 的 client 的 `client_id` 由 server 記錄為 owner：

```python
@dataclass
class Room:
    room_id: str
    owner_client_id: str   # server 在 create_room 時寫入，不可由 client 修改
    observers: dict[str, Observer] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    # ...
```

### Server 端 `ClientIdentity` Dataclass

```python
@dataclass
class ClientIdentity:
    client_id: str
    name: str
    session_token: str    # HMAC token，驗算用
```

Server 在記憶體中以 `client_id` 為 key 維護 `ClientIdentity`，`name` 可覆寫。

### 每次連線的角色判斷與 `name` 處理

```python
async def handle_connection(self, ws):
    msg = await ws.recv()
    client_id, is_new = self._resolve_identity(msg)
    name = msg.get("name", "unknown")[:64]  # 長度限制

    # 更新或建立 ClientIdentity（name 以本次連線帶入的值為準）
    self.clients[client_id] = ClientIdentity(
        client_id=client_id,
        name=name,
        session_token=issue_session_token(client_id),
    )

    # 首次 / 驗算失敗：先回傳新 session token
    if is_new:
        await ws.send(json.dumps({
            "type": "session",
            "session_token": self.clients[client_id].session_token,
            "client_id": client_id,
        }))

    msg_type = msg.get("type")
    if msg_type == "create_room":
        room = self._create_room(msg.get("room_id"), owner_client_id=client_id)
        is_owner = True
    else:  # join
        room = self.get_room(msg.get("room_id"))
        is_owner = bool(room and room.owner_client_id == client_id)

    # 發送 joined（每次連線都發，不論新舊）
    await ws.send(json.dumps({
        "type": "joined",
        "client_id": client_id,
        "room_id": room.room_id if room else None,
        "is_owner": is_owner,
        "observers": [...],
        "history": [...],
    }))

    if is_owner:
        await self._handle_owner_session(ws, client_id, room)
    else:
        await self._handle_observer_session(ws, client_id, room)
```

**連線時序：**
```
首次 / 驗算失敗：  session → joined → 進入 session loop
回訪（驗算通過）：          joined → 進入 session loop
```

**`name` 優先順序**：每次連線以 msg 帶入的 `name` 覆寫 server 記憶體，`token.json` 的 `name` 作為 default 帶入。

### `_resolve_identity()` 邏輯

```python
def _resolve_identity(self, msg: dict) -> tuple[str, bool]:
    """
    回傳 (client_id, is_new)
      - is_new=True  → 需要回傳新 session_token 給 client
      - is_new=False → token 驗算通過，恢復舊身份
    """
    presented_client_id = msg.get("client_id", "")
    presented_token = msg.get("session_token", "")

    if presented_client_id and presented_token:
        if verify_session_token(presented_client_id, presented_token):
            return presented_client_id, False   # 驗算通過，恢復身份
    
    # 沒帶 token 或驗算失敗 → 永遠產生新身份（不沿用舊 client_id）
    # 安全考量：驗算失敗表示 client_id 可能是偽造的，沿用會讓攻擊者劫持他人身份
    return secrets.token_hex(8), True
```

> **注意**：`client_id` 由 client 自行生成（`secrets.token_hex(8)`，16 chars hex）。
> 碰撞機率極低（2^64 空間），可接受。若未來需要更強保證，改由 server 生成並回傳。

---

## Name 可修改

`name` 是顯示用的資料，與 identity（`client_id`）無關，應允許用戶在任何時候修改。

### 修改方式

client 連線後可發送 `update_name` 訊息：

```json
{
  "type": "update_name",
  "name": "Bob"
}
```

**驗證規則**：`name` 必須是非空字串，長度 ≤ 64 chars，否則 server 回傳 error 並忽略。

server 更新記憶體中的 `ClientIdentity.name`，廣播給同 room 所有人：

```json
{
  "type": "name_updated",
  "client_id": "a3f8c2d1",
  "old_name": "Alice",
  "new_name": "Bob"
}
```

client 端同步更新 `~/.config/openparty/token.json` 裡的 `name` 欄位。

---

## WebSocket Handshake 格式變更

### Create Room 訊息（client → server，建立房間）

```json
{
  "type": "create_room",
  "room_id": "room-abc",
  "name": "Alice",
  "client_id": "a3f8c2d1",
  "session_token": "e3b0c44..."
}
```

建立 room 的 client 自動成為 owner：`room.owner_client_id = client_id`。

Server 回應（依序）：`session`（若 is_new）→ `joined`（含 `is_owner: true`）。

### Join 訊息（client → server，加入現有房間）

```json
{
  "type": "join",
  "room_id": "room-abc",
  "name": "Alice",
  "client_id": "a3f8c2d1",
  "session_token": "e3b0c44..."
}
```

- 首次連線：`client_id` 由 client 自行生成（`secrets.token_hex(8)`），`session_token` 留空
- 回訪連線：帶上 `~/.config/openparty/token.json` 裡的 `client_id`、`session_token`、`name`

### Joined 回應（server → client，每次成功加入後）

```json
{
  "type": "joined",
  "client_id": "a3f8c2d1",
  "room_id": "room-abc",
  "is_owner": true,
  "observers": [...],
  "history": [...]
}
```

- `is_owner`：server 告知 client 它在這個 room 的角色，client 端據此顯示/隱藏 owner 控制項
- client 端可將 `is_owner` 存入 `token.json` 的 `rooms` 欄位作為 cache（見下方）

### Session 回應（server → client，首次或 token 驗算失敗時）

```json
{
  "type": "session",
  "client_id": "a3f8c2d1",
  "session_token": "e3b0c44..."
}
```

`session` 訊息在 `joined` **之前**發送，client 收到後儲存到 `token.json`，再進行後續的 room 操作。

---

## 多裝置行為

每台機器有獨立的 `~/.config/openparty/token.json`，因此同一個人用兩台機器連線會產生兩個不同的 `client_id`。

**Owner 在多裝置的行為：**

- 機器 A 建立 room → `room.owner_client_id = client_id_A`
- 機器 B 連進同一 room → `client_id_B`，server 判斷為 observer

**想在另一台機器繼續以 owner 身份使用：** 複製 `~/.config/openparty/token.json` 到新機器即可（`client_id` 和 `session_token` 不變）。

這是**設計行為**，不是 bug。OpenParty 目前不支援多裝置自動同步 owner 身份，這屬於持久化功能的範疇（見 [persistence.md](./persistence.md)）。

---

## Agent Role

Agent 的身份識別邏輯與 observer/owner **不同**，不在本文件範疇內：

- Agent 由 owner 的 `spawn_agent` 指令觸發，server 內部啟動 subprocess
- Agent 連線時帶入 server 發放的一次性 `spawn_token`（見原始重構計畫）
- `_resolve_identity()` 只處理 observer/owner，agent 走獨立的 `_auth_agent()` 路徑

> 詳細設計見 `claude-sonne-role-refactor-plan.md` Section 2.1 的 `RoomTokenRegistry`。

---

## 參考設計

| 專案 | 機制 | 相關度 |
|------|------|--------|
| tmate | `session_token` / `session_token_ro`，建立時由 server 發放 | ⭐⭐⭐ |
| sshx | `HMAC(server_secret, session_name)` stateless 驗證 | ⭐⭐⭐ |
| Jitsi Meet | `enable-auto-owner`：第一個進房 = moderator；JWT 時改用 claim | ⭐⭐ |
| socket.io | `sessionID`：無 token 時自動發新 ID | ⭐⭐ |

---

## 實作優先序

| 優先 | 工作項目 | 改動範圍 |
|------|---------|---------|
| P0 | `server.py`：`_resolve_identity()` + `issue_session_token()` | server.py |
| P0 | `openparty_tui.py`：讀寫 `~/.config/openparty/token.json` | openparty_tui.py |
| P1 | `Room` dataclass 加入 `owner_client_id` 欄位 | server.py |
| P1 | `handle_connection` 改為 dispatcher | server.py |
| P2 | 移除 `is_owner: bool` 欄位，由 `room.owner_client_id` 取代 | server.py |

> 持久化機制另見 [persistence.md](./persistence.md)

---

## TODO List & 驗收標準

### P0 — Server: `_resolve_identity()` + `issue_session_token()`

**TODO**
- [ ] 新增 `SERVER_SECRET` 環境變數讀取（啟動時若未設定應 warn 或 raise）
- [ ] 實作 `issue_session_token(client_id)` → HMAC-SHA256 hexdigest
- [ ] 實作 `verify_session_token(client_id, token)` → `secrets.compare_digest`
- [ ] 實作 `_resolve_identity(msg)` → `(client_id, is_new)`
- [ ] 新增 `ClientIdentity` dataclass（`client_id`, `name`, `session_token`）
- [ ] `handle_connection` 依 `is_new` 決定是否回傳 `session` 訊息
- [ ] `handle_connection` 依 `msg.type` dispatch `create_room` / `join`
- [ ] `handle_connection` 每次連線均回傳 `joined`（含 `is_owner`）

**驗收標準**
- 新連線（無 token）：server 回傳 `session` 訊息，含新 `client_id` 與 `session_token`
- 回訪連線（token 正確）：server 不回傳 `session`，直接回傳 `joined`，`client_id` 與上次相同
- token 驗算失敗（篡改 token 或 client_id）：server 視為新連線，產生全新 `client_id`，**不沿用** presented `client_id`
- 任何連線傳入 `{"owner": true}` 無效：owner 資格僅由 `room.owner_client_id == client_id` 決定
- `SERVER_SECRET` 固定時，server 重啟後舊 token 仍可驗算通過

---

### P0 — Client: `~/.config/openparty/token.json` 讀寫

**TODO**
- [ ] 首次連線：client 自行生成 `client_id = secrets.token_hex(8)`，`session_token` 留空
- [ ] 收到 `session` 訊息後，將 `client_id` + `session_token` 寫入 `token.json`
- [ ] 收到 `joined` 訊息後，更新 `token.json` 的 `rooms[room_id].is_owner` 與 `joined_at`
- [ ] 回訪連線：從 `token.json` 讀取 `client_id`、`session_token`、`name`，帶入 handshake
- [ ] `token.json` 建立時設定 `chmod 600`

**驗收標準**
- 首次加入後，`~/.config/openparty/token.json` 存在且包含正確的 `client_id`、`session_token`、`name`
- 重啟 TUI 後重新連線，`client_id` 與上次相同（身份恢復）
- `token.json` 的 `rooms[room_id].is_owner` 在每次 join 後以 server 回傳值覆寫
- `token.json` 的權限為 `600`（其他用戶不可讀）

---

### P1 — Server: `Room` dataclass + `handle_connection` dispatcher

**TODO**
- [ ] `Room` dataclass 新增 `owner_client_id: str` 欄位
- [ ] `create_room` 時寫入 `room.owner_client_id = client_id`
- [ ] `handle_connection` 的角色判斷改為 `room.owner_client_id == client_id`，移除 `msg.get("owner")`
- [ ] `handle_connection` 驗證完畢後 dispatch 到 `_handle_owner_session` / `_handle_observer_session`

**驗收標準**
- 建立 room 的 client 收到 `joined.is_owner = true`
- 其他 client 加入同一 room 收到 `joined.is_owner = false`，即使傳入 `{"owner": true}` 也無效
- Server 重啟前後 owner 身份由 `room.owner_client_id` 決定（in-memory 階段重啟後需重建 room，屬預期行為）

---

### P1 — Server: `update_name` 支援

**TODO**
- [ ] `_handle_owner_session` 與 `_handle_observer_session` 均處理 `update_name` 訊息
- [ ] 驗證 `name`：非空字串，長度 ≤ 64 chars；不符合回傳 `error` 並忽略
- [ ] 更新 `ClientIdentity.name`
- [ ] 廣播 `name_updated`（含 `client_id`、`old_name`、`new_name`）給同 room 所有人

**驗收標準**
- 合法 `update_name` → 同 room 所有 client 收到 `name_updated` 廣播
- 空字串 / 超過 64 chars → server 回傳 `error`，`name` 不變，無廣播
- Owner 與 observer 均可修改自己的名字

---

### P2 — Server: 移除 `Observer.is_owner` 欄位

**TODO**
- [ ] `Observer` dataclass 移除 `is_owner: bool` 欄位
- [ ] 所有使用 `observer.is_owner` 的地方改為 `room.owner_client_id == observer.observer_id`

**驗收標準**
- `Observer` dataclass 不含 `is_owner` 欄位
- 所有 owner 判斷統一使用 `room.owner_client_id`，無例外
