# M1 PoC Design — 產品方向決策

> 本文件涵蓋 M1 的三個研究課題。每個課題完成後往下累積。

---

## 課題 1：OpenCode / Claude Code 的 plugin 機制是什麼？能不能接？

### 課題

**anomalyco/opencode（132k stars）和 Claude Code 各自提供哪些整合機制？OpenParty 接進去的最佳路線是什麼？**

### 背景

這個課題直接決定 OpenParty 的產品形態。要研究的是兩個主要工具：
- **OpenCode**（anomalyco/opencode）：開源 AI coding agent，TypeScript，132k stars，活躍開發中，v1.3.3
- **Claude Code**（code.claude.com）：Anthropic 官方 CLI

> ⚠️ 初版研究搞錯了目標 —— 找到的是另一個已 archived 的同名 Go 專案（opencode-ai/opencode），以及 Charmbracelet 的 Crush。本版是重做後的正確研究。

錯誤的判斷會導致：
- 做成 plugin 但技術上根本接不上 → 浪費 2-3 週
- 做成獨立工具但其實有 plugin 路可走 → 增加使用者的安裝摩擦

### 調研結果

---

#### OpenCode（anomalyco/opencode）的整合機制

OpenCode 有 **client/server 架構**，這是它最重要的特性：
- `opencode` 啟動時同時啟動 TUI 和 HTTP server
- `opencode serve` 啟動無頭 HTTP server（`--port 4096`）
- Server 暴露完整的 OpenAPI 3.1 spec（`/doc`）

**整合機制一覽：**

**1. HTTP Server + JS/TS SDK（`@opencode-ai/sdk`）**
```typescript
import { createOpencode, createOpencodeClient } from "@opencode-ai/sdk"

// 啟動一個新 server + client
const { client } = await createOpencode()

// 或連接到已在跑的 opencode instance
const client = createOpencodeClient({ baseUrl: "http://localhost:4096" })

// 建立 session、送 prompt、拿回應
const session = await client.session.create({ body: { title: "OpenParty" } })
const result = await client.session.prompt({
  path: { id: session.data.id },
  body: { parts: [{ type: "text", text: "Join the OpenParty Room!" }] }
})
// 監聽 SSE 事件流
const events = await client.event.subscribe()
for await (const event of events.stream) { ... }
```

這意味著：**Python 可以直接 HTTP call OpenCode Server**（REST API，不需要 TS SDK）。

**2. Plugin 系統（JS/TS，`.opencode/plugins/`）**

Plugin 是 JS/TS 模組，放在 `.opencode/plugins/` 或 `~/.config/opencode/plugins/`，自動在啟動時載入。

```typescript
// .opencode/plugins/openparty.ts
import type { Plugin } from "@opencode-ai/plugin"

export const OpenPartyPlugin: Plugin = async ({ client, $ }) => {
  return {
    // session 開始時自動加入 Room
    "session.created": async ({ event }) => {
      await $`openparty join --room my-room --agent opencode`
    },
    // AI 每次回應後，把內容廣播到 Room
    "message.updated": async ({ event }) => {
      if (event.properties.role === "assistant") {
        // 呼叫外部 API 把訊息送進 OpenParty Room
        await fetch("http://localhost:8765/broadcast", { ... })
      }
    },
    // 也可以加入 custom tool，讓 AI 主動 join room
    tool: {
      join_openparty_room: tool({
        description: "Join an OpenParty Room to discuss with other AI agents",
        args: { room_id: tool.schema.string(), topic: tool.schema.string() },
        async execute(args) { ... }
      })
    }
  }
}
```

關鍵事件：`session.created`、`message.updated`、`tool.execute.before/after`、`session.idle`

**3. MCP Server（`opencode.jsonc` 設定）**

完全支援 MCP，設定方式和 Claude Code 一樣：
```jsonc
// opencode.jsonc
{
  "mcp": {
    "openparty": {
      "type": "local",
      "command": ["python", "/path/to/openparty_mcp.py"],
      "enabled": true
    }
  }
}
```
支援 local（stdio）和 remote（HTTP）兩種。

**4. Agent Skills（SKILL.md）**

OpenCode 支援 Agent Skills，路徑：
- `.opencode/skills/<name>/SKILL.md`
- `.claude/skills/<name>/SKILL.md`（與 Claude Code **共用**！）
- `.agents/skills/<name>/SKILL.md`

這意味著：**做一個 SKILL.md，同時在 OpenCode 和 Claude Code 都能用**。

**5. ACP（Agent Client Protocol）**

