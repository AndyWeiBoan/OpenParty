# Hermes-Agent 值得 OpenParty 學習的設計

> 研究來源：`~/3rd-party/hermes-agent`（Nous Research, v0.9.0）
> 整理自 claude-sonne & claude-sonne-2 的聯合研究

---

## 🏗 架構設計

### Tool Registry Pattern
- **概念**：每個 tool 模組自行呼叫 `register()`，registry 本身不依賴任何 tool，避免循環 import
- **關鍵檔案**：
  - `tools/registry.py` — tool 自我註冊機制
  - `model_tools.py` — tool discovery，查詢 registry，不寫死 tool 列表
- **好處**：新增 tool 只需建立新模組，核心程式碼零修改

### Toolset 組合系統
- **概念**：工具分群組，用 `includes` 組合多個 toolset；`check_fn` 做 capability gate（例如缺 API key 自動禁用）
- **關鍵檔案**：
  - `toolsets.py` — toolset 定義與組合邏輯
  - `toolset_distributions.py` — 各平台/用途的 toolset 分發

### Schema-First Tools
- **概念**：每個 tool 定義 OpenAI-compatible JSON Schema，model 自動發現可用工具
- **關鍵檔案**：
  - `tools/*.py` — 各 tool 的 schema 定義（每個 tool 內含 `schema` dict）
  - `model_tools.py` — 載入與過濾 tool schema

### Modular Prompt Building
- **概念**：system prompt 由多個 stateless function 組合，每段可獨立測試或 A/B test
- **關鍵檔案**：
  - `agent/prompt_builder.py` — 各段 prompt 的組合邏輯

---

## 🤖 Multi-Agent 協調

### 子 Agent 沙箱隔離
- **概念**：child agent 擁有獨立 context + 獨立 terminal session；parent 只看到最終摘要，不暴露中間推理
- **關鍵檔案**：
  - `tools/delegate_tool.py` — subagent 生成、credential 繼承、progress relay

### 工具集繼承上限（Ceiling Model）
- **概念**：子 agent 可用工具永遠是 parent 的子集，無法獲得比 parent 更多的工具
- **關鍵檔案**：
  - `tools/delegate_tool.py` — 工具繼承上限邏輯（blocked tools 強制移除）

### 遞迴深度限制
- **概念**：MAX_DEPTH=2，grandchild 直接拒絕，防止無限遞迴
- **關鍵檔案**：
  - `tools/delegate_tool.py` — depth 檢查

### 中斷傳播
- **概念**：parent interrupt → 遍歷 `_active_children` 逐一呼叫 `child.interrupt()`；僅影響該 thread，不干擾其他 session
- **關鍵檔案**：
  - `run_agent.py` — `interrupt()` 方法與 `_active_children` 管理

---

## ⚡ 並行執行策略

### 工具並行安全分類
- **概念**：三類工具分別處理：
  - `_NEVER_PARALLEL`：需要使用者互動（序列執行）
  - `_PARALLEL_SAFE`：冪等工具（web_search, read_file）可並行
  - `_PATH_SCOPED`：檔案操作只要路徑不重疊就可並行
- **關鍵檔案**：
  - `run_agent.py` — `_should_parallelize_tool_batch()` 方法

### Path Overlap 偵測
- **概念**：並行執行前偵測目標路徑是否重疊，防止 race condition
- **關鍵檔案**：
  - `run_agent.py` — path overlap 檢查邏輯

---

## 📋 任務管理（Todo System）

### 讀寫合一的 Todo Tool
- **概念**：單一 `todo` tool，write 時帶 `todos` 參數，read 時省略；`merge=false` 整批替換，`merge=true` 按 id 更新單項
- **關鍵檔案**：
  - `tools/todo_tool.py` — TodoStore class，merge/replace 模式，schema 描述行為約束

### TodoStore Hydration（Stateless Agent 重建狀態）
- **概念**：Gateway 每訊息建立新 agent 實例，啟動時從歷史對話反向掃描最後一次 todo 回應，自動還原任務狀態，不需 persistent object
- **關鍵檔案**：
  - `run_agent.py` — `_hydrate_todo_store()` 方法

### File-based Cron Job Storage
- **概念**：任務直接存在 JSON 檔案，不需 Redis/MQ；支援 `once`、`interval`、`cron` 三種類型
- **關鍵檔案**：
  - `cron/` — cron job 相關邏輯
  - `tools/cronjob_tools.py` — cron tool 介面

