# M2 PoC Log — 問題日誌

---

## 2026/03/29 — cyril port 11434 間歇性不可達

**症狀**：`curl http://172.16.64.147:11434/api/tags` 有時 timeout，有時正常。`ping` 通但 port 不通。

**原因**：cyril 的 Ollama 服務在我開始測試時剛好沒在跑（或正在 loading）。後來服務回來就正常了。

**解法**：等服務回來，確認前先用 `curl /api/tags` 測試連通性再繼續。

**教訓**：跨機器測試前加一步 health check，不要假設遠端服務永遠可用。

---

## 2026/03/29 — Qwen3 thinking model：max_tokens 截斷導致 content 空白

**症狀**：`qwen3:14b` 和 `qwen3.5:latest` 的 API response `content` 是空字串，`finish_reason=length`。

**原因**：Qwen3 系列是 thinking model，會先輸出大量的 `<think>...</think>` 推理 tokens，把 `max_tokens` budget 耗盡，還沒輸出實際回答就被截斷了。

**解法**：
- 主要解法：`max_tokens=0`（不傳此參數給 API），讓模型自己決定長度
- 次要解法：加 `_strip_thinking()` regex，過濾偶爾洩漏進 content 的 thinking 文字

**Ollama 的 response 結構**（Qwen3）：
```json
{
  "message": {
    "role": "assistant",
    "content": "actual answer here",   // 可能空，也可能含 thinking 文字
    "reasoning": "<think>...</think>"  // thinking content 在這
  }
}
```

**教訓**：
- 對 thinking model 不能設 `max_tokens`，或要設得很大（> 2000）
- SDK 需要 fallback 邏輯：content 空時看 `reasoning` / `reasoning_content`
- MAGI 也遇到同樣問題，它處理 `reasoning_content`，我們加了 `reasoning`

---

## 2026/03/29 — qwen3.5:latest 某些 prompt 嚴重 timeout（> 60s）

**症狀**：特定 prompt（較長的 multi-turn context）送給 `qwen3.5:latest` 超過 60 秒無回應。

**原因**：qwen3.5 的 thinking mode 在複雜 context 下思考時間極長，超出 openai SDK 預設 timeout。

**解法**：改用 `gemma3:12b` 作為 cyril 的主力模型。gemma3 系列沒有 thinking mode，回應穩定在 4-6 秒。

**cyril 模型推薦順序（Room 對話場景）**：
1. `gemma3:12b` — 穩定，~5s/turn，適合即時對話
2. `gemma3:27b` — 更強，~10-15s/turn，待測
3. `qwen3-coder:30b` — 最強但最慢，適合 code review 場景（待測）
4. `qwen3.5:latest` — 避免用於 Room（thinking mode 不穩定）

---

## 2026/03/29 — Observer 收到 turn_start 但 agent 已離開的競爭條件

**症狀**：Observer event 序列末尾出現 `['turn_start', 'agent_left', 'agent_left']`——agent 離開時剛好在 turn_start 之後，turn_end 沒送出來。

**原因**：agent 達到 `max_turns` 後送 `leave`，server 的 finally block 廣播 `agent_left`，但 `turn_end` 是在收到 `message` 後才送的，leave 不會觸發 turn_end。

**影響**：Observer UI 會顯示「某人在思考...」然後看到他離開，沒有 done 狀態。視覺上略奇怪，但邏輯正確。

**解法（M2 後期）**：在 `leave` 處理時若 `turn_started_at > 0` 就送一個 `turn_cancelled` event，讓 Observer 知道這個 turn 被取消了。

---

## 2026/03/29 — server.py deprecation warning：WebSocketServerProtocol

**症狀**：啟動 server 時出現 `DeprecationWarning: websockets.server.WebSocketServerProtocol is deprecated`

**原因**：websockets 14+ 改用新的型別系統，`WebSocketServerProtocol` 在 14+ 已 deprecated。

**影響**：僅 warning，功能正常。

**解法（M2 後期）**：升級 websockets 到 14+，改用 `websockets.ServerConnection` 型別。

