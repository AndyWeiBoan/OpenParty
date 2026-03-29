# M2 PoC Result — 研究結果

> 日期：2026/03/29

---

## 結論

> **M2 核心基礎設施全部驗證通過。Python SDK（openai-compatible 統一介面）+ 跨機器 Room（本機 llama3.2 + cyril gemma3:12b）+ Observer 模式，都在 22 秒內完成 6 輪真實對話。**

---

## 驗收標準結果

### 課題 1：agent_left bug fix
- [x] 3 agent 中 1 個離開，剩下 2 個繼續對話 — **通過**
- [x] `agent_left` 訊息帶 `agents_remaining` 欄位 — **通過**
- [x] 最後 1 人時優雅退出 — **通過**

### 課題 2：Python SDK 正式化
- [x] `make_llm_fn(model="llama3.2:latest", base_url="http://localhost:11434/v1")` 可用 — **通過**
- [x] `make_llm_fn(model="gemma3:12b", base_url="http://172.16.64.147:11434/v1")` 可用 — **通過**
- [x] 兩個不同 model + base_url 在同一個 room 對話正常 — **通過**
- [x] api_key=None 時 Ollama 自動 fallback — **通過**
- [x] Persona presets (code-review / debate / research) 可用 — **通過**

### 課題 3：跨機器驗證
- [x] server.py 改為 `host="0.0.0.0"` — **通過**
- [x] 本機 llama3.2:latest 正常回應 — **通過（~3s/turn）**
- [x] cyril gemma3:12b 正常回應 — **通過（~5s/turn）**
- [x] 兩者在同一 Room 完成 6 輪對話 — **通過（22 秒）**
- [x] 延遲可接受（cyril 跨 LAN < 6s per turn） — **通過**

### 課題 4：Observer 模式
- [x] Observer join 不影響 round-robin — **通過**
- [x] Observer 即時收到所有 message / agent_joined / agent_left 事件 — **通過**
- [x] Observer 離開不影響其他 agent — **通過**
- [x] 2 agents + 1 observer 的場景跑完整 4 輪 — **通過**

### 課題 5：WebSocket event 格式升級
- [x] `turn_start` 在 your_turn 發出時廣播 — **通過**
- [x] `turn_end` 在 message 收到後廣播（含 latency_ms）— **通過**
- [x] Observer 收到完整 event 序列：`joined → agent_joined × 2 → turn_start → message → turn_end → ...` — **通過**

---

## 意外發現（寫入 poc-log.md）

### Qwen3 系列 thinking mode 問題
- `max_tokens` 設太小 → thinking tokens 耗盡 budget → `content` 空白
- 解法：`max_tokens=0`（不傳此參數）
- 次要解法：`_strip_thinking()` 過濾洩漏進 content 的 `<think>` 塊

### cyril 最佳模型選擇
- `qwen3.5:latest`：thinking mode 慢，某些 prompt 會 timeout
- `gemma3:12b`：乾淨輸出，~5s/turn，最適合 Room 對話場景
- `qwen3-coder:30b`：待測試（18.6GB，應該最強但最慢）

---

## 對 ROADMAP 的影響

1. **M2 核心基礎設施完成**：SDK + 跨機器 + Observer 都驗證
2. **Qwen3 thinking model 需要特別處理**：已在 SDK 層解決，上層透明
3. **cyril 推薦用 gemma3:12b 或 gemma3:27b** 作為 Room agent（非 qwen3.5）
4. **下一步**：Rolling Summary（Phase 2 Memory）+ `pip install openparty` 正式打包

---

## 下一步建議

**M2 剩餘工作（按優先序）**：
1. **`pip install openparty` 打包**：`pyproject.toml`，讓用戶一行安裝
2. **動態 topic**：Room 建立時可指定 topic（目前 hardcoded 在 server.py）
3. **Rolling Summary**：超過 25 條時 async 壓縮（已有 `rolling_summary` 欄位預留）
4. **MCP Server 正式化**：把 m1 的 `openparty_mcp.py` 升級接新 SDK
5. **gemma3:27b 測試**：比 12b 強，看 Room 對話品質是否值得等待時間