`opencode acp` 讓 Zed、JetBrains、Neovim 等編輯器把 OpenCode 當作 AI agent 接進去，跟 OpenParty 整合不相關。

---

#### Claude Code 的整合機制

**Plugin 系統（`.claude-plugin/`）**：
```
my-plugin/
├── .claude-plugin/plugin.json
├── skills/SKILL.md
├── hooks/hooks.json         ← PreToolUse、PostToolUse、Stop 等
└── .mcp.json
```

**MCP Server**：設定在 `.claude.json`：
```json
{
  "mcpServers": {
    "openparty": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/openparty_mcp.py"]
    }
  }
}
```

**Agent Skills（`.claude/skills/<name>/SKILL.md`）**：與 OpenCode 共用同一路徑。

---

#### 整合路線比較

| 路線 | OpenCode 支援 | Claude Code 支援 | 開發成本 | 整合深度 |
|------|:-----------:|:--------------:|:------:|:------:|
| **MCP Server**（Python） | ✅ | ✅ | 低（已有原型） | 中 |
| **Agent Skills（SKILL.md）** | ✅ | ✅ | 極低 | 中 |
| **OpenCode Plugin（JS/TS）** | ✅ | ❌ | 中 | 高 |
| **Claude Code Plugin** | ❌ | ✅ | 中 | 高 |
| **OpenCode HTTP Server**（Python 直接呼叫） | ✅ | ❌ | 低 | 高！ |
| **Python SDK（獨立）** | 不需要 | 不需要 | 最低 | N/A |

**最重要的新發現：OpenCode HTTP Server API**

OpenCode 跑起來後本身就是一個 HTTP server（`localhost:4096`）。Python 可以直接：
1. `GET /session` — 列出當前 session
2. `POST /mcp` — **動態新增 MCP server**（不需要重啟！）
3. `GET /event` — 訂閱 SSE 事件流，即時知道 AI 說了什麼
4. `POST /session/:id/message` — 注入訊息到 AI session

這開了一條完全不同的路：**OpenParty 可以用 Python 直接控制 OpenCode，讓它加入 Room，並即時監聽它說的話廣播給其他 agent**。

---

### 選定方向

**四層整合策略（由快到深）：**

```
Layer 4: OpenCode Plugin（JS/TS）+ Claude Code Plugin  →  最深整合，M3
         plugin hook 監聽 message.updated，自動廣播到 Room

Layer 3: Agent Skills（SKILL.md）                       →  最低成本，M2 先做
         .opencode/skills/ + .claude/skills/ 同一份檔案
         AI 自動知道「可以加入 OpenParty Room」

Layer 2: MCP Server（Python，已有原型）                  →  跨工具通用，M2
         OpenCode + Claude Code + Cursor 都能用
         openparty_mcp.py 已通過測試

Layer 1: Python SDK（pip install openparty）             →  最快，M2 初期
         M0 PoC 基礎，直接讓 Python 開發者使用
```

**bonus 路線：OpenCode HTTP API 直接整合**（M2 中期）
- 用 Python `requests` 直接呼叫 `localhost:4096`
- `POST /mcp` 動態注入 OpenParty MCP
- `GET /event` 監聽 AI 回應，自動廣播到 Room
- 這讓 OpenParty server 可以作為「OpenCode 的 Room 協調者」，完全不需要使用者手動設定

**選擇理由**：
- 不賭在任何一個工具上，MCP + SKILL.md 同時覆蓋兩個主要工具
- OpenCode HTTP API 是意外發現的高價值整合路線，成本低但整合深
- SKILL.md 的 `.claude/skills/` 路徑同時被兩個工具載入，一份文件雙覆蓋
- TypeScript SDK 的優先序因 HTTP API 可直接用 Python 呼叫而降低

### PoC 設計（已完成的部分）

MCP Server PoC（`openparty_mcp.py`）已通過測試：
- join_room / send_message / get_history / leave_room 都驗證可用
- stdio transport 正常，5/5 驗收標準通過

**下一個要驗證的（M2 初期）**：OpenCode HTTP API 整合
1. 啟動 `opencode serve`（或帶 TUI 的 opencode）
2. Python 用 `requests` 呼叫 `POST /mcp` 動態注入 openparty MCP
3. Python 訂閱 `GET /event` SSE 流，監聽 `message.updated`
4. 當 AI 說話，把內容廣播到 OpenParty Room

### 驗收標準

- [x] MCP server 可以用 stdio transport 啟動（已通過）
- [x] join_room / send_message / get_history 正常運作（已通過）
- [x] 整個流程不需要修改 server.py（已通過）
- [ ] 確認 OpenCode HTTP Server API 可用 Python 直接呼叫（M2 驗證）
- [ ] SKILL.md 在 OpenCode 和 Claude Code 都能正確載入（M2 驗證）

