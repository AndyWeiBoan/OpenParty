# PoC Design — Agent Skill + MCP「一句話加入 Room」

> M2 子課題  
> 日期：2026/03/29

---

## 課題

**用戶在 Claude Code 裡說「join room xxx」，Claude 能否自動呼叫 MCP tool 加入 OpenParty Room，並與另一個 Claude Code session 真正對話？**

---

## 背景

這是 OpenParty 最核心的使用者體驗假設：
> "用戶不需要懂 WebSocket，不需要寫程式，只需要說一句話"

M1 已驗證 MCP server 技術可行（`openparty_mcp.py` 測試通過），M2 已驗證跨機器 Room 可行。但這兩件事合在一起——「Claude Code 說話 → 自動呼叫 MCP → 加入 Room → 和另一個 Claude Code 對話」——從未端對端跑過。

**不驗證的風險：**
- 花 M2/M3 時間做 SKILL.md 和 MCP 正式化，但實際上 Claude 不會自動觸發 tool，每次都要用戶手動說「please call join_room tool」→ 使用者體驗差，整個產品定位崩潰

**三個技術不確定點：**

1. **SKILL.md 觸發機制**：Claude Code 讀到 SKILL.md 後，聽到「join room xxx」會自動呼叫 MCP tool？還是需要用戶明確說「use the tool」？

2. **MCP stdio + WebSocket 長連線共存**：MCP server 用 stdio transport 和 Claude Code 溝通，同時維持一條 WebSocket 長連線到 Room Server。這兩個 async loop 能否在同一個 process 裡穩定共存？（M1 驗證過，但當時 server.py 是舊版，現在有 Observer + event 格式升級）

3. **兩個 Claude Code session 真正互相對話**：Claude A 加入 Room → Claude B 加入 Room → Claude A 收到 your_turn → 生成回應 → Claude B 收到 → 生成回應。這個完整循環從未端對端跑過。

---

## 調研結果

### SKILL.md 觸發機制

**方向 A：純 SKILL.md，靠描述讓 Claude 自動判斷**
- SKILL.md 的 `description` 欄位說「當用戶說 join room 時觸發」
- Claude Code 在每次對話開始時載入所有 skills，根據 description 決定是否啟用
- 優點：零設定，用戶完全透明
- 缺點：不確定 Claude 是否一定會自動呼叫 tool，可能需要 prompt 引導

**方向 B：SKILL.md 提供詳細 workflow，用戶說「use openparty skill」**
- SKILL.md 作為操作手冊，Claude 按步驟執行
- 優點：確定性高，流程清晰
- 缺點：用戶需要知道「skill」這個概念，不夠自然

**方向 C：只靠 MCP tool description，不用 SKILL.md**
- MCP tool 的 description 本身說清楚「用於加入 Room」
- Claude 根據 tool description 自行判斷何時呼叫
- 優點：最簡單，一個元件
- 缺點：沒有 context，Claude 可能不知道「join room」和這個 tool 的關係

**現有參考：** `dotnet-trace-exception-analysis` SKILL.md 格式是 YAML frontmatter + Markdown 內文，description 說「Use when...」，Claude Code 會在適當時機自動載入。

### MCP stdio + WebSocket 共存

M1 的 `openparty_mcp.py` 做法：用 `asyncio.create_task` 在背景跑 WebSocket listener，MCP tool 呼叫是 async function，共用同一個 event loop。M1 測試通過，但測試是直接呼叫 Python function，不是真正走 stdio transport。

走 stdio transport 時，FastMCP 自己也有一個 async loop 在讀 stdin/寫 stdout。理論上兩個 loop（MCP + WebSocket）都在同一個 asyncio event loop 裡，應該可以共存，但需要實測確認。

---

## 選定方向

**SKILL.md 用方向 A**（純描述觸發）+ **方向 B**（詳細 workflow）的組合：
- `description` 欄位用方向 A 的觸發語言，讓 Claude 自動偵測
- SKILL.md 內文用方向 B 的詳細 workflow，確保 Claude 知道完整步驟

**MCP 用現有 `openparty_mcp.py`**，接上 M2 新的 server.py（加入 Observer event format），驗證 stdio transport 下 WebSocket 是否穩定。

---

## PoC 設計

**最小化實驗：同一台機器，兩個 terminal，各一個 Claude Code session，完成 2 輪對話。**

### 元件

```
Terminal 1: python server.py
Terminal 2: claude  ← 說「join room poc-skill-001 as Architect, discuss Redis caching」
Terminal 3: claude  ← 說「join room poc-skill-001 as Security, discuss Redis caching」

Claude A ──stdio──▶ openparty_mcp.py ──WS──▶ server.py ◀──WS── openparty_mcp.py ◀──stdio── Claude B
```

### 需要實作的東西

**1. `~/.claude/skills/openparty/SKILL.md`**
告訴 Claude：
- OpenParty 是什麼
- 聽到「join room XXX」時：呼叫 `join_room` → 呼叫 `check_your_turn` → 呼叫 `send_message` → loop
- MCP server 在哪裡（用戶需要先 `claude mcp add`）

**2. `openparty_mcp_v2.py`（升級版）**
- 接上新 server.py 的 Observer event format
- `check_your_turn` 要能感知 `turn_start` event
- 確保 stdio transport 下 WebSocket listener 不衝突

**3. `setup.sh`（一鍵設定）**
```bash
# 用戶只需要跑這一個腳本
./setup.sh
# 它會：
# 1. 安裝 openparty_mcp.py 到固定路徑
# 2. claude mcp add openparty -- python /path/to/openparty_mcp.py
# 3. 建立 ~/.claude/skills/openparty/SKILL.md
# 4. 啟動 server.py
```

### 測試步驟（PoC 跑法）

```
Step 1: ./setup.sh
Step 2: Terminal 2 打開 claude，說：
        "join room poc-skill-001 as Architect and discuss whether we should add Redis caching"
Step 3: Terminal 3 打開 claude，說：
        "join room poc-skill-001 as Security"
Step 4: 觀察兩個 Claude 是否開始互相對話
```

---

## 驗收標準

- [ ] **A1** 說「join room poc-skill-001」後，Claude 不需要用戶說「call the tool」就自動呼叫 `join_room` MCP tool
- [ ] **A2** MCP server 啟動後，WebSocket 連線到 server.py 正常，`get_room_status` 顯示已連線
- [ ] **A3** 兩個 Claude Code session 都加入同一個 Room，server log 顯示 2 個 agent
- [ ] **A4** Claude A 收到 `your_turn`，生成回應並呼叫 `send_message`
- [ ] **A5** Claude B 收到 Claude A 的訊息，生成回應（至少 2 輪完整交換）
- [ ] **A6** `setup.sh` 可以一鍵完成所有設定，用戶不需要手動操作

---

## 不在範圍內

- 跨機器（同一台機器兩個 terminal 就夠，跨機器是 M3）
- Groq / OpenAI 模型（Claude Code 本身就是 LLM，不需要額外 key）
- Observer CLI（已驗證，這次只看 server log）
- 超過 4 輪對話
- OpenCode 整合（先驗 Claude Code，通了再套到 OpenCode）
- Rolling Summary
