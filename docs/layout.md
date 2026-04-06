# OpenParty TUI — Layout Reference

## ASCII Layout Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ RoomHeader  (#room-header)                          [auto h]    │
│  OpenParty — Room: my-room   Topic: hi                          │
│  Agents:                                                         │
│     ⠹ claude-sonne: Read(foo.py)                                │
│     ● claude-opus: standby                                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│ ChatLog  (#chat)                               [flex / scroll]  │
│                                                                  │
│   2026-04-06 10:32:01  claude-sonne  (claude-sonnet-4-6)        │
│       這是一條訊息...                                            │
│                                                                  │
│   2026-04-06 10:32:45  [owner] Andy                             │
│       回覆內容...                                                │
│                                                                  │
│   ▲ PageUp / PageDn / End to scroll                             │
│                                                                  │
├╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┤
│ CompletionList               [auto-height, max 18, hidden=默認] │  ◄─ 浮在 input 上方
│  ▶  /broadcast  同時向所有 agent 發話，並行回答                  │
│     /add-agent  新增 AI agent 加入房間                           │
│     /kick       踢除房間成員                                     │
├─────────────────────────────────────────────────────────────────┤
│ StatusBar  (#round-status-bar)                      [1 line]    │
│  idle:    Round 3 / idle  — [owner] Andy                        │
│  thinking: Round 3 / thinking  — [owner] Andy                   │
├─────────────────────────────────────────────────────────────────┤
│ MessageInput  (#input)                    [auto / multi-line]   │  ◄─ owner 才有
│  >                                                              │
└─────────────────────────────────────────────────────────────────┘
```

## 區塊定義

### 1. RoomHeader (`#room-header`)
- **位置**：最頂端，固定
- **高度**：`height: auto`（依 agent 數量動態展開，約 2 + N 行）
- **背景**：`#0a1a0a`（深綠黑）/ 前景：`#ffaa00`（金黃）
- **邊框**：`border-bottom: solid #ffaa00`
- **Padding**：`padding: 0 1`
- **內容**：
  - 第 1 行：`OpenParty — Room: {room_id}   Topic: {topic}`
  - 第 2 行：`Agents:`
  - 後續每行一個 agent：
    - 思考中：`   {spinner} {name}: {status_summary}`
    - 待機中：`   ● {name}: standby`
    - 無 agent 時：`   (waiting...)`
- **status_summary 對應規則**（`update_block()` 依 blocks 列表反向搜尋第一個匹配 block）：
  - `"thinking"` block → `thinking...`
  - `"tool_use"` block → `{tool}({first_input_val[:18]})`
  - `"tool_result"` block → `{tool}:done({result[:18]})`
  - `"tool_error"` block → `{tool}:error({error[:18]})`
  - `"text"` block → `responding...`
  - 其他 block type → `thinking...`（fallback，總是 break）
- **更新頻率**：每 0.1s ticker（只有至少一個 agent 在思考時才啟動；無 agent 思考時 timer 停止）
- **Spinner**：`["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]`
- **Class**：`RoomHeader(Static)`

---

### 2. ChatLog (`#chat`)
- **位置**：RoomHeader 之下，佔剩餘主要空間
- **高度**：`flex`（撐滿中間）
- **類型**：`ChatLog(RichLog)`（可捲動，Rich 格式輸出，`wrap=True`，`markup=False`）
- **功能**：
  - `PageUp` / `PageDn`：半頁捲動（`size.height // 2`）
  - `End`：跳到底部（`animate=False`）
  - 收到新訊息時，若已在底部則自動滾動（否則保持當前位置）
- **內容格式**（每條訊息）：
  ```
  （空行）
    {YYYY-MM-DD HH:MM:SS.mmm}  {name}  ({model_label})
        {訊息內容（支援 **bold**、*italic*、@mention（黃色）、#tag（青色））}
  ```
- **私訊格式**：header 以 magenta 顯示，附加 `【私訊 → {recipients}】` 或 `【私訊】`
- **owner 訊息**：`OWNER_STYLE`（黑底白字）
- **agent 訊息**：round-robin 彩色（cyan/yellow/green/magenta/blue），按首次出現順序分配
- **Round 分隔線**：當 `msg.round` 增大時插入 `── Round {N} ──`（第一個 round 不插分隔線）
- **Class**：`ChatLog(RichLog)`

---

### 3. CompletionList
- **位置**：渲染順序在 ChatLog 之後（視覺上浮於 input 上方）
- **高度**：`height: auto`，上限 `max-height: 18`
- **預設**：`display: none`（隱藏，`styles.display = "none"`）
- **觸發**：輸入 `/` 開頭時（command 補全）或 `@` / `#` 開頭時（mention 補全）
- **互動**：`↑` / `↓` 移動選項，`Tab` 填入（不執行），`Enter` 確認執行，`Esc` 關閉
- **Command 補全項目**：
  - `/leave`、`/add-agent`、`/kick`、`/kick-all`、`/broadcast`
  - 只有 `/broadcast` 在選取後填入而不執行（`COMMANDS_WITH_ARGS`）
- **Class**：`CompletionList(Static)`

---

### 4. StatusBar (`#round-status-bar`)
- **位置**：CompletionList 之後、MessageInput 之前
- **高度**：`height: 1`（固定 1 行）
- **背景**：idle 時 `#ffaa00` 底 / `#0a1a0a` 字；thinking 時 `#0a1a0a` 底 / `#ffaa00` 字（顏色對調）
- **顯示格式**：` {round_str} / {state}  — {display_name}`
  - `round_str`：`Round {N}`（若 N > 0）或 `idle`
  - `state`：`thinking` 或 `idle`
- **狀態切換**：
  - `turn_start` → `set_thinking(True)`
  - `turn_end`（所有 agent 皆結束，`_thinking` set 清空）→ `set_thinking(False)`
  - `set_round(N)` → 更新 round 數字顯示
- **注意**：StatusBar **不顯示 spinner 也不顯示 agent 名稱**；spinner 與 agent 狀態由 RoomHeader 負責
- **Class**：`StatusBar(Static)`

---

### 5. MessageInput (`#input`)
- **位置**：最底端
- **類型**：`MessageInput(TextArea)`（多行輸入，取代原 `Input` widget）
- **高度**：`height: auto`（TextArea 自動展開）
- **顯示條件**：僅 owner 模式（`if self.owner`）
- **按鍵行為**：
  - `Enter`（無 Shift）→ 送出訊息（或確認補全選項）
  - `Shift+Enter` → 插入換行（多行輸入，由 TextArea 原生處理）
- **功能**：送出訊息、觸發補全（`/command`、`@mention`、`#mention`）
- **Class**：`MessageInput(TextArea)`

---

## 狀態摘要

| 區塊            | 高度       | 預設可見 | 僅 Owner | 更新來源                                                          |
|-----------------|------------|----------|----------|-------------------------------------------------------------------|
| RoomHeader      | auto (動態) | ✅       | ❌        | `turn_start` / `agent_thinking` / `turn_end` / 0.1s timer        |
| ChatLog         | flex        | ✅       | ❌        | WebSocket 訊息（`message`、`agent_joined`、`agent_left` 等）      |
| CompletionList  | auto≤18     | ❌       | ✅        | 輸入 `/`、`@`、`#`                                                |
| StatusBar       | 1           | ✅       | ❌        | `turn_start` / `turn_end`（idle↔thinking 切換）                   |
| MessageInput    | auto        | ✅       | ✅        | 使用者輸入（TextArea）                                             |

---

## compose() 順序

Textual 的 `compose()` 決定垂直排列（`openparty_tui.py` 第 716–722 行）：

```python
yield RoomHeader(id="room-header")
yield ChatLog(id="chat")
yield CompletionList()
yield StatusBar(self.owner, self.display_name, id="round-status-bar")
if self.owner:
    yield MessageInput(id="input")
```

`AgentSidebar` class 定義於 `openparty_tui.py`（第 260–330 行），但**未被 `compose()` 使用**，目前不在實際 TUI 佈局中。

---

## 備註

- `CompletionList` 預設 `display: none`，不佔可見空間，但仍在 DOM 中
- Observer 模式（`--owner` 未傳）不顯示 `MessageInput`（`CompletionList` 仍在 DOM 但不會觸發補全）
- `MessageInput` 已從原本的 `Input`（單行）改為 `TextArea`（多行，`Shift+Enter` 換行）
- `StatusBar` 的 id 為 `#round-status-bar`
- `turn_start` 事件中，只呼叫 `header.start_thinking()`，**不額外**呼叫 `header.update_info()`，避免雙重 render（已在 room-header-bugs.md Finding 6 修正）
- `agent_thinking` 事件有 guard：若該 agent 的 `turn_end` 已到達（在 `_turn_complete` set 中），遲到的 `agent_thinking` 會被丟棄，不更新 header