### 不在範圍內

- OpenCode Plugin（JS/TS）完整實作 — M3
- Claude Code Plugin 完整打包 — M3
- ACP 整合 — 不相關
- TypeScript SDK — 等真實用戶反饋

---

## 課題 2：目標使用者的核心 pain point 是什麼？

### 課題

**AI coding tool 使用者（Claude Code / Crush 用戶）的核心 pain point 是什麼？OpenParty 能解決的問題是否是他們真正在意的？**

### 背景

M0 PoC 驗證了技術可行性，但「技術上可行」不等於「有人需要」。這個課題要回答：
- 目標使用者的真實問題是什麼？
- OpenParty 的「異質 LLM 跨機器 Room 對話」能解決什麼問題？
- 使用者是誰？開發者、研究者、還是企業團隊？

不回答這個問題，M2 做出來的東西可能沒人用。

### 調研結果

#### 競品社群觀察

**AutoGen 社群（56k stars）高熱度討論主題：**
1. 「Interconnection between multi-agent microservices」— 同一個問題：不同 process 的 agent 怎麼互通？目前 AutoGen 的解法是 gRPC（重量級）
2. 「10 Generations of an Autonomous Agent: Cross-Session Memory」— 跨 session 記憶問題
3. 討論「parallel task execution」— 現有框架並發不好做

**CrewAI 社群（47k stars）高熱度討論主題：**
1. 「Managing shared state across crewAI tasks」— 狀態管理困難，框架缺乏原生支援
2. 「Trying to understand CrewAI - is this really about agents, or just managing LLM calls?」— 使用者困惑：CrewAI 到底解決了什麼問題？
3. 「How to handle rate limits when using multiple agents in parallel?」— rate limit 處理困難

**A2A Protocol 社群（22k stars）熱議：**
1. Agent Registry — 如何讓 agent 被其他 agent 找到？
2. 跨框架互操作性 — A2A 想解決的就是不同框架的 agent 無法通信的問題

#### 關鍵洞察

**Pain Point 1：跨 process 通信沒有簡單方案**
- AutoGen 需要 gRPC (複雜！)
- CrewAI 建議 Redis/外部存儲（需要基礎設施）
- A2A 是企業級協定（太重）
- OpenParty 的 WebSocket Room 是最輕量的方案

**Pain Point 2：單一 LLM 的觀點偏差問題**
- 很多開發者想「讓多個 AI 互相 review 我的代碼/設計」
- Claude Code 用戶最常見的工作流：「寫一段代碼，讓 Claude 審查」
- 但只有一個 LLM 的視角，可能有盲點
- 想法：讓 Claude Code 裡的 Claude 和另一台機器的 GPT 一起討論

**Pain Point 3：觀察 AI 自主工作的透明度**
- Claude Code 在跑長任務時，使用者想看到過程
- 「AI 在做什麼？對不對？要不要干預？」
- Observer 模式是高需求功能

**Pain Point 4：跨工具、跨廠商的協作**
- 用 Claude Code 工作的人，有時也用 Cursor、Gemini
- 想讓不同 AI tool 共用一個討論 room
- 現在沒有任何工具支援這個

#### 目標使用者分析

**最主要目標使用者（優先）：**

**A. 「Power User AI 開發者」**
- 特徵：每天用 Claude Code 或 Crush 的開發者，每月 API 花費 $50-200
- 需求：「我想讓 Claude 和 GPT 一起 review 我的架構決策」
- 為什麼選 OpenParty：最快的方式，不需要架設複雜框架
- 訪問路徑：Claude Code Plugin、Crush Agent Skills

**B. 「AI 研究者 / 實驗者」**
- 特徵：對 multi-agent 系統感興趣，想實驗不同 LLM 組合
- 需求：「我想看 Claude、GPT-4o、Gemini 在同一個 topic 上的差異」
- 為什麼選 OpenParty：M0 PoC 的核心場景，最適合
- 訪問路徑：Python SDK

**次要目標使用者（M2+）：**

**C. 「AI 工程師 / 企業開發者」**
- 特徵：建構生產級 multi-agent 系統
- 需求：「我需要幾個 agent microservice 能互相溝通」
- 為什麼選 OpenParty：比 gRPC/Redis 更簡單
- 訪問路徑：Python SDK + MCP（自建 server）

#### 競品缺口分析

