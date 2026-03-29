# M2 PoC Implement — 實作記錄

---

## 實作思路

M2 主線是讓「任何人 5 分鐘內就能跑起來一個跨機器多 LLM Room」。實作分四條線平行推進：

1. **Bug fix**：agent_left 時全員退場的問題，修 server.py + agent_sdk.py
2. **SDK 正式化**：把 M0 的各自接 SDK 改成 openai-compatible 統一介面
3. **跨機器驗證**：本機 llama3.2 + cyril gemma3:12b 真實對話
4. **Observer 模式**：read-only 連線 + 結構化 event 格式（MAGI 啟發）

中途遇到 Qwen3 系列 thinking mode 問題（見 poc-log.md）。

---

## 檔案結構

```
m2-core-mvp/
├── poc-design.md
├── poc-implement.md        ← 本文件
├── poc-result.md
├── poc-log.md
├── magi-research.md        ← MAGI 研究筆記
└── src/
    ├── openparty/
    │   ├── __init__.py     ← 公開 API：OpenPartyAgent, make_llm_fn, get_preset
    │   ├── agent.py        ← OpenPartyAgent（M0 agent_sdk.py 升級版）
    │   ├── llm.py          ← make_llm_fn，openai-compatible 統一介面
    │   └── presets.py      ← Persona presets (code-review / debate / research)
    ├── run_cross_machine.py ← 跨機器驗證腳本
    └── observer_cli.py      ← Observer CLI，即時觀看 Room

# Root level (修改的檔案)
server.py                   ← M2 升級：Observer + event 格式 + bug fix
agent_sdk.py                ← Bug fix：agent_left 不再全員退場
```

---

## 關鍵設計決策

### 1. openai package 而非 LiteLLM

最初考慮用 LiteLLM 作為統一層（MAGI 的做法），但決定用 `openai` package + `base_url` 替換：
- LiteLLM 依賴很重（幾十個 sub-packages），不適合 `pip install openparty`
- Groq、Ollama、OpenRouter 都是 OpenAI-compatible，`base_url` 換掉就搞定
- `openai` package 已是 AI 開發者的標準依賴

### 2. max_tokens=0 = 不設限

Qwen3 系列是 thinking model，thinking tokens 會先把 `max_tokens` budget 用完，導致 `content` 空白（`finish_reason=length`）。設 `max_tokens=0` 表示不傳這個參數，讓模型自己決定長度。

### 3. `_strip_thinking()` 過濾

有些模型（Qwen3.5）偶爾把 `<think>...</think>` 或 `Thinking Process:` 部分混入 `content`。加了 regex 過濾確保送進 Room 的是乾淨的回答。

### 4. Observer 不進 round-robin

Observer join 時加入 `room.observers` dict 而非 `room.agents`，完全不影響 turn-taking 邏輯。所有 broadcast 都同時送給 observers。

### 5. turn_start / turn_end events

受 MAGI WebSocket event 設計啟發。`turn_start` 在 `your_turn` 發出時廣播（讓 observer 知道「某人在思考中」），`turn_end` 在收到 `message` 後廣播（附帶 latency_ms）。

---

## 如何執行

### 前置條件
```bash
pip install openai websockets
# 需要 Ollama 在 localhost:11434 跑，或設定任何 OpenAI-compatible endpoint
```

### 跑跨機器 demo
```bash
# 終端機 1：啟動 server
python server.py

# 終端機 2：啟動 Observer（可選，看即時對話）
cd m2-core-mvp/src
python observer_cli.py --room m2-demo-001

# 終端機 3：跑 agents
cd m2-core-mvp/src
python run_cross_machine.py
```

### 換模型
```bash
# Agent 2 用 cyril 的 gemma3:27b（更強）
AGENT2_MODEL=gemma3:27b python run_cross_machine.py

# Agent 2 用 Groq
AGENT2_BASE_URL=https://api.groq.com/openai/v1 AGENT2_MODEL=llama-3.3-70b-versatile python run_cross_machine.py
```