### Preemptive next_run 更新（Crash-Safe）
- **概念**：任務開始執行前就先更新下次執行時間，crash 也不會重複觸發；重啟後錯過的任務直接快轉到下一個未來執行時間
- **關鍵檔案**：
  - `cron/` — `advance_next_run()` 邏輯

### Cross-Process Lock
- **概念**：Unix `fcntl` + Windows `msvcrt`，防止多個 gateway 進程同時 tick 同一個任務
- **關鍵檔案**：
  - `cron/` — file-based lock 實作

---

## ⏱ 長任務處理

### Iteration Budget + Grace Call
- **概念**：parent 90 iterations，subagent 50 iterations；`execute_code` iteration 可退款；budget 耗盡時不直接中斷，給模型最後一次機會做收尾
- **關鍵檔案**：
  - `run_agent.py` — `IterationBudget` class（thread-safe counter）、grace call 邏輯

### Structured Context Compression（非截斷）
- **概念**：prompt 達 50% context limit 時觸發；用 auxiliary LLM 做結構化摘要（追蹤 Resolved/Pending questions、Remaining Work）；壓縮前 flush memory；壓縮後自動注入 todo snapshot；SQLite session 自動 split 保留歷史可溯
- **關鍵檔案**：
  - `run_agent.py` — `_compress_context()` 方法
  - `agent/context_compressor.py` — 壓縮邏輯與 iterative 更新摘要

### Context Pressure 分級警告
- **概念**：50% 黃色 → 70% 橘色 → 85% 紅色，不是突然爆炸
- **關鍵檔案**：
  - `run_agent.py` — `_emit_context_pressure()` 方法

### Inactivity-based Timeout（非 Wall-clock）
- **概念**：不限制總執行時間，而是偵測活動——每 5 秒 poll，超過 10 分鐘沒有 token/tool 活動才 kill；正常跑的長任務不被誤殺
- **關鍵檔案**：
  - `run_agent.py` — `get_activity_summary()` 方法、inactivity 偵測邏輯
  - `gateway/session.py` — timeout handler

### Session Isolation for Scheduled Jobs
- **概念**：每個 cron job 有獨立 `session_id`，`skip_memory=True`、`skip_context_files=True`，不汙染主對話
- **關鍵檔案**：
  - `cron/` — job 執行時的 session 建立

---

## 🔄 Batch 長任務 & Resume

### Content-based 完成偵測
- **概念**：不用 index 判斷完成，按 prompt 文字內容比對；dataset 順序改變也不影響 resume
- **關鍵檔案**：
  - `batch_runner.py` — `_scan_completed_prompts_by_content()` 方法

### 增量 Checkpoint + 失敗 Retry
- **概念**：每個 batch 完成後原子寫入 checkpoint（非 fatal）；只有成功存入 trajectory 才標為完成，否則下次 resume 時重試
- **關鍵檔案**：
  - `batch_runner.py` — `_save_checkpoint()`、`_load_checkpoint()` 方法

### Shadow Git Checkpoint（檔案操作可回滾）
- **概念**：每個 directory 用 shadow git repo 做快照；在破壞性操作（`write_file`、`rm`、`sed -i`）前自動建立；同一 directory 每輪只建一次；restore 前再建 pre-rollback snapshot
- **關鍵檔案**：
  - `tools/checkpoint_manager.py` — `CheckpointManager` class，`ensure_checkpoint()`、`restore()` 方法

---

## 💾 資料儲存

### SQLite + FTS5 全文搜尋
- **概念**：不需外部搜尋引擎，WAL 模式支援多讀單寫，直接搜所有對話歷史
- **關鍵檔案**：
  - `gateway/session.py` — session DB 管理
  - `tools/session_search_tool.py` — FTS5 搜尋 + LLM 摘要

### 兩層記憶架構
- **概念**：MEMORY.md（事實）+ USER.md（使用者模型），任務結束後自動同步；用 `<memory-context>` tag 包圍，防止 model 把記憶當新輸入
- **關鍵檔案**：
  - `agent/memory_manager.py` — 記憶讀寫與同步
  - `tools/memory_tool.py` — memory tool 介面

### Atomic JSON Writes
- **概念**：write-to-temp + atomic rename，防止 crash 造成資料損毀
- **關鍵檔案**：
  - `utils.py` — `atomic_json_write()` 函式（全專案共用）

