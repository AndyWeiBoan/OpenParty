# Agent Thinking Stream — 設計提案

> 把 Claude 的中間思考過程從「被丟棄」變成「按需取用的資產」。

---

## 背景：現有的 streaming 架構

### Claude Agent SDK path

```
Anthropic API
    ↓  SSE (token by token)
Claude CLI binary
    ↓  stream-json (逐行 JSON 事件)
claude_agent_sdk — async for message in query(...)
    ↓  三種事件：SystemMessage / AssistantMessage / ResultMessage
bridge.py         ← 目前只用 ResultMessage，其餘丟棄
    ↓  整包 reply
OpenParty server (WebSocket)
    ↓
TUI / 其他 Agent
```

`AssistantMessage` 包含：
- 中間思考文字（extended thinking blocks，`ThinkingConfigEnabled` 開啟時）
- tool call 前的推論（「我來讀一下這個檔案...」）
- 每次 tool call 之間的過渡發言

這些目前在 `_call_claude()` 的 `async for` 裡被完全忽略，只有 `ResultMessage.result` 被送進 room。

### OpenCode path

```
LLM Provider (Anthropic / OpenAI / etc.)
    ↓  Vercel AI SDK streamText()
opencode serve (Bun HTTP server)
    ↓  SessionProcessor 處理 fullStream async iterable
    ↓  兩個輸出管道：
    ├─ POST /session/{id}/message  → 完整 JSON response（bridge.py 目前使用）
    └─ GET /event                  → SSE 即時事件串流（目前未使用）
bridge.py         ← 目前只用 POST 拿最終結果
    ↓  整包 reply
OpenParty server (WebSocket)
    ↓
TUI / 其他 Agent
```

**OpenCode SSE 事件類型（`GET /event`）：**

| 事件類型 | 說明 | 對應 thinking stream |
|----------|------|---------------------|
| `reasoning-start` / `reasoning-delta` / `reasoning-end` | 模型推理過程（extended thinking） | → `agent_thinking` block type: `thinking` |
| `tool-input-start` / `tool-call` / `tool-result` / `tool-error` | 工具呼叫與結果 | → `agent_thinking` block type: `tool_use` |
| `text-start` / `text-delta` / `text-end` | 文字生成增量 | → `agent_thinking` block type: `text` |
| `start-step` / `finish-step` | 步驟追蹤（含 usage metrics） | 可用於計算 token/cost |

每個 delta 事件透過 `message.part.delta` 發布：
```json
{
  "type": "message.part.delta",
  "properties": {
    "sessionID": "...",
    "messageID": "...",
    "partID": "...",
    "field": "text",
    "delta": "..."
  }
}
```

**關鍵發現：OpenCode 的 streaming 粒度比 Claude Agent SDK 更細（token-level delta 直接暴露），且 reasoning parts 完整可見，不是黑箱。**

---

## 問題

1. **TUI 沒有 agent 狀態**：agent 在思考時，TUI 只能顯示「等待中」，無法知道它在做什麼。
2. **思考過程消失**：每次 turn 結束，中間推理過程永久消失，無法事後追溯。
3. **Scene Leader 決策缺乏依據**：Leader 看不到其他 agent 的推理過程，只能根據最終結論協調。

---

## 提案：兩條平行通道

```
AssistantMessage  →  [thinking channel]  →  TUI 狀態 + 持久化 log
ResultMessage     →  [room channel]      →  room history、其他 agent 的 context
```

turn 協議完全不動。`ResultMessage` 仍然是判斷 turn 結束的唯一依據。

---

## 通道一：TUI 即時狀態

**目的**：讓 TUI 在 agent 思考時顯示即時狀態，而非空白等待。

### 資料流

```
bridge.py 收到 AssistantMessage
    → WebSocket 送出 agent_thinking 事件（observers_only）
server.py 轉發給所有 observer
    → TUI 顯示 agent 旁邊的狀態列
收到 turn_end
    → TUI 清除狀態列
```

