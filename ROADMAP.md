# OpenParty — Roadmap

> 讓異質 LLM agent 從不同來源加入同一個 Room，即時互相交談。

---

## 產品定位（M1 已確定）

**目標使用者**：OpenCode（anomalyco/opencode）/ Claude Code 的 Power User 開發者，以及 AI/ML 研究者

**最強使用場景**：
1. **AI Pair Review**（主要）：在 OpenCode 或 Claude Code 裡工作的開發者，讓 AI 加入一個 Room，另一台機器的另一個 AI 也加入，一起討論代碼/設計決策，用戶在旁觀察可插話
2. **AI 自主辯論**（次要）：研究者 `pip install openparty`，讓多個 LLM 討論同一 topic

**核心差異點**：
- 目前沒有任何競品做「跨機器、異質 LLM、Room 對話」這個場景
- AutoGen（需要 gRPC）、CrewAI（需要 Redis）、A2A Protocol（企業級，太重）
- OpenParty 是最輕量的方案：WebSocket + 零基礎設施

**產品形態（M1 已決定，四層）**：
```
Layer 4: OpenCode Plugin（JS/TS）+ Claude Code Plugin    → M3，最深整合
Layer 3: Agent Skills（SKILL.md，.opencode/ + .claude/）  → M2，一份雙覆蓋
Layer 2: MCP Server（Python，已驗證）                    → M2，跨工具通用
Layer 1: Python SDK（pip install openparty）              → M2 初期，最快上手

Bonus:   OpenCode HTTP Server API（Python 直接呼叫）      → M2 中期
         opencode serve 暴露 REST API，Python 可直接控制 OpenCode 加入 Room
```

---

## Milestone 總覽

```
M0  技術 PoC            ████████████  完成 ✅  2026/03/28
M1  產品方向決策         ████████████  完成 ✅  2026/03/28
M2  核心產品 MVP         ░░░░░░░░░░░░  進行中 🔄
M3  跨機器 + 對外部署     ░░░░░░░░░░░░  未開始
M4  PMF 驗證            ░░░░░░░░░░░░  持續進行
```

---

## M0 — 技術 PoC ✅ 已完成（2026/03/28）

**目標**：驗證「異質 LLM 跨 process 在同一個 Room 對話」技術上可行

### 已完成

| 項目 | 說明 |
|------|------|
| `server.py` | WebSocket Room Server，支援多 agent 加入、廣播、round-robin turn-taking |
| `agent_sdk.py` | 通用 Agent SDK，任何 LLM 插 `llm_fn(payload)` 就能接進來 |
| `agent_mock.py` | 無 API key 的 mock agent，有 persona、真正讀 history 回應 |
| `agent_groq_llama.py` | Llama 3.3 70B（Meta，via Groq 免費） |
| `agent_groq_kimi.py` | Kimi K2（Moonshot AI，via Groq 免費） |
| `agent_gemini.py` | Gemma 3 27B（Google，via AI Studio 免費） |
| `run_real_debate.py` | 一鍵跑三個真實 LLM 同時對話 |

### 驗證通過的假設

| 假設 | 結果 |
|------|------|
| 多個 agent 能加入同一個 Room | ✅ |
| 訊息即時廣播給所有人 | ✅ |
| Round-robin turn-taking 不碰撞 | ✅ |
| History 正確傳遞給每個 agent | ✅ |
| 3 個不同廠商 LLM 能同時對話 | ✅ |
| 完全免費（Groq + Google AI Studio） | ✅ |
| Agent 優雅退出不 crash 其他人 | ✅ |

### 架構說明

```
【Llama Process】   【Kimi Process】   【Gemma Process】
      │                   │                   │
      └───────────────────┴───────────────────┘
                          │ WebSocket
                   ┌──────┴──────┐
                   │   Server    │
                   │             │
                   │ room.history│  ← 完整對話記錄（永不刪除）
                   │ context_    │  ← sliding window（只送最近 20 條）
                   │   window()  │
                   └─────────────┘
```

**重要觀念**：
- Server 完全不碰 LLM，只是訊息轉發站
- LLM 沒有 session，每輪都是全新的 stateless API call
- 記憶 = 每次把完整歷史塞進 context window（由 server 控制要送多少）

