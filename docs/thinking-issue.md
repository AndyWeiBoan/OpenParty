# Thinking Stream — Phase 1 已知問題與矛盾

> 實作前需解決的設計缺口，依嚴重程度排列。

---

## 嚴重（會直接卡住實作）

### I-1：`_call_claude()` 無法存取 `ws`

**位置**：`bridge.py:446`

`_call_claude()` 是獨立 method，不持有 WebSocket 引用。
Phase 1 要在 `async for message in query(...)` 收到 `AssistantMessage` 時即時送出 `agent_thinking` 事件，但 `ws` 是 `run()` 的局部變數，`_call_claude()` 根本看不到它。

**修法**：將簽名改為 `_call_claude(self, prompt, ws)`，或在 `run()` 建立連線後存進 `self.ws`。

---

### I-2：OpenCode SSE 訂閱與 `session_id` 建立的時序

**位置**：`thinking-stream.md Q6`、`bridge.py OpenCodeClient`

文件說「SSE 為主要通道，POST 只是 fallback」，但目前流程是：

```
POST /session         → 取得 session_id
POST /session/{id}/message → 送 prompt
```

改成 SSE 主要通道後必須先訂閱 `GET /event`（否則漏掉開頭的事件），
但 `session_id` 要等第一個 `POST /session` 回來才有。

`GET /event` 是 opencode serve 的**全局串流**（非 per-session），
所以技術上可以先訂閱再 POST，用 sessionID 過濾（Q7 已確認可行）。
但文件沒有明確定義這個時序，實作時容易出錯。

**需要確認**：SSE 連線在 `POST /session` 之前建立，還是之後？fallback 的 timeout 值？

---

### I-3：`server.py` agent loop 缺少 `agent_thinking` handler

**位置**：`server.py:683`

Agent message loop 目前只處理 `update_model`、`message`、`leave`。
文件說 bridge 把 `agent_thinking` 送給 server，server 轉給 observers only，
但 server 端沒有對應的 handler，`agent_thinking` 會直接掉進 `else` 被丟棄。

**確認方向**：走 agent 的現有 WebSocket，不另開通道。

**修法**（詳細）：

1. 在 agent loop 的 `msg["type"]` 分支裡新增 `agent_thinking` case。
2. 收到後呼叫 `_broadcast(room, msg, observers_only=True)`——**只廣播給 observers，不能回送給其他 agents**，否則 agents 會收到彼此的 thinking 事件，製造無限迴圈風險。
3. 同時將事件 append 至 `room.thinking_log`；若 `thinking_log` 尚未初始化（舊版 room），需加 `hasattr` guard 或在 Room 初始化時確保欄位存在。
4. FIFO 上限（最近 20 turns）由 `thinking_log` append 後統一 trim，不在 broadcast 時處理。

**邊界情況**：
- `agent_thinking` 在 `turn_end` 之後抵達（網路延遲）：server 仍接收並寫入 log，但 client 端 guard 拒絕渲染，符合 Phase 1 驗收條件。
- `room` 不存在（agent 已離開）：直接 return，不寫 log，不 broadcast。

---

## 設計不一致（實作時有歧義）

### I-4：`layout.md` 底部備注是舊版殘留

**位置**：`docs/layout.md:129`

```
RoomHeader → ChatLog → CompletionList → ThinkingBar → SepBar → MessageInput
```

上面已定義 `StatusBar` 取代了 `ThinkingBar + SepBar`，這行直接矛盾。
**修法**：刪除或更新這行備注。

---

### I-5：`agent_thinking.turn` 欄位語義未定義

**位置**：`thinking-stream.md` 事件格式、Phase 2 `query_thinking_log`

事件格式有 `"turn": 5`，但未說明對應：
- `room.current_round`
- `len(room.history)`
- 其他計數器

FIFO 淘汰邏輯（保留最近 20 turns）和未來 `query_thinking_log { "turns": [3, 4, 5] }` 都依賴這個欄位。
Server 側和 client 側若理解不一致，log 查詢結果就會錯。

**建議**：對應 `room.current_round`（每次 owner 發話遞增），語義最清晰。

---

### I-6：多 agent 並發時 StatusBar 的「最新」語義未定義

**位置**：`thinking-stream.md Q5`、驗收條件「三個 agent 同時 thinking」

Q5 說「只顯示最新一個 block 的單行摘要」，但 broadcast 模式下多個 agent 同時 thinking，
StatusBar 只有一行。驗收條件有此手動測試，但未定義預期行為：

- 最後收到的 `agent_thinking` 覆蓋顯示？
- Round-robin 輪播？
- 顯示最先取得 turn 的 agent？

**建議**：Phase 1 採「最後收到的 `agent_thinking` 覆蓋顯示」，最簡單，文件補一行說明即可。

---

### I-7：`RoomHeader` spinner 的驅動來源未說明

**位置**：`docs/layout.md` RoomHeader 定義、`thinking-stream.md`

`layout.md` 說 RoomHeader 第 2 行顯示 `claude-opus ⠙`（spinner），每 0.1s 更新，
「只有 agent 在思考時才刷新」。但 thinking-stream.md 完全未提到 RoomHeader 要修改，
只談 StatusBar。

現有 `RoomHeader` widget 已存在，需確認：
- 它是否也要消費 `turn_start` / `turn_end` / `agent_thinking`？
- 和 StatusBar 的 spinner 是同一套 timer 還是各自獨立？

**建議**：RoomHeader spinner 由 `turn_start`（開始）/ `turn_end`（停止）驅動即可，
不需要 `agent_thinking`，兩者解耦。

---

## 小細節（不影響實作，確認後補文件即可）

### I-8：`turn_start` 和第一個 `agent_thinking` 之間 StatusBar 顯示什麼

**位置**：`docs/layout.md` StatusBar 定義

`turn_start → 切換到 thinking 狀態`，但第一個 `agent_thinking` 還沒到，
StatusBar 要顯示什麼？

`turn_start` 事件本身有 `name` 欄位（`server.py:356`），
可以直接顯示 `⠹ AgentName thinking...` 作為初始狀態。
這樣做是合理的，但文件沒有明說。

**建議**：補一條：「`turn_start` 時以事件的 `name` 欄位作為初始 thinking 摘要，
顯示 `⠹ {name} thinking...`，待 `agent_thinking` 到來後更新。」

---

## 待確認事項清單

| # | 問題 | 建議答案 | 狀態 |
|---|------|----------|------|
| I-1 | `_call_claude()` 怎麼存取 ws | 傳入參數 or `self.ws` | 待確認 |
| I-2 | OpenCode SSE 訂閱時序 + fallback timeout | 先訂閱 SSE，再 POST /session | 待確認 |
| I-3 | server.py 要不要在 agent loop 處理 `agent_thinking` | 是，走現有 agent WS；observers_only broadcast；FIFO trim 在 log append 後 | ✅ 已確認 |
| I-5 | `turn` 欄位對應哪個計數器 | `room.current_round` | 待確認 |
| I-6 | 多 agent 並發時 StatusBar 顯示哪個 | 最後收到的覆蓋 | 待確認 |
| I-7 | RoomHeader spinner 驅動來源 | `turn_start`/`turn_end` | 待確認 |

---

*建立日期：2026-04-06*