### 精細 Cost Tracking
- **概念**：每個 session 紀錄 input/output/cache tokens 以及 `estimated_cost_usd` vs `actual_cost_usd`
- **關鍵檔案**：
  - `agent/usage_pricing.py` — token 計費邏輯
  - `gateway/session.py` — session cost 紀錄

---

## 🔌 多供應商容錯

### Multi-Provider Fallback Chain
- **概念**：OpenRouter → Nous Portal → 自訂 endpoint → Anthropic，HTTP 402 自動切下一個；不 lock-in 單一供應商
- **關鍵檔案**：
  - `agent/auxiliary_client.py` — fallback chain 邏輯
  - `agent/retry_utils.py` — retry + error 分類

### Credential Pool 輪換
- **概念**：多組 API key 自動輪換，遇到 429 rate limit 自動切換下一組
- **關鍵檔案**：
  - `agent/credential_pool.py` — key pool 管理

### Auxiliary Client 模式
- **概念**：摘要、視覺、搜尋等輔助任務走獨立 client + fallback chain，不佔用主 agent 額度
- **關鍵檔案**：
  - `agent/auxiliary_client.py` — auxiliary client 實作

---

## 🔐 安全性

### Prompt Injection 掃描
- **概念**：注入外部檔案（SOUL.md, .cursorrules）前先掃不可見 unicode、HTML 注入、prompt 劫持模式
- **關鍵檔案**：
  - `agent/prompt_builder.py` — context 注入前掃描邏輯
  - `tools/skills_guard.py` — skill 安全掃描

### 敏感資訊遮蔽
- **概念**：所有 log 中自動遮蔽 phone、token、credentials
- **關鍵檔案**：
  - `agent/redact.py` — 遮蔽邏輯

### Skill Hub 信任層級
- **概念**：trust level（builtin / trusted / community）、安裝前內容掃描、quarantine + audit log
- **關鍵檔案**：
  - `tools/skills_hub.py` — skill 安裝與信任管理
  - `tools/skills_guard.py` — skill 內容掃描

---

## 📦 Progressive Disclosure（Skills 系統）

### 三層載入
- **概念**：metadata → 完整 SKILL.md → 附屬檔案，按需載入；避免把所有 skills 塞入 system prompt
- **關鍵檔案**：
  - `tools/skills_tool.py` — `skills_list`（metadata）、`skill_view`（完整內容）
  - `skills/` — skill 檔案目錄（YAML frontmatter + Markdown）

---

## 🌐 Gateway 架構

### 統一 Gateway 抽象
- **概念**：同一套 slash command 解析 + session 管理服務 13 個平台；新增平台只需實作 `Platform` subclass；同一 agent instance 可跨平台連續
- **關鍵檔案**：
  - `gateway/run.py` — gateway 啟動與平台初始化
  - `gateway/platforms/` — 各平台 adapter（`base.py` 定義介面）
  - `gateway/session.py` — 跨平台 session 管理

### Platform-Aware 格式化
- **概念**：agent 知道自己在哪個平台，自動調整輸出格式（不支援 markdown 的平台不輸出 markdown）
- **關鍵檔案**：
  - `gateway/display_config.py` — 平台格式設定

### Terminal Backend 抽象
- **概念**：一行環境變數切換 local / Docker / SSH / Modal；agent 不需知道底層執行環境
- **關鍵檔案**：
  - `tools/environments/` — Local、Docker、SSH、Modal 各 backend 實作

---

## 📊 優先實作建議

| 優先級 | 設計 | 理由 |
|--------|------|------|
| 🔴 高 | Tool Registry + Toolsets | 擴充工具零修改核心 |
| 🔴 高 | Multi-Provider Fallback Chain | Production 必備韌性 |
| 🔴 高 | SQLite FTS5 Session Store | 低依賴、全文搜尋、跨 session 記憶 |
| 🟡 中 | Todo hydration（stateless 重建） | 長任務不丟狀態 |
| 🟡 中 | 壓縮後注入 todo snapshot | 防止 agent 重做已完成任務 |
| 🟡 中 | Inactivity-based timeout | 比 wall-clock 更適合 agent 任務 |
| 🟡 中 | Shadow git checkpoint | 低成本可回滾保證 |
| 🟢 低 | Credential Pool 輪換 | 高流量時才會需要 |
| 🟢 低 | Gateway 多平台抽象 | 需要多平台時再實作 |