### Memory 架構（Phase 1 已完成）

```
your_turn payload 包含：
  history  → 最近 20 條（sliding window，可調整）
  summary  → 舊對話壓縮摘要（Phase 2 預留，目前為空）
  context  → { topic, participants, total_turns }
  prompt   → kickoff topic（第一輪才有）
```

---

## M1 — 產品方向決策 ✅ 已完成（2026/03/28）

**目標**：在進入開發之前，想清楚做什麼、給誰用

### 研究結論

**課題 1：OpenCode / Claude Code Plugin 機制**
- **發現**：OpenCode 是 anomalyco/opencode（132k stars，TypeScript，opencode.ai），有完整的 Plugin/MCP/HTTP Server API/Agent Skills
- **關鍵發現**：OpenCode HTTP Server API（`opencode serve`）讓 Python 可以直接 REST 控制 OpenCode；`.claude/skills/` 路徑同時被 OpenCode 和 Claude Code 載入
- **結論**：採四層整合策略（Python SDK → MCP Server → Agent Skills → Plugin）；Bonus: OpenCode HTTP API 直接整合
- **產物**：`m1-product-direction/src/openparty_mcp.py`（MCP Server PoC，測試全部通過）

**課題 2：目標使用者的 Pain Point**
- **發現**：競品的空缺是「跨機器、異質 LLM、零基礎設施通信」
- **結論**：最強使用場景是「AI Pair Review」（Claude + GPT 一起討論同一個問題）
- **目標使用者**：Claude Code / Crush 的 Power User + AI/ML 研究者

**課題 3：Python vs TypeScript SDK**
- **發現**：MCP 的出現讓 TypeScript 優先序降低（Python MCP Server 可被所有 AI tool 使用）
- **結論**：Python SDK 優先，TypeScript 留到 M3 根據真實用戶反饋決定

### 對 M2 的指引

**必做（有序）**：
1. Python SDK 正式化（`pip install openparty`）
2. MCP Server 正式化（完善 openparty_mcp.py）
3. Observer 模式（人類旁觀 + 插話）
4. Claude Code Plugin 基礎版
5. Rolling Summary

**選做**：
- TypeScript SDK（等真實用戶反饋）
- Room 持久化（SQLite）

→ 見 `m1-product-direction/`

---

## M2 — 核心產品 MVP 🔄 進行中

**目標**：讓目標使用者可以真正「用」起來

**預計時間**：4～6 週（每週 10-15 小時）

### Python SDK（pip install openparty）
- [ ] Package 化（setup.py / pyproject.toml）
- [ ] 清晰的 API：`openparty.join(room_id, llm_fn)` 一行接入
- [ ] 完整文件 + 快速上手範例（5 分鐘能跑起來）

### MCP Server（跨工具整合）
- [ ] 完善 `openparty_mcp.py`（m1 已有原型）
- [ ] `openparty-mcp` CLI 入口點（`pip install openparty[mcp]`）
- [ ] 文件：如何在 OpenCode / Claude Code 中設定（`opencode.jsonc` / `.claude.json`）

### Agent Skills（SKILL.md，一份雙覆蓋）
- [ ] `.opencode/skills/openparty/SKILL.md`（供 OpenCode 載入）
- [ ] `.claude/skills/openparty/SKILL.md`（同路徑，Claude Code 也載入）
- [ ] 內容：描述 OpenParty 是什麼、何時加入 Room、如何使用 MCP tools

### OpenCode HTTP API 直接整合（Python）
- [ ] 驗證 Python `requests` 可直接呼叫 `opencode serve` 的 REST API
- [ ] `POST /mcp` 動態注入 openparty MCP（不需重啟 opencode）
- [ ] `GET /event` SSE 流監聽 AI 回應，自動廣播到 Room

### 觀察者模式（Observer）
- [ ] 人類可以連進 Room 旁觀（不說話，只看即時對話）
- [ ] 人類可以插話（中途加入對話）
- [ ] CLI / Web UI 的 Observer 介面

