# Thinking Stream Phase 1 — 驗收狀態與待辦清單

> 根據 2026/04/06 討論室分析結果整理。
> 對應文件：`docs/thinking-stream.md` Phase 1 驗收條件。

---

## 現況摘要

**功能程式碼：✅ 完成**
**自動化測試：✅ 完成（8/8 通過）**
**整體完成度：✅ 100%，Phase 1 驗收條件全部達成**

---

## 已完成項目

| 項目 | 位置 |
|------|------|
| Claude path：`AssistantMessage` → `agent_thinking` 事件，三種 block 類型映射 | `bridge.py:625-645` |
| OpenCode path：`GET /event` SSE 訂閱 | `bridge.py:498-588` `_opencode_sse_listener()` |
| OpenCode path：sessionID 過濾 | `bridge.py:530-531` |
| OpenCode path：reasoning delta 聚合（buffer → end flush） | `bridge.py:538-553` |
| OpenCode path：tool-call / text-delta 轉換 | `bridge.py:557-583` |
| server.py：`agent_thinking` 轉 observers only | `server.py:687-700` |
| server.py：存入 `room.thinking_log`，FIFO 20 turns 淘汰 | `server.py:691-698` |
| server.py：`thinking_log` 資料結構定義 | `server.py:115` |
| server.py：`turn` 欄位由 server 注入 | `server.py:689` |
| TUI StatusBar：idle/thinking 模式切換 | `openparty_tui.py:342-396` |
| TUI：`agent_thinking` 顯示 + `turn_end` 清除 | `openparty_tui.py:844-850` |
| TUI：`turn_end` guard（防亂序） | `openparty_tui.py:847-848` |
| TUI：`turn_start` reset flag | `openparty_tui.py:829` |

---

## 待辦清單

### 前置條件（最優先，其他所有測試依賴它）

- [x] **定義 `AgentThinkingEvent` dataclass**
  - 位置建議：`bridge.py` 頂部或獨立 `shared/types.py`
  - 必填欄位：`type`, `agent_id`, `name`, `turn`, `blocks`
  - Block subtype dataclass：`ThinkingBlock`、`ToolUseBlock`、`TextBlock`（各含對應欄位）
  - 作為所有自動化測試的格式比對基準

### 實作修正（1 項）

- [x] **`bridge.py` OpenCode path：修正 timeout fallback 架構**
  - **現況**：`_call_opencode_with_thinking` 中 POST task 與 SSE task 並行，POST 完成後直接 cancel SSE
  - **問題**：驗收條件要求「SSE stream 超時後 fallback 補送 POST 請求」，現況為兩者同時跑，不是 SSE 主通道 + POST fallback
  - **修正方向**：改為 SSE 為主要通道，SSE 逾時（或連線中斷）才觸發 POST fallback；定義明確的 timeout 時長與觸發條件

### 自動化測試（8 項，建議統一放 `tests/test_thinking_stream.py`）

- [x] **Claude path normalizer 單元測試**
  - mock `AssistantMessage`（含 `ThinkingBlock` + `ToolUseBlock`）
  - 驗證輸出的 `agent_thinking` 事件符合 `AgentThinkingEvent` schema
  - 驗證 block 類型映射正確（thinking / tool_use / text）

- [x] **OpenCode path normalizer 單元測試**
  - mock SSE event stream（`reasoning-start` / `reasoning-delta` / `reasoning-end` / `tool-call` / `text-delta`）
  - 驗證 delta 聚合後輸出的 `agent_thinking` 事件格式正確
  - 驗證 delta 順序不同時仍能正確拼接

- [x] **sessionID 過濾測試**
  - 送入不匹配 sessionID 的事件 → 確認被丟棄
  - 送入匹配 sessionID 的事件 → 確認正確處理

- [x] **server 廣播測試**
  - 收到 `agent_thinking` → 確認只廣播給 observers（含 owner），不發給 agents

- [x] **thinking_log 寫入測試**
  - 觸發 `agent_thinking` → 確認 blocks 正確寫入 `room.thinking_log[agent_id]`
  - 驗證 timestamp 和 turn 欄位存在

- [x] **FIFO 淘汰測試**
  - 塞入 25 筆 thinking log → 確認只保留最近 20 筆
  - 確認最早的 5 筆被移除

- [x] **timeout fallback 完整驗收**（依賴上方實作修正完成後）
  - 模擬 SSE stream 超時
  - 確認 fallback 補送 POST 請求
  - 確認 POST 回傳結果正確寫入 `thinking_log`，該 turn 完整結束

- [x] **turn_end guard + turn_start reset 測試**
  - 收到 `turn_end` 後同一 agent 的後續 `agent_thinking` 不渲染（server 端仍存 log）
  - 下一次 `turn_start` 正確重置 flag，使下一個 turn 的 `agent_thinking` 可正常渲染

---

## 注意事項

- **`turn` 欄位注入點**：bridge.py 送出的 `agent_thinking` 不含 `turn`，由 `server.py:689` 注入 `room.current_round`。設計可行，但測試時需注意 bridge 側輸出與 server 側廣播的事件格式不同。
- **timeout fallback 修正前**：timeout fallback 測試無法撰寫（測試標的不存在），建議先完成實作修正再補測試。
- **手動驗收項目不在此列**：TUI 視覺呈現、多 agent 並發顯示、端到端整合等手動測試項目詳見 `docs/thinking-stream.md` Phase 1 驗收條件。

---

*整理日期：2026/04/06*
