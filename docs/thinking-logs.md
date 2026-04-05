# Thinking Stream — 實作紀錄

> Phase 1 實作過程中的決策、問題和觀察。依日期排列。

---

## 2026-04-06 Phase 1 實作

### 決策：I-1 — `_call_claude()` 如何存取 ws

**決定**：在 `AgentBridge.__init__` 加 `self.ws = None`，`run()` 進入 `async with websockets.connect` 後立即設 `self.ws = ws`，離開後清 `None`。

不用傳參數的原因：`_call_opencode_with_thinking` 和 `_opencode_sse_listener` 也需要 ws，用 `self.ws` 統一存取比逐層傳參乾淨。

---

### 決策：I-2 — OpenCode SSE 訂閱時序

**決定**：POST 和 SSE 並行啟動。

流程：
1. `create_session()` 拿到 `session_id`（若尚未建立）
2. `asyncio.create_task(oc._post_message(body))` — 非 blocking，立即回傳 Task
3. `asyncio.create_task(self._opencode_sse_listener(post_task))` — 同時開始訂閱 `GET /event`
4. `await post_task` — 等 POST 完成
5. finally: cancel sse_task

SSE 在 POST 送出後才訂閱，理論上可能漏掉極早的事件（race window 約 1 個 RTT）。Phase 1 可接受，thinking stream 只是 observability，不影響結果正確性。

**fallback timeout**：SSE 設 310 秒（比 POST 的 300 秒多 10 秒），確保 POST 超時後 SSE 也會結束。

---

### 決策：I-5 — `turn` 欄位語義

**決定**：Bridge 送出的 `agent_thinking` 事件**不含 `turn` 欄位**，由 Server 在接收後加上 `room.current_round`。

理由：Bridge 不知道 Server 的 `current_round`，強行同步沒有意義。Server 才是 turn 的 authoritative source。

---

### 決策：I-6 — 多 agent 並發時 StatusBar 顯示哪個

**決定**：最後收到的 `agent_thinking` 覆蓋顯示。

Phase 1 StatusBar 是單行，不支援多 agent 同時顯示。廣播模式下多個 agent 並發思考時，StatusBar 顯示最新收到的那個 agent 的狀態，前一個被覆蓋。行為對使用者是可接受的——StatusBar 是 hint，不是完整狀態面板。

---

### 決策：I-7 — RoomHeader spinner 驅動來源

**決定**：維持現狀，不改動。

`RoomHeader.update_info()` 由 `_tick_header()` (0.1s timer) 驅動，spinner 和 `self._thinking` set 連動。`turn_start` 加入 `self._thinking`，`turn_end` 移除。**不需要讀 `agent_thinking`**，Header 和 StatusBar 是獨立的 UI 層。

---

### 問題：OpenCode SSE 事件格式未驗證

**狀態**：⚠️ 未測試

目前 `_opencode_sse_listener` 同時處理兩種格式：

1. `type == "message.part.delta"` + `field == "reasoning"` → reasoning delta
2. `type == "reasoning-delta"` → reasoning delta（alternative）
3. `type == "tool-call"` → tool_use block
4. `type == "message.part.stop"` / `type == "reasoning-end"` → flush reasoning block

實際 opencode serve 的 SSE 格式需要對照真實輸出驗證。如果事件格式與假設不符，`_opencode_sse_listener` 可能完全沉默（不送任何 `agent_thinking`）但不會 crash——POST 的最終結果仍然正確。

**下一步**：啟動 opencode serve，訂閱 `GET /event`，記錄實際事件結構。

---

### 問題：`_render()` 命名衝突

**狀態**：✅ 已修

`AgentSidebar._render()` 和 `StatusBar._render()` 與 Textual 內部 `Widget._render()` 衝突（return type 不相容）。

修法：統一改名為 `_refresh_display()`。

---

### 問題：`Optional[OpenCodeClient]` 的 Pyright narrowing

**狀態**：✅ 已修

`self._opencode: Optional[OpenCodeClient]` 在每個 `if self.engine == "opencode":` block 都需要個別 `assert self._opencode is not None` 才能通過 Pyright。共加了 4 處：

- `run()` 開頭的 `ensure_opencode_server` 呼叫前
- 主要 call 前
- retry call 前
- `_call_opencode_with_thinking()` 頂部
- `_opencode_sse_listener()` 頂部

---

### 問題：`StatusBar` 在 broadcast 模式下的 idle 判斷

**狀態**：⚠️ 待觀察

`turn_end` handler 在 `self._thinking` 為空時才呼叫 `StatusBar.set_idle()`。廣播模式下多個 `turn_end` 依序到達，最後一個 `turn_end` 觸發 `set_idle()`。若有 `turn_end` 亂序（某 agent 的 `turn_end` 比其他 agent 的 `agent_thinking` 早），StatusBar 可能提早回 idle。Phase 1 可接受。

---

## 驗收狀態

### 自動化測試（尚未撰寫）

| 項目 | 狀態 |
|------|------|
| `agent_thinking` 事件格式 schema | ⬜ 尚未 |
| Claude path 單元測試 | ⬜ 尚未 |
| server.py 接收 + 廣播 | ⬜ 尚未 |
| `thinking_log` 儲存 | ⬜ 尚未 |
| FIFO 淘汰 | ⬜ 尚未 |
| OpenCode path normalizer 單元測試 | ⬜ 尚未 |
| sessionID 過濾 | ⬜ 尚未 |

### 手動測試（尚未執行）

| 項目 | 狀態 |
|------|------|
| TUI StatusBar 視覺呈現 | ⬜ 尚未 |
| idle/thinking 模式切換時序 | ⬜ 尚未 |
| 多 agent 並發顯示 | ⬜ 尚未 |
| 端到端整合 | ⬜ 尚未 |

---

*最後更新：2026-04-06*
