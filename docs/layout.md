# OpenParty TUI — Layout Reference

## ASCII Layout Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ RoomHeader  (#room-header)                          [2 lines]   │
│  OpenParty — Room: my-room   Topic: hi                          │
│  Agents: claude-3-5-sonnet ● | claude-opus ⠙                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│                                                                  │
│ ChatLog  (#chat)                               [flex / scroll]  │
│                                                                  │
│   claude-sonne  (claude-sonnet-4-5)                             │
│   10:32:01                                                       │
│   這是一條訊息...                                               │
│                                                                  │
│   [owner] Andy                                                   │
│   10:32:45                                                       │
│   回覆內容...                                                    │
│                                                                  │
│   ▲ PageUp / PageDn / End to scroll                             │
│                                                                  │
├╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┤
│ CompletionList               [auto-height, max 18, hidden=默認] │  ◄─ 浮在 input 上方
│  > /broadcast  同時向所有 agent 發話，並行回答                  │
│    /add-agent  新增 AI agent 加入房間                           │
│    /kick       踢除房間成員                                     │
├─────────────────────────────────────────────────────────────────┤
│ StatusBar  (#status-bar)                            [1 line]    │
│  idle:    [owner] Andy  Type message + Enter. /leave to exit.  │
│  thinking: ⠹ claude-sonne → Read(foo.py)                       │
├─────────────────────────────────────────────────────────────────┤
│ MessageInput  (#input)                              [1 line]    │  ◄─ owner 才有
│  >                                                              │
└─────────────────────────────────────────────────────────────────┘
```

## 區塊定義

### 1. RoomHeader (`#room-header`)
- **位置**：最頂端，固定
- **高度**：`height: auto`（約 2 行）
- **背景**：`#0a1a0a`（深綠黑）/ 前景：`#ffaa00`（金黃）
- **內容**：
  - 第 1 行：`OpenParty — Room: {room_id}   Topic: {topic}`
  - 第 2 行：`Agents: {engine/model} {spinner|●} | ...`
- **更新頻率**：每 0.1s（只有 agent 在思考時才刷新）
- **Class**：`RoomHeader(Static)`

---

### 2. ChatLog (`#chat`)
- **位置**：Header 之下，佔剩餘主要空間
- **高度**：`flex`（撐滿中間）
- **類型**：`RichLog`（可捲動，Rich 格式輸出）
- **功能**：
  - `PageUp` / `PageDn`：半頁捲動
  - `End`：跳到底部
  - 收到新訊息自動滾動（若已在底部）
- **內容格式**（每條訊息）：
  ```
  （空行）
    {name}  ({model_label})
    {HH:MM:SS}
    {訊息內容（支援 **bold**、*italic*、@mention、#tag）}
  ```
- **Class**：`ChatLog(RichLog)`

---

### 3. CompletionList
- **位置**：渲染順序在 ChatLog 之後（視覺上浮於 input 上方）
- **高度**：`height: auto`，上限 `max-height: 18`
- **預設**：`display: none`（隱藏）
- **觸發**：輸入 `/` 開頭時（command 補全）或 `@` 開頭時（mention 補全）
- **互動**：`↑` / `↓` 移動選項，`Tab` / `Enter` 確認，`Esc` 關閉
- **Class**：`CompletionList(Static)`

---

### 4. StatusBar (`#status-bar`)（原 ThinkingBar + SepBar 合併）
- **位置**：CompletionList 之後、MessageInput 之前
- **高度**：`height: 1`（固定 1 行）
- **背景**：idle 時 `white`/`black`；thinking 時 `#0a1a0a`/`#ffaa00`
- **兩種狀態**：
  - **idle**（無 agent 在思考）：顯示原 SepBar 內容
    - owner 模式：`{display_name}  Type message + Enter to send. /leave to exit.`
    - observer 模式：`Observer: {name}  Read-only mode.`
  - **thinking**（有 agent 在思考）：顯示最新 thinking block 摘要
    - `thinking` block → `⠹ AgentName thinking...`
    - `tool_use` block → `⚙ AgentName → Read(foo.py)`
    - `text` block → 不切換，維持前一個狀態
- **狀態切換**：
  - `turn_start` → 切換到 thinking 狀態
  - `agent_thinking` → 更新 thinking 摘要
  - `turn_end` → 切回 idle（加 guard：turn_end 後忽略遲到的 agent_thinking）
- **動畫**：thinking 狀態時每 0.1s 更新 spinner frame（⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏）
- **Class**：`StatusBar(Static)`（取代原 `ThinkingBar` + `SepBar`）

---

### 5. MessageInput (`#input`)
- **位置**：最底端
- **高度**：`height: 1`（Textual `Input` widget 預設）
- **顯示條件**：僅 owner 模式（`if self.owner`）
- **Placeholder**：`> `
- **功能**：送出訊息、觸發補全、@mention、/command 解析
- **Class**：`MessageInput(Input)`

---

## 狀態摘要

| 區塊            | 高度    | 預設可見 | 僅 Owner | 更新來源                               |
|-----------------|---------|----------|----------|----------------------------------------|
| RoomHeader      | auto~2  | ✅        | ❌        | `agent_thinking` / timer              |
| ChatLog         | flex    | ✅        | ❌        | WebSocket 訊息                         |
| CompletionList  | auto≤18 | ❌        | ✅        | 輸入 `/` 或 `@`                        |
| StatusBar       | 1       | ✅        | ❌        | idle↔thinking 切換（`turn_start`/`turn_end`/`agent_thinking`） |
| MessageInput    | 1       | ✅        | ✅        | 使用者輸入                             |

---

## 備註

- Textual 的 `compose()` 順序決定垂直排列，從上到下：
  `RoomHeader → ChatLog → CompletionList → ThinkingBar → SepBar → MessageInput`
- `CompletionList` 和 `ThinkingBar` 預設隱藏，不佔可見空間，但仍在 DOM 中
- Observer 模式（`--owner` 未傳）不顯示 `MessageInput` 和 `CompletionList`
