# M1 PoC Result — 研究結果

> 本文件記錄 M1 各課題的研究結論。

---

## 課題 1：OpenCode / Claude Code Plugin 機制研究

### 結論

> **anomalyco/opencode（132k stars）有完整的整合機制：Plugin（JS/TS）、MCP、Agent Skills、HTTP Server API、SDK。Claude Code 有 Plugin、MCP、Agent Skills。兩個工具共用 `.claude/skills/` 路徑。最重要的新發現是 OpenCode 的 HTTP Server API，讓 Python 可以直接程式化控制 OpenCode 加入 Room，是成本最低、整合最深的路線之一。採四層整合策略。**

### 驗收標準結果

- [x] **MCP server 可以用 stdio transport 啟動，沒有 crash**
  - 通過。FastMCP 啟動正常，6 個工具正確註冊。

- [x] **join_room / send_message / get_history 正常運作**
  - 通過。Mock Agent 成功收到廣播訊息，5/5 驗收標準通過。

- [x] **整個流程不需要修改 server.py（向後相容）**
  - 通過。MCP server 作為普通 WebSocket client 連接。

- [ ] **OpenCode HTTP Server API 可用 Python 直接呼叫** → M2 驗證
- [ ] **SKILL.md 在 OpenCode 和 Claude Code 都能正確載入** → M2 驗證

### 選定方案

**四層整合策略：**

```
Layer 4: OpenCode Plugin (JS/TS) + Claude Code Plugin      → M3
         plugin hook 自動廣播 message.updated 到 Room

Layer 3: Agent Skills (SKILL.md)                           → M2
         .opencode/skills/ 和 .claude/skills/ 同一份檔案
         讓 AI 自動知道「可以加入 OpenParty Room」

Layer 2: MCP Server (openparty_mcp.py，已驗證)             → M2
         OpenCode + Claude Code + Cursor 共用

Layer 1: Python SDK (pip install openparty)                → M2 初期
         M0 PoC 基礎
```

**Bonus：OpenCode HTTP Server API（Python 直接呼叫）** → M2 中期
- `POST /mcp` 動態注入 OpenParty MCP（不需重啟 opencode）
- `GET /event` SSE 流監聽 AI 回應，自動廣播到 Room
- Python `requests` 即可，無需 TS SDK

### 重要調研結論

1. **正確的 OpenCode 是 anomalyco/opencode（132k stars，TypeScript）**
   - 初版研究搞錯目標，本版已修正
   - OpenCode 有 client/server 架構，HTTP API 是核心特性

2. **OpenCode 和 Claude Code 共用 `.claude/skills/` 路徑**
   - 做一份 SKILL.md，兩個工具都能載入
   - 這是覆蓋兩個生態的最低成本方式

3. **OpenCode HTTP Server API 是意外發現的高價值整合路線**
   - `opencode serve` 暴露完整 REST API
   - Python 可以直接控制 OpenCode，無需 TS SDK
   - `POST /mcp` 動態注入 MCP，`GET /event` 即時監聽

4. **MCP 仍是跨工具通用整合的標準層**
   - openparty_mcp.py 已驗證可用
   - OpenCode + Claude Code + Cursor 都支援

### 對 ROADMAP 的影響

1. **產品形態確認**：Python SDK + MCP + SKILL.md 為核心，Plugin 為 M3 加分
2. **新增 OpenCode HTTP API 整合**為 M2 中期任務
3. **TypeScript SDK 優先序再降低**：HTTP API 讓 Python 可以直接控制 OpenCode
4. **ROADMAP 中「OpenCode」描述需更新**為正確的 anomalyco/opencode

### 下一步

1. M2 初期：Python SDK + MCP Server 正式化
2. M2 初期：做 SKILL.md（`.opencode/skills/` 和 `.claude/skills/` 雙路徑）
3. M2 中期：用 Python 驗證 OpenCode HTTP API 整合路線

---

## 課題 2：目標使用者的核心 pain point

### 結論

> **目標使用者是「Power User AI 開發者」和「AI 研究者」。核心 pain point 是「想讓多個 AI（不同廠商、不同機器）在同一個 Room 討論，但現有工具要不太重（gRPC/Redis）、要不沒有跨工具支援」。最強的使用場景是「AI Pair Review」。**

### 驗收標準結果

- [x] 找到競品的空缺 — AutoGen 需要 gRPC，CrewAI 需要 Redis，A2A 是企業協定，OpenParty 是目前最輕的方案
- [x] 定義出最有力的使用場景 — AI Pair Review（主要）、AI 自主辯論（次要）
- [x] 確認目標使用者 — Claude Code / Crush 的 Power User 開發者 + AI/ML 研究者
- [x] 理解競品無法滿足的原因 — 競品全是「同機器、同框架」，OpenParty 是「跨機器、異質 LLM、零基礎設施」

### 選定方案

聚焦場景：
1. **AI Pair Review**（主要）：Claude Code 用戶讓 Claude 加入 Room，另一個 GPT/Gemini 加入，一起討論
2. **AI 自主辯論**（次要）：研究者讓多個 LLM 討論同一 topic

### 對 ROADMAP 的影響

- 確認「Claude Code / Crush 整合」是 M2 最高優先級功能
- 「Observer 模式」（人類旁觀）是核心場景需求，必須在 M2 做
- 「AI Pair Review」的使用場景要在文件和 README 中明確說明

---

## 課題 3：Python SDK vs TypeScript SDK

### 結論

> **先做 Python SDK。MCP 的整合機制讓 TypeScript 的優先序降低，因為 Python MCP server 可以被所有工具使用。TypeScript SDK 留到 M3 再根據真實用戶反饋決定。**

### 驗收標準結果

- [x] Python SDK 優先的理由充分 — M0 基礎、Python 生態、MCP 覆蓋所有工具
- [x] TypeScript 不緊迫 — Claude Code Plugin 可 bundle Python MCP server，不需要 TS SDK

### 選定方案

**Python SDK 優先（M2），TypeScript SDK 待評估（M3）**

### 對 ROADMAP 的影響

- M2 不需要做 TypeScript SDK，節省約 2-3 週開發時間
- M3 的 TypeScript 項目改為「選評估項」而非「必做項」

---

## M1 整體結論

### 產品定位確定

**OpenParty = 最簡單的「跨機器、異質 LLM、Room 對話」基礎設施**

- **Who**：Claude Code / Crush 的 Power User 開發者 + AI 研究者
- **What**：讓不同 AI tool / 不同機器的 LLM 能加入同一個討論 Room
- **Why**：現有競品要不太重（gRPC/Redis），要不無跨工具支援
- **How**：Python SDK + MCP Server（主要），Claude Code Plugin（加分）

### 產品形態確定（三層）

```
Layer 3: Claude Code Plugin + Crush Agent Skills（M2+）
Layer 2: MCP Server（M2）
Layer 1: Python SDK（M2 初期）
```

### 對 M2 的指引

**M2 必做（有序）：**
1. Python SDK 正式化（`pip install openparty`）
2. MCP Server 正式化（openparty_mcp.py → 完整版）
3. Observer 模式（人類旁觀）
4. Claude Code Plugin 基礎版（bundle MCP + SKILL.md）
5. Rolling Summary（超過 25 條時壓縮）

**M2 選做（根據用戶反饋）：**
- TypeScript SDK
- Room 持久化（SQLite）

### ROADMAP 需要更新

1. 「OpenCode」改為「Crush」
2. M2 新增「Claude Code Plugin / Crush Agent Skill」任務
3. M2 「TypeScript SDK」改為選做
4. 加入「Observer 模式」為 M2 高優先任務
