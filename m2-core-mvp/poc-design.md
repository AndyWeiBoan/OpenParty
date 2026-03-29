# M2 PoC Design — 核心產品 MVP

> 本文件定義 M2 要驗證的課題、驗收標準、範圍邊界。

---

## M2 的核心目標

M0 驗證了「技術可行」，M1 確認了「做什麼、給誰用」。  
M2 要回答：**這個東西能讓真實用戶在 5 分鐘內跑起來嗎？**

目標使用者心中的句子：
> "我想讓我的 Claude Code 和另一台機器的 AI 一起討論我的問題，現在就能做到嗎？"

---

## 課題清單（依優先序）

---

### 課題 1：修掉 agent_left bug，讓 Room 不再因一人離開而崩潰

**假設**：只要修 server.py 的 `agent_left` 處理邏輯，Room 就能在有人離開時繼續運作。

**背景**：M0 已知問題。目前 `agent_sdk.py` 收到 `agent_left` 後無條件 `leave()`，導致 3 人 room 一人離開其他人全跟著走。這是 M2 的基礎問題，不修就無法做跨機器驗證。

**PoC 設計**：
- 修改 `agent_sdk.py`：收到 `agent_left` 時，只有 room 只剩自己一個人時才離開
- 修改 `server.py`：`agent_left` 後如果 room 還有 ≥ 2 人，繼續 round-robin

**驗收標準**：
- [ ] 3 個 agent 跑一個 room，其中 1 個在第 3 輪後離開
- [ ] 剩下 2 個 agent 繼續對話，不受影響
- [ ] 剩最後 1 個時，它優雅退出

**不在範圍**：多人同時離開的 edge case

---

### 課題 2：Python SDK 正式化（openai-compatible 統一介面）

**假設**：用 `openai` Python package + `base_url` 參數，可以用一個統一的 `llm_fn` 接所有 OpenAI-compatible endpoint（本機 Ollama、cyril-ollama、Groq、OpenAI）。

**背景**：
- M0 的 agent 各自接不同 SDK（groq, gemini, anthropic...），用戶需要自己接
- MAGI 用 LiteLLM 做統一層，但 LiteLLM 依賴太重
- 更好的方案：`openai` package + `base_url` 換掉（Groq、Ollama 都是 OpenAI-compatible）
- 目標：`pip install openparty` 後，用戶只需設定 `base_url` + `model` 就能跑

**PoC 設計**：
建立 `m2-core-mvp/src/` 作為 SDK 原型：

```
src/
├── openparty/
│   ├── __init__.py
│   ├── agent.py      ← OpenPartyAgent（從 agent_sdk.py 升級）
│   ├── llm.py        ← 統一 LLM 介面（openai-compatible）
│   └── presets.py    ← Persona presets（code-review / debate）
```

`llm.py` 的核心介面：
```python
async def make_llm_fn(
    model: str,
    base_url: str | None = None,   # None = OpenAI, "http://localhost:11434/v1" = Ollama
    api_key: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int = 300,
) -> Callable[[dict], Awaitable[str]]:
    """回傳一個可以直接傳給 OpenPartyAgent 的 llm_fn"""
```

**驗收標準**：
- [ ] `make_llm_fn(model="llama3.1:8b", base_url="http://localhost:11434/v1")` 可以跑
- [ ] `make_llm_fn(model="qwen3:14b", base_url="http://localhost:11434/v1")` 也可以跑
- [ ] 兩個 agent 用不同 model + base_url 加入同一個 room，對話正常
- [ ] `api_key` 為 None 時自動 fallback 到 `"ollama"`（Ollama 不需要真實 key）

**不在範圍**：Anthropic、Gemini 等非 OpenAI-compatible 廠商（留到用戶有需求再接）

---

### 課題 3：跨機器驗證（本機 Ollama + cyril-ollama）

**假設**：用 cyril（`172.16.64.147:11434`）上的模型作為一個 agent，本機 Ollama 的模型作為另一個 agent，兩者加入同一個 Room，對話正常。