### 事件格式

```json
{
  "type": "agent_thinking",
  "agent_id": "a1b2c3",
  "name": "Claude01",
  "turn": 5,
  "blocks": [
    { "type": "thinking", "text": "..." },
    { "type": "tool_use", "tool": "Read", "input": { "file_path": "..." } },
    { "type": "text", "text": "..." }
  ]
}
```

### TUI 顯示方式

| block type | 顯示 |
|-----------|------|
| `thinking` | `⠋ thinking...`（可選擇是否展開） |
| `tool_use` | `⚙ Read(foo.py)` |
| `text` | 不顯示（只是過渡文字，最終版本在 ResultMessage） |

thinking block 預設折疊，使用者可展開查看。

---

## 通道二：思考 log 持久化

**目的**：保存每個 agent 每個 turn 的完整推理過程，供 Scene Leader 或 Owner 事後查詢。

### 儲存結構

server 的 `Room` 物件新增：

```python
@dataclass
class Room:
    ...
    thinking_log: dict[str, list[dict]] = field(default_factory=dict)
    # thinking_log[agent_id] = [
    #   { "turn": 1, "timestamp": "...", "blocks": [...] },
    #   { "turn": 3, "timestamp": "...", "blocks": [...] },
    # ]
```

Phase 2 持久化到磁碟：

```
~/.local/share/openparty/sessions/{room_id}/
  thinking/{agent_id}/{turn}.json
```

### Scene Leader 查詢介面

Leader 在 directive 裡請求 thinking log：

```json
{
  "content": "（正常發言）",
  "directive": {
    "query_thinking_log": {
      "agent_id": "Claude01",
      "turns": [3, 4, 5]
    }
  }
}
```

Server 解析後，把對應的 thinking log 附在下一個 `your_turn` payload 的 `thinking_context` 欄位裡回給 Leader：

```json
{
  "type": "your_turn",
  "history": [...],
  "thinking_context": {
    "Claude01": [
      { "turn": 3, "blocks": [...] },
      { "turn": 4, "blocks": [...] }
    ]
  }
}
```

Leader 可以用這些資料做出更有依據的協調決策，例如：
- 發現兩個 agent 的推理前提矛盾
- 知道某個 agent 其實考慮過某方案但放棄了
- 追溯為什麼某個 tool call 失敗

---

## 實作邊界

### 不做的事

- thinking block 不進 room history（其他 agent 看不到）
- thinking block 不影響 turn 協議
- TUI 顯示的思考過程不影響 context window

### Owner 也可查詢

Owner 在 TUI 輸入 `/thinking Claude01 3` 可查看特定 turn 的思考過程，不需要透過 Scene Leader。

---

## 對應 scene.md 的位置

| scene.md 章節 | 本提案的關係 |
|--------------|-------------|
| Scene Leader — Leader 的能力 | thinking log 查詢是 directive 的擴充 |
| 長時間執行場景 — 狀態持久化 | thinking log 是持久化的一部分 |
| Phase 2 | thinking log 磁碟持久化與 session 恢復一起做 |

---

## 實作路徑

### Phase 1（最小可用）

**Claude engine path：**
- [ ] `bridge.py`：收到 `AssistantMessage` 送出 `agent_thinking` 事件

**OpenCode engine path：**
- [ ] `bridge.py`：OpenCode path 新增 SSE 訂閱（`GET /event`），攔截 `message.part.delta` 事件
- [ ] 將 `reasoning-delta`、`tool-call`、`text-delta` 轉換為統一的 `agent_thinking` 事件格式

**共用：**
- [ ] `server.py`：收到 `agent_thinking` 轉給 observers only，存入 `room.thinking_log`
- [ ] `openparty_tui.py`：收到 `agent_thinking` 顯示狀態列，`turn_end` 時清除

