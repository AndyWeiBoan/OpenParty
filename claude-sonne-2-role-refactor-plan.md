# OpenParty 身份識別重構計畫
**Author**: claude-sonne-2  
**Date**: 2026-04-07

---

## 問題診斷

目前 `server.py` 的身份識別存在兩個獨立的問題，必須分開處理：

### 問題 1：信任根錯誤（Security）
```python
role = msg.get("role", "agent")      # 客戶端自稱
is_owner = msg.get("owner", False)   # 客戶端自稱
```
身份完全由客戶端聲明，server 無任何驗證。任何人都能宣稱是 owner。

### 問題 2：程式碼結構混亂（Maintainability）
`handle_connection()` 內用 `role` 字串分叉成 agent / observer 兩條完全不同的邏輯路徑，混在同一個函式裡，邊界不清、難以測試。

---

## 重構方向

**原則：先修安全，再整理結構，順序不可顛倒。**

---

## Phase 1：建立 Server-Side Token 驗證

### 1.1 Owner Token 機制

Server 啟動時生成一個隨機 `owner_token`，並以某種安全方式傳遞給合法的 owner（例如印在 stdout、寫入暫存檔、或通過 CLI flag 傳入 TUI）。

```python
# server.py RoomServer.__init__
import secrets
self.owner_token: str = secrets.token_urlsafe(32)
```

啟動時 log：
```
[SERVER] Owner token: <token>  (pass this to openparty_tui.py --owner-token <token>)
```

### 1.2 TUI 側攜帶 Token

`openparty_tui.py` 新增 `--owner-token` CLI 參數，加入 room 時在 handshake payload 中附上：

```python
{
    "type": "join",
    "role": "observer",
    "owner": True,
    "owner_token": "<token>",   # 新增
    ...
}
```

### 1.3 Server 驗證 Token

Server 收到 `owner: True` 時比對 token：

```python
if msg.get("owner", False):
    provided_token = msg.get("owner_token", "")
    if not secrets.compare_digest(provided_token, self.owner_token):
        await ws.send(json.dumps({"type": "error", "message": "Invalid owner token"}))
        return
    is_owner = True
else:
    is_owner = False
```

`secrets.compare_digest` 防止 timing attack。

---

## Phase 2：Handler 分離（結構重構）

驗證通過後，根據已確認的身份 dispatch 到獨立 handler：

```python
async def handle_connection(self, ws: WebSocketServerProtocol):
    raw = await ws.recv()
    msg = json.loads(raw)

    if msg.get("type") != "join":
        await ws.send(json.dumps({"type": "error", "message": "First message must be 'join'"}))
        return

    role = msg.get("role", "agent")
    room = self.get_or_create_room(msg.get("room_id", "default"))

    if role == "observer":
        is_owner = await self._verify_owner(ws, msg)  # 驗證 token，回傳 bool
        await self.handle_observer_connection(ws, room, msg, is_owner)
    else:
        await self.handle_agent_connection(ws, room, msg)
```

### 新函式職責

| 函式 | 職責 |
|------|------|
| `handle_connection()` | 只做 routing，不含業務邏輯 |
| `_verify_owner()` | token 比對，回傳 bool，失敗時自動送 error |
| `handle_observer_connection()` | observer/owner 的完整生命週期 |
| `handle_agent_connection()` | agent 的完整生命週期 |

---

## Phase 3：Magic String 消除（錦上添花）

定義 `ConnectionRole` 和 `MessageType` enum，消除散落各處的魔法字串：

```python
from enum import StrEnum

class ConnectionRole(StrEnum):
    AGENT = "agent"
    OBSERVER = "observer"

class MessageType(StrEnum):
    JOIN = "join"
    JOINED = "joined"
    YOUR_TURN = "your_turn"
    MESSAGE = "message"
    LEAVE = "leave"
    SPAWN_AGENT = "spawn_agent"
    AGENT_THINKING = "agent_thinking"
    # ... etc
```

---

## 實作優先序

| 優先 | 項目 | 理由 |
|------|------|------|
| P0 | Phase 1 Token 驗證 | 安全漏洞，必須先修 |
| P1 | Phase 2 Handler 分離 | 結構問題，修完 P0 順手做 |
| P2 | Phase 3 StrEnum | 技術債，有空再清 |

---

## 注意事項

- Token 驗證加入後，舊版 TUI（無 `--owner-token`）會無法以 owner 身份加入，需同步更新 `openparty_tui.py`
- Agent 身份目前不需要 token（agent 只能發言，無法 spawn/kick），但未來若有需要可在同一架構下擴充
- `secrets.compare_digest` 是必須的，不能用 `==` 比較 token
