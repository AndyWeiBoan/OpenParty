# M0 PoC Implement — 實作紀錄

## 實作思路

三個組件，職責完全分離：

1. **Room Server**：純 WebSocket hub，只管廣播和 turn routing，完全不碰 LLM
2. **Agent SDK**：通用連線層，接收 `your_turn` → 呼叫 `llm_fn` → 送回 `message`
3. **llm_fn**：每個 LLM 自己實作，把 room history 轉成各家 API 格式

關鍵設計：**Server 不知道 LLM 的存在**，LLM 也不知道 WebSocket 的存在，中間靠 SDK 橋接。

## 檔案結構

```
src/
├── server.py            ← WebSocket Room Server
├── agent_sdk.py         ← 通用 Agent SDK（OpenPartyAgent class）
├── agent_mock.py        ← Mock agent（無需 API key，有 persona 的腳本 agent）
├── agent_groq_llama.py  ← Llama 3.3 70B（Meta，via Groq）
├── agent_groq_kimi.py   ← Kimi K2（Moonshot AI，via Groq）
├── agent_gemini.py      ← Gemma 3 27B（Google，via AI Studio）
├── run_debate.py        ← 一鍵跑 mock agent 辯論
└── run_real_debate.py   ← 一鍵跑真實 LLM 辯論
```

## 關鍵設計決策

**1. LLM 沒有 session，每輪都是 stateless API call**

記憶不靠 session keep-alive，而是每次把完整歷史塞進 context window。  
好處：任何 LLM 都可以接入，不需要特殊的 session 管理。

**2. Turn-taking 由 Server 控制（push 模型）**

Agent 不需要輪詢，Server 主動推送 `your_turn`。  
Agent 說完話 → Server 收到 → 廣播 → 推送 `your_turn` 給下一個人。

**3. llm_fn 介面統一為 `async fn(payload: dict) -> str`**

payload 包含：
```python
{
  "history":  [...],   # 最近 N 條訊息（sliding window）
  "summary":  "...",   # 舊對話摘要（Phase 2，目前為空）
  "context":  {...},   # room metadata
  "prompt":   "...",   # kickoff topic（第一輪才有）
}
```

各家 LLM 自己把 payload 轉成各自的 API 格式（OpenAI messages、Gemini contents 等）。

**4. `build_messages()` 共用函式（Llama / Kimi 共用）**

Groq 相容 OpenAI 格式，所以 Llama 和 Kimi 可以共用同一個 message builder。  
Gemma 不支援 `system_instruction`，所以另外處理，把 system prompt 當作第一輪 user 訊息。

**5. Memory：Sliding Window（Phase 1）**

```python
SLIDING_WINDOW_SIZE = 20

def context_window(self) -> list[dict]:
    return self.history[-SLIDING_WINDOW_SIZE:]
```

`room.history` 永遠保留完整記錄，只有送給 agent 的部分做 sliding window。  
Phase 2 的 `rolling_summary` 欄位已預留，介面也已定義好。

## 如何執行

**環境安裝**：
```bash
pip install websockets openai anthropic google-genai
```

**Option A：Mock agent（無需 API key）**：
```bash
python run_debate.py
```

**Option B：真實 LLM**：
```bash
export GROQ_API_KEY="..."
export GEMINI_API_KEY="..."
python run_real_debate.py
```

**Option C：分開跑（模擬不同機器）**：
```bash
# Terminal 1
python server.py

# Terminal 2
python agent_groq_llama.py

# Terminal 3
python agent_groq_kimi.py
```