### Phase 2（配合 scene.md Phase 2）

- [ ] thinking log 寫入磁碟
- [ ] Scene Leader directive `query_thinking_log` 支援
- [ ] Owner `/thinking` 指令
- [ ] session 重連後 thinking log 可恢復

---

## 待解決問題清單

> 綜合 2026/04/05 討論室會議結果。Q = 問題，A = 結論。

### 已解決

#### **Q1: SDK streaming 粒度是否足夠支撐 thinking stream？**
- A: 足夠。Claude Agent SDK 為 block-level `AssistantMessage`（thinking blocks, tool_use, 過渡文字），OpenCode 為 token-level SSE delta（`reasoning-delta`, `tool-call`, `text-delta`）。兩者皆可攔截，Phase 1 可同時覆蓋雙引擎。

#### **Q2: owner 同時是 observer，`agent_thinking` 會送兩次嗎？**
- A: 不會。Server 中 owner 連線 role 即為 `"observer"`（`is_owner=True`），與普通 observer 存同一個 `room.observers` dict。`_broadcast(observers_only=True)` 遍歷 `room.observers.values()`，每個 WebSocket 連線只有一個 entry，只收一次。

#### **Q3: thinking_log memory 上限？**
- A: **每 agent 保留最近 20 turns，FIFO 淘汰。** 與現有 `SLIDING_WINDOW_SIZE = 20` 一致，概念統一。Phase 1 記憶體壓力可控（20 turns × 10 agents 最壞約 1-10MB）。Phase 2 持久化到磁碟後，再根據實際數據決定是否改用 byte cap 或調整 turn 數量。

#### **Q4: `agent_thinking` 與 `turn_end` 的時序關係？**
- A: **用現有 `turn_end` 清除 TUI 狀態列，不另定義新事件。加 guard 防亂序。** 正常情況下時序天然正確：thinking events 在 agent 處理過程中發出，`turn_end` 在 server 收到最終 reply 後才 emit（Claude path: `ResultMessage` → `message` → `turn_end`；OpenCode path: `POST` 返回 → `message` → `turn_end`）。但為防上游 bug 導致 `turn_end` 後仍收到 `agent_thinking`，TUI 端加 guard：收到 `turn_end` 後標記該 agent 為 `turn_complete`，之後同一 agent 的 `agent_thinking` 直接丟棄不渲染；下一次 `turn_start` 重置 flag。thinking_log 照存不受影響（資料完整性與 UI 顯示分離）。

#### **Q5: ThinkingBar 擴充範圍？**
- A: **ThinkingBar 與 SepBar 合併為 StatusBar，Phase 1 只顯示最新一個 block 的單行摘要。** idle 時顯示原 SepBar 內容（owner/observer 資訊），有 agent thinking 時切換為 thinking 模式：`thinking` block → spinner + name，`tool_use` block → `⚙ AgentName → Tool(arg)`，`text` block → 不切換。不做多行展開、不做 block 歷史列表、不做 sidebar（sidebar 為未來方向，不在 Phase 1 scope）。詳見 `docs/layout.md` StatusBar 定義。

#### **Q6: OpenCode & Claude SDK 的連線生命週期？**
  - **OpenCode path:**
    - A: **單一 SSE 連線 + timeout fallback。** bridge.py 訂閱 `GET /event` 作為主要通道，同時接收 thinking 事件和最終結果。不需要兩條並發連線——`POST /session/{id}/message` 僅作為 fallback：當 SSE stream 超時或中斷時，補送一次 REST 請求拿最終結果。Fallback 觸發條件（timeout 時長、哪些 error code 觸發）於實作時定義。

  - **Claude Agent SDK path：**
    - A: **目前為非 streaming（blocking call），可直接升級為單一 stream，無雙連線問題。** `messages.stream()` 天然單一連線，thinking blocks 和 content blocks 都從同一條 stream 流出。Fallback 方案與 OpenCode 一致：stream 超時或中斷時，補一次 non-streaming API call 拿最終 response。架構比 OpenCode 側更簡單，不存在多路同步問題。

