# Persistence

> Status: 待設計（目前尚無持久化機制）  
> Last updated: 2026-04-08

---

## 現狀

OpenParty server 目前所有狀態均存於記憶體（in-memory），server 重啟後全部消失：

- `Room` 物件（含 `owner_client_id`、`observers`、`agents`、`history`）
- Session token 與 client identity 的對應關係
- 對話歷史、rolling summary

Client 端目前也沒有持久化（token 存放機制尚未實作）。

---

## 需要持久化的項目

### Server 端

| 資料 | 說明 | 優先度 |
|------|------|--------|
| `client_id → name` mapping | 記錄每個 client 的顯示名稱 | P1 |
| `room_id → owner_client_id` | 讓 room 重啟後 owner 仍有效 | P0 |
| `room.history` | 對話歷史 | P1 |
| `room.rolling_summary` | LLM rolling summary | P1 |
| `room` 其他 metadata（topic 等） | 房間配置 | P2 |

### Client 端

| 資料 | 路徑 | 說明 |
|------|------|------|
| `client_id` + `session_token` | `~/.config/openparty/token.json` | 跨 session 保持身份 |
| 加入過的 rooms 清單 | `~/.config/openparty/token.json` | 方便快速重連 |

Client 端格式詳見 [feat-identity-and-authorization.md](./feat-identity-and-authorization.md)。

---

## 實作方向（待決定）

### 方案 A：JSON 檔案（最簡單）

```
~/.config/openparty/server_state.json
```

```json
{
  "clients": {
    "a3f8c2d1": { "name": "Alice", "joined_at": "2026-04-08T10:00:00Z" }
  },
  "rooms": {
    "room-abc": {
      "owner_client_id": "a3f8c2d1",
      "created_at": "2026-04-08T10:00:00Z"
    }
  }
}
```

**優點**：零依賴、人類可讀、易除錯  
**缺點**：concurrent write 問題，不適合高頻寫入（history 每條訊息都要寫）

### 方案 B：SQLite（推薦）

```
~/.config/openparty/state.db
```

```sql
CREATE TABLE clients (
    client_id   TEXT PRIMARY KEY,
    name        TEXT,
    joined_at   TIMESTAMP
);

CREATE TABLE rooms (
    room_id          TEXT PRIMARY KEY,
    owner_client_id  TEXT REFERENCES clients(client_id),
    created_at       TIMESTAMP,
    topic            TEXT DEFAULT ''
);

CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id    TEXT REFERENCES rooms(room_id),
    client_id  TEXT,
    content    TEXT,
    role       TEXT,
    created_at TIMESTAMP
);
```

**優點**：Python 標準庫內建（`import sqlite3`）、支援 concurrent read、schema 明確  
**缺點**：需要遷移邏輯、稍微增加複雜度

### 方案 C：HMAC Stateless Token（session token 不需持久化）

`session_token` 本身**不需要**持久化——server 不儲存任何 token，驗證邏輯是純數學運算：

```
verify(client_id, token) = HMAC(SERVER_SECRET, client_id) == token
```

只要 `SERVER_SECRET` 固定（存於環境變數），server 重啟後可驗證任何舊 token，無需 DB。

```python
# server.py 啟動時
SERVER_SECRET = os.environ.get("OPENPARTY_SERVER_SECRET")
if not SERVER_SECRET:
    raise ValueError("OPENPARTY_SERVER_SECRET must be set for persistent identity")
```

這解決了 session token 的問題，但 `room.owner_client_id` 仍需持久化（room 是 in-memory 的）。

---

## 建議實作路線

```
Phase 1（現在）：
  - Client 端：實作 ~/.config/openparty/token.json 讀寫
  - Server 端：OPENPARTY_SERVER_SECRET env var（stateless token）
  - Room owner：in-memory，重啟後需重建 room

Phase 2（之後）：
  - Server 端加入 SQLite 持久化 rooms + clients
  - 支援 server 重啟後 owner 身份恢復

Phase 3（未來）：
  - Message history 持久化
  - Rolling summary 持久化
```

---

## 注意事項

- Session token 的持久性完全依賴 `OPENPARTY_SERVER_SECRET` 固定，若 env var 改變，所有 client 的 token 立即失效
- SQLite 在 async context 需要使用 `aiosqlite` 或在 thread pool 中執行，避免 blocking event loop
- Client 端的 `token.json` 應設為 `chmod 600`（僅 owner 可讀）