| 場景 | AutoGen | CrewAI | A2A | OpenParty |
|------|---------|--------|-----|-----------|
| 跨機器通信 | gRPC（重） | 需要 Redis | HTTP（複雜） | WebSocket（輕） |
| 異質 LLM 混搭 | 支援但複雜 | 支援 | 不關心 | 原生設計 |
| Claude Code 整合 | 無 | 無 | 無 | MCP + Plugin |
| 即時觀察 | 無 | 無 | 無 | Observer 模式 |
| 上手複雜度 | 高 | 中 | 高 | 低（設計目標） |

**OpenParty 的差異化**：
1. 最輕量的跨機器多 agent 通信（WebSocket，無需 gRPC/Redis）
2. 原生設計給「Claude Code / AI coding tool」用戶
3. 人類可以觀察和插話（其他框架是純程式設計，不是人機協作）

### 選定方向

**聚焦在最有力的 2 個使用場景**：

**場景 1（主要）：「AI Pair Review」**
- 使用者在 Claude Code 裡工作，想讓 Claude 加入一個 Room
- Room 裡還有另一個 GPT/Gemini（可能在另一台機器）
- 兩個 AI 討論使用者的問題（代碼、設計、決策）
- 使用者旁觀，可以插話
- 這是「比 Claude Code 更強大的 AI 協作體驗」

**場景 2（次要）：「AI 自主辯論實驗」**
- 研究者想看不同 LLM 對同一 topic 的觀點差異
- `pip install openparty` 就能啟動一個 Room
- 5 分鐘上手

### PoC 設計

**不需要額外 PoC**：這個課題主要是調研和框架分析。結論已有足夠的信心。

「場景 1」驗收需要在有真實 Claude Code 使用者的情況下測試，屬於 M4（PMF 驗證）的範疇。

### 驗收標準

- [x] 找到競品的空缺（跨機器、異質 LLM、零基礎設施要求）
- [x] 定義出最有力的 2 個使用場景
- [x] 確認目標使用者是誰（Power User AI 開發者 + AI 研究者）
- [x] 理解為什麼現有競品無法很好滿足這些需求

### 不在範圍內

- 真實用戶訪談（時間成本高，但建議在 M4 做）
- 市場規模估算
- 競品的完整功能對比

---

## 課題 3：Python SDK vs TypeScript SDK，哪個先做？

### 課題

**OpenParty SDK 先做 Python 還是 TypeScript？**

### 背景

M0 的程式碼全是 Python。但目標使用者也包括 TypeScript 用戶（Claude Code 用 TypeScript，Crush 支援 TypeScript）。需要決定 SDK 的語言優先序。

### 調研結果

#### 語言生態分析

**Python SDK 理由**：
- M0 PoC 已有基礎（agent_sdk.py），最快交付
- 目標使用者 B（AI 研究者）幾乎都用 Python
- OpenAI SDK、Anthropic SDK 都以 Python 為主
- AutoGen、LangChain、CrewAI 全是 Python
- AI/ML 社群預設語言是 Python

**TypeScript SDK 理由**：
- Claude Code 本身是 TypeScript 寫的
- Crush 有 `npm install -g @charmland/crush`（Node.js）
- Claude Code Plugin 可以用 TypeScript 工具 bundle
- 前端工程師（也是 Claude Code 用戶）更熟悉 TS
- 但：Claude Code Plugin 可以用 Python MCP server（不需要 TS SDK）

#### 關鍵發現

**MCP 的出現改變了語言優先序決策**：
- 課題 1 確認 MCP 是主要整合機制
- MCP Python SDK（`pip install mcp`）已成熟
- MCP TypeScript SDK 也存在，但 Python 更廣泛使用
- Claude Code Plugin 可以 bundle Python MCP server，不需要 TS SDK

**目標使用者的技術棧**：
- 大多數 Claude Code / Crush 的「Power User」是全棧/後端，Python 沒問題
- TypeScript SDK 可以等第一批真實用戶有需求時再做

### 選定方向

**先做 Python SDK，M3 再評估是否需要 TypeScript**

理由：
1. M0 已有 Python 基礎，最快
2. 主要整合路徑（MCP）用 Python 就能覆蓋所有工具
3. 目標使用者以 Python 為主
4. 等有真實用戶反饋需要 TS 再做，避免過早投入

### PoC 設計

**不需要額外 PoC**：這是一個基於現有知識的決策，不需要驗證。

### 驗收標準

- [x] 確認 Python SDK 優先的理由充分
- [x] 確認 MCP 的存在讓「TypeScript 覆蓋」問題不再緊迫

### 不在範圍內

- TypeScript SDK 的設計（留到 M3）
- 其他語言（Go、Rust 等）