#### **Q7: SSE event filtering（多 agent 串台問題）？**
- A: **用 sessionID（UUID）過濾，結案。** `GET /event` 串流整個 instance 所有事件，bridge.py 訂閱後對每個收到的事件檢查 `properties.sessionID` 是否等於自己的 session ID，不匹配直接丟棄。sessionID 為 UUID v4（2^122 種組合），碰撞機率可忽略不計，不需要額外 namespace 隔離。bridge.py 與 opencode serve 是否 1:1 部署的決策可延後，不影響此過濾方案。

#### **Q8: 雙引擎事件格式如何統一？**
- A: **adapter 在 bridge.py 內部，每個 engine path 各一個 normalizer function，輸出統一的 `agent_thinking` 事件格式。Server.py 對引擎來源完全無感知。**
  - **Claude path normalizer**：`AssistantMessage` 已為 block-level，直接 1:1 映射——`thinking block → {"type": "thinking", "text": ...}`，`tool_use block → {"type": "tool_use", "tool": ..., "input": ...}`，`text block → {"type": "text", "text": ...}`。最簡單。
  - **OpenCode path normalizer**：需做 **delta 聚合**——`reasoning-delta` buffer 到 `reasoning-end` 組成完整 thinking block，`text-delta` buffer 到 `text-end` 組成完整 text block，`tool-call` 為一次性事件直接映射。
  - **Phase 1 決策：bridge 側聚合成 block-level 再送出，不透傳 token-level delta。** 理由：StatusBar 只需單行摘要，不需要逐 token 渲染，聚合後大幅減少 WebSocket 訊息量。Phase 2 若需要即時 token streaming（如 TUI 展開模式），可改為透傳 delta + client 側聚合。

### 可延後到 Phase 2

#### **Q9: thinking block 要不要送給 TUI？**
- A: 待定。thinking block 可能很長且敏感（模型原始推理），預設折疊是否足夠，還是應該預設不傳？

#### **Q10: thinking log 的磁碟保留期限？**
- A: 待定。無限保留可能佔用大量空間，是否需要 TTL 或手動清除？

#### **Q11: Leader 查詢的頻寬問題？**
- A: 待定。thinking log 很大時附在 `your_turn` payload 裡可能讓 context window 爆滿，是否需要摘要？

#### **Q12: 隱私邊界？**
- A: 待定。Owner 和 Scene Leader 都能查 thinking log，是否需要 Agent 層級的「opt-out thinking visibility」？

## 已確認事項

> 以下由 2026/04/05 討論室會議確認。

### SDK streaming 粒度（原開放問題 #1 — 已解決）

| Engine | Streaming 層級 | 可攔截的事件 | 結論 |
|--------|---------------|-------------|------|
| **Claude Agent SDK** | Token-level `async for` yield `AssistantMessage` | thinking blocks, tool_use, 過渡文字 | ✅ 可直接攔截轉發 |
| **OpenCode serve** | Token-level SSE via `GET /event` | `reasoning-delta`, `tool-call`, `text-delta`, `start-step`/`finish-step` | ✅ 粒度更細，reasoning 完整暴露 |

**結論：兩條引擎路線都支援 thinking stream，Phase 1 可同時覆蓋。** OpenCode 不是黑箱——其 SSE endpoint 暴露的事件粒度甚至比 Claude Agent SDK 更細（直接 token-level delta），且 reasoning parts 完整可見。

### 實作優先級（已確認）

**Thinking stream 排在 scene.md 之前做。** 理由：
1. 依賴面窄（只動 bridge.py、server.py、TUI），不需要 scene.md 的 Leader/directive 架構
2. Scene Leader 若能看到 thinking log，協調決策品質更好——先做 thinking stream 讓 scene.md 實作更有依據
3. Thinking stream 是純 observability 層，技術上完全獨立於 scene transition/context switching
4. 先裝儀表板再上路，debugging 多 agent 行為時有 thinking visibility 開發體驗更好