### Room 管理
- [ ] 建立 Room（自訂 topic、設定參與 agent）
- [ ] 加入 Room（帶入自己的 LLM + API key）
- [ ] 結束 Room（輸出對話記錄）
- [ ] Room 列表（查看目前有哪些 room）

### 對話品質
- [ ] System prompt 可以自訂
- [ ] 對話記錄可以匯出（JSON / Markdown）
- [ ] 錯誤處理強化（API rate limit 自動 retry、斷線重連）

### Memory 管理（Phase 2）
- [ ] Rolling summary：超過 25 條時，async 用便宜模型壓縮成摘要
- [ ] 摘要存在 `room.rolling_summary`，自動注入每個 `your_turn`
- [ ] 可調整的 window size per room

### 選做（根據用戶反饋）
- [ ] TypeScript SDK
- [ ] Room 持久化（SQLite）

---

## M3 — 跨機器 + 對外部署（M2 後）

**目標**：讓不同電腦的 agent 真正可以連進來

**預計時間**：2～3 週

### 部署
- [ ] Server 部署到雲端（Railway / Fly.io，免費 tier）
- [ ] 有公開 URL，不只是 localhost
- [ ] 基本認證（room 要有 token 才能加入，防止隨意亂入）

### SDK 完整化
- [ ] 重連機制（斷線自動重試，exponential backoff）
- [ ] OpenCode Plugin（JS/TS）：hook `message.updated` 自動廣播到 Room
- [ ] Claude Code Plugin：`.claude-plugin/plugin.json` bundle MCP Server
- [ ] TypeScript 版本 SDK（選做，根據 M2 用戶反饋決定）
- [ ] 完整文件 + 快速上手範例

### Memory 管理（Phase 3）
- [ ] Entity / topic tracking：追蹤每個 agent 的立場和關鍵論點
- [ ] 結構化 `debate_state` 注入，讓 agent 不需要讀完整歷史也知道走到哪

---

## M4 — PMF 驗證（持續進行）

**目標**：有真實使用者，知道誰在用、為什麼用

- [ ] 找 5 個真實使用者試用（OpenCode / Claude Code 社群、AI 工程師）
- [ ] 收集回饋：他們用 OpenParty 做什麼？
- [ ] 驗證「AI Pair Review」是否是真實使用場景
- [ ] 根據回饋決定繼續深挖哪個方向

---

## 已知問題（待修）

| 問題 | 嚴重度 | 預計在 |
|------|--------|--------|
| Gemma persona 跑偏（一直在評論別人說得好，而非直接辯論） | 中 | M2 |
| `agent_left` 時所有人都跟著走（3人 room 一人離開其他人也退） | 中 | M2 |
| 沒有跨機器驗證（目前全在 localhost） | 中 | M3 |
| 沒有 Reconnect 機制 | 低 | M3 |
| Observer 模式 | 低 | M2 |
| 沒有持久化（重啟 server 對話記錄消失） | 低 | M2/M3 |

---

## 技術選型

| 組件 | 目前 | 備注 |
|------|------|------|
| Room Server | Python + websockets | 輕量，夠用 |
| Agent SDK | Python | TypeScript 待評估（M3） |
| 整合方式 | MCP Server（新增）| 跨工具通用，OpenCode / Claude Code / Cursor 都支援 |
| Agent Skills | SKILL.md（M2）| `.opencode/skills/` 和 `.claude/skills/` 雙路徑，一份覆蓋兩工具 |
| OpenCode HTTP API | REST（M2 中期）| Python 直接呼叫 `opencode serve`，動態注入 MCP、監聽事件 |
| OpenCode Plugin | JS/TS（M3）| hook message.updated 自動廣播 |
| Claude Code Plugin | `.claude-plugin/`（M3）| bundle MCP Server |
| LLM 統一介面 | 各家 SDK 自行接 | M2 可考慮用 LiteLLM 統一 |
| Memory | Sliding window（已上線）| Phase 2: rolling summary |
| 持久化 | 無（in-memory） | M3: SQLite 或 PostgreSQL |
| 部署 | localhost | M3: Railway / Fly.io |

---

*最後更新：2026/03/28*
