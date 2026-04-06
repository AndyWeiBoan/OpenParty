* original:
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

* expected:
  ```
┌─────────────────────────────────────────────────────────────────┐
│ RoomHeader  (#room-header)                          [2 lines]   │
│ OpenParty — Room: my-room   Topic: hi                          │
│ Agents:                                                        │
│    ● claude-3-5-sonnet: standby                                 │
│    ⠙ claude-opus: thinking...                                   │
│    ⠹ claude-sonne: read(foo.py)...
                                                                  │
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
│ StatusBar  (#round status-bar)                            [1 line]    │
├─────────────────────────────────────────────────────────────────┤
│ MessageInput  (#input)                              [1 line]    │  ◄─ owner 才有
│  >                                                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Todo List

- [ ] **RoomHeader：改為每個 agent 獨立一行顯示狀態**
  - 修改 `RoomHeader.update_info()` (`openparty_tui.py`)
  - 格式：`● agent-name: standby` / `⠙ agent-name: thinking...` / `⠹ agent-name: read(foo.py)...`
  - 移除原本的單行 `" | "` 拼接格式，改為逐行 `\n` 串接
  - 狀態文字來源：thinking set（有 → 顯示 spinner + block summary；無 → 顯示 `●` + `standby`）

- [ ] **RoomHeader：將 per-agent thinking 狀態追蹤從 StatusBar 遷移過來**
  - 在 `RoomHeader` 內部新增 `_agent_status: dict[str, str]`（agent_name → status summary）
  - 新增 `start_thinking(agent_name, summary)` / `stop_thinking(agent_name)` / `update_block(agent_name, summary)` 方法（參考 `AgentSidebar` 現有實作模式）
  - spinner timer 邏輯同樣參考 `AgentSidebar._tick()` 複用

- [ ] **StatusBar：簡化為只顯示 round 狀態**
  - 移除 `set_thinking()` / `update_block()` 的 per-agent 顯示邏輯
  - 改為顯示 round 資訊（例如：`Round 3 / thinking` 或 `Round 3 / idle`）
  - 更新 id 為 `#round-status-bar`（修正 expected layout 中 `#round status-bar` 的空格筆誤）

- [ ] **呼叫端更新**
  - 調整 `openparty_tui.py` 中觸發 `StatusBar.set_thinking()` / `update_block()` 的呼叫點，改為同時更新 RoomHeader
  - 確認 `turn_start` / `turn_end` / `agent_thinking` 事件處理邏輯與新 RoomHeader 介面相容