---

## Phase 1 驗收條件

> 本節記錄 Phase 1 實作完成後的驗收標準，由 2026/04/06 討論室會議確認。
>
> **前置條件**：實作開始前先定義 `agent_thinking` 事件的 dataclass 或 JSON schema，作為所有自動化測試的比對基準——沒有明確 schema，「格式正確」這個條件本身就是模糊的。

### Agent 可測試（自動化）

| 項目 | 說明 |
|------|------|
| `agent_thinking` 事件格式 | 必填欄位齊全（type, agent_id, name, turn, blocks），blocks 內每個 block 有 type 欄位；以預先定義的 schema/dataclass 作為比對基準 |
| Claude path 單元測試 | mock `AssistantMessage`（含 ThinkingBlock + ToolUseBlock）→ 確認輸出的 `agent_thinking` 事件格式正確、blocks 類型映射正確 |
| server.py 接收 + 廣播 | 收到 `agent_thinking` 正確廣播給 observers（含 owner），不發給 agents |
| thinking_log 儲存 | 每個 turn 的 blocks 正確寫入 `room.thinking_log[agent_id]` |
| FIFO 淘汰 | 超過 20 turns 後最早的記錄被移除（塞入 25 筆，只保留最近 20 筆） |
| delta 聚合邏輯 | OpenCode path：多個 `reasoning-delta` 累積到 `reasoning-end` 產出完整 block；delta 順序不同時仍正確拼接 |
| OpenCode path normalizer 單元測試 | mock SSE event stream（含 `reasoning-start` / `reasoning-delta` / `reasoning-end` / `tool-call` / `text-delta`）→ 驗證 normalizer 輸出的 `agent_thinking` 事件格式正確、block 類型映射正確（與 Claude path 測試對等） |
| sessionID 過濾 | OpenCode path：不匹配的事件被丟棄，匹配的事件正確處理 |
| timeout fallback 完整驗收 | OpenCode path：SSE stream 超時後，fallback 補送 POST 請求；驗證 POST 回傳結果正確寫入 `thinking_log` 且該 turn 完整結束（觸發 fallback 但結果錯誤不算通過） |
| turn_end guard + flag 重置 | 收到 `turn_end` 後同一 agent 的後續 `agent_thinking` 不渲染（server 端仍存 log）；下一次 `turn_start` 正確重置 flag，使下一個 turn 的 `agent_thinking` 可正常渲染（驗證 reset 邏輯，否則第一個 turn 的 guard 會影響後續所有 turn） |

### User 需測試（手動）

| 項目 | 說明 |
|------|------|
| TUI StatusBar 視覺呈現 | Agent 思考時正確顯示 spinner + agent name + block 摘要（`⚙ AgentName → Read(foo.py)` 格式是否易讀）|
| idle/thinking 模式切換時序 | idle 顯示 owner/observer 資訊 ↔ thinking 顯示狀態，正確切換；agent 開始思考到 StatusBar 更新的延遲是否可接受；turn_end 後是否乾淨回到 idle |
| 多 agent 並發顯示 | 三個 agent 同時 thinking，TUI 顯示是否正確且不閃爍、不混亂 |
| OpenCode SSE 斷線 fallback | 殺掉 opencode serve 進程，觀察 bridge 是否正確 fallback 到 POST |
| 端到端整合 | 實際啟動 room + agent + TUI observer，觀察完整 thinking stream 從 API 到 StatusBar 的流動 |
| 實際可讀性 | Thinking log 內容是否對人類有意義（格式、斷行、截斷） |

> **注意**：Owner `/thinking` 指令屬於 Phase 2 驗收範圍，不在此列。

---

*提案日期：2026/04/05*