**背景**：這是 OpenParty 最核心的價值主張——「跨機器、異質 LLM」。M0 只在 localhost 驗證，M2 必須驗證真正的跨機器場景。cyril 有 `qwen3-coder:30b`, `gemma3:27b` 等模型，正好和本機的 `qwen3:14b`, `mistral-nemo:12b` 形成異質組合。

**PoC 設計**：
1. 在本機啟動 `server.py`（`ws://0.0.0.0:8765`，對外暴露）
2. 本機跑 agent A：`make_llm_fn(model="qwen3:14b", base_url="http://localhost:11434/v1")`
3. 本機跑 agent B：`make_llm_fn(model="qwen3:14b", base_url="http://172.16.64.147:11434/v1", ...)`
   → 這個 agent 的 LLM 在 cyril，但 WebSocket 連回本機 server
4. 觀察對話是否正常

注意：cyril 的模型回應速度可能較慢，需要調整 timeout。

**驗收標準**：
- [ ] server.py 改為 `host="0.0.0.0"` 接受非 localhost 連線（如需要）
- [ ] 本機 agent 用 `localhost:11434` 的模型正常回應
- [ ] cyril agent 用 `172.16.64.147:11434` 的模型正常回應
- [ ] 兩者在同一個 Room 對話，≥ 5 輪不中斷
- [ ] 延遲可接受（cyril 跨網路延遲 < 30s per turn）

**不在範圍**：真正跨 internet 的部署（M3），目前只驗證 LAN 內

---

### 課題 4：Observer 模式（read-only 連線）

**假設**：在現有 WebSocket 架構上加一個 `role: "observer"` 的連線類型，觀察者可以即時看到所有訊息但不參與 round-robin。

**背景**：「AI Pair Review」場景的核心是使用者旁觀 AI 討論。沒有 Observer 模式，這個場景就無法實現。

**PoC 設計**：
- server.py 加入 Observer 支援：`{"type": "join", "role": "observer", ...}`
- Observer 不加入 `room.agents`（不參與 round-robin）
- Observer 加入 `room.observers`（獨立 set）
- 所有廣播同時送給 observers
- 加入 WebSocket event 格式升級（見課題 5）

**驗收標準**：
- [ ] Observer 加入 room 不影響 round-robin
- [ ] Observer 即時收到所有訊息（message, agent_joined, agent_left, your_turn 事件）
- [ ] Observer 離開不影響其他 agent
- [ ] 2 個 agent + 1 個 observer 的場景跑 5 輪正常

**不在範圍**：Observer 插話（M2 後期）、Web UI（M3）

---

### 課題 5：WebSocket event 格式升級

**假設**：把 server 的廣播訊息格式標準化（加入更多 event type），讓 Observer 和未來的 UI 能知道 room 的完整狀態。

**背景**：M0 的訊息格式很基本。參考 MAGI 的 event streaming 設計，讓 server 發出更豐富的事件。

**新增 event types**：
```json
{"type": "room_state", "turn_count": 5, "current_speaker": "llama", "participants": [...]}
{"type": "turn_start", "agent_id": "...", "name": "Llama", "turn_number": 6}
{"type": "turn_end", "agent_id": "...", "name": "Llama", "latency_ms": 1200}
```

**驗收標準**：
- [ ] server 在每次 `your_turn` 前廣播 `turn_start`
- [ ] server 在收到 `message` 後廣播 `turn_end`（含 latency）
- [ ] Observer 收到完整的 event 序列

---

## 課題優先序決定

```
優先做（Milestone 2a）：
  課題 1 → 修 bug（基礎）
  課題 2 → SDK 正式化（基礎）
  課題 3 → 跨機器驗證（核心價值主張）

之後做（Milestone 2b）：
  課題 4 → Observer 模式
  課題 5 → Event 格式升級
```

---

## 不在 M2 範圍內

- Rolling Summary（M2 後期，需要額外 LLM call）
- Room 持久化（M3）
- 對外部署（M3）
- Web UI（M3）
- TypeScript SDK（等用戶反饋）
- OpenCode HTTP API 整合（M2 中期，確認 opencode 版本後再做）
