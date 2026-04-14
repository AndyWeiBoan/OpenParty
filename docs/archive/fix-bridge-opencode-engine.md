# Fix: Bridge.py OpenCode Engine 無限執行問題

## 來龍去脈

### 症狀
OpenParty 使用 opencode engine 時，有時候任務會「跑得沒完沒了」，看起來像無限迴圈。

### 背景：Claude SDK 的前車之鑑
先前調查 Claude SDK 不穩定的 root cause，發現是因為設定了 `max_turns=8`，導致複雜任務超過八輪思考後被 CLI 強制中斷。opencode engine 的症狀相反——不是過早中斷，而是跑不停，因此需要另行調查。

### Root Cause 調查結果

#### 1. OpenCode Engine 的主迴圈設計
`prompt.ts`（line 1344）的主迴圈是 `while (true)`，唯一的 break 條件是：
- `lastAssistant.finish` 不是 `"tool-calls"` 且沒有待處理 tool calls
- `result === "stop"` 從 processor 返回
- compaction 返回 `"stop"`

只要 LLM 持續回傳 `finish: "tool-calls"`，迴圈就永遠不會停止。`DOOM_LOOP_THRESHOLD = 3` 的保護機制也只有在同一個 tool、同樣 input 連續三次才觸發，LLM 只要稍微換個 tool 或參數就能繞過。

#### 2. Bridge.py 選錯了 Endpoint
OpenCode serve 提供**兩個**提交 prompt 的 endpoint，但 bridge.py 用錯了：

| Endpoint | 行為 | bridge.py 現況 |
|----------|------|----------------|
| `POST /:sessionID/message` | **阻塞**：等 engine 的 `while(true)` loop 跑完才返回完整結果 | ✅ 正在使用（錯誤選擇） |
| `POST /:sessionID/prompt_async` | **非阻塞**：立刻返回 204，engine 在背景執行 | ❌ 未使用（正確選擇） |

Bridge.py 在 `_post_message()` 對 `/message` 設定了 `timeout=aiohttp.ClientTimeout(total=120, sock_read=120)`，這代表整個 HTTP 連線最多只能掛 120 秒。任何需要超過 120 秒的 opencode 任務都會被強制砍掉，回傳 `"(opencode error: ...)"` 錯誤字串。

#### 3. SSE 架構職責混亂
目前的雙 task 架構：
```python
post_task = asyncio.create_task(oc._post_message(body))   # 阻塞式等待最終答案
sse_task = asyncio.create_task(self._opencode_sse_listener(post_task))  # 廣播中間過程
```

- POST task 掛著等 engine 跑完（阻塞），`asyncio.create_task` 只是讓兩個 task 「並行等待」，底層 HTTP 連線仍然是阻塞的
- SSE task 只用於廣播中間思考過程給觀察者，不負責取得最終答案
- SSE 收到 `post_task` 作為 `done_task` 參數，僅用於在 POST 完成後啟動 30 秒 idle timeout（`_SSE_IDLE_AFTER_DONE = 30.0`），避免 SSE stream 不正常關閉時永遠卡住

這個設計導致：
- **120 秒硬上限**：任何超過 120 秒的 opencode 任務必然失敗
- **Fallback 無效**：timeout 後只回傳 error string，沒有嘗試取得部分結果
- **可能重試風暴**：上層若把 error string 視為重試信號，舊的 engine 還在跑、新任務又開始，形成「跑不完」的假象

#### 4. 正確的 Fallback Endpoint 未被使用
OpenCode serve 提供 `GET /:sessionID/message`（session.ts line 549）可隨時查詢 session 的所有訊息。Session 是持久化的，訊息存在 SQLite database，即使 HTTP 連線中斷，engine 跑完的結果仍然可以查詢。Bridge.py 完全沒有利用這個機制。

---

## 解決方案

### 正確架構：`prompt_async` + SSE + GET fallback

```
1. POST /session/{id}/prompt_async   → 立刻返回 204，engine 在背景啟動
2. 訂閱 SSE /event                   → 監聽 message.updated 事件取得最終結果
3. SSE timeout 或斷線後              → GET /session/{id}/message 查詢已存的結果
```

### 新流程說明

```
bridge.py                          opencode serve
    |
    +---> POST /prompt_async -------> 立刻 204（engine 在背景跑）
    |
    +---> GET /event (SSE) ---------> 開始推送事件流
    |         |
    |         | message.part.updated（reasoning, tool calls...）
    |         | message.updated（每次 assistant message 狀態更新）
    |         | message.updated（finish != "tool-calls"，任務完成）
    |         |
    |     收到最終 message.updated（含 finish 狀態）
    |
    +---> 從 SSE event 提取最終文字結果
    |
    [若 SSE timeout 310 秒仍未收到完成事件]
    |
    +---> GET /session/{id}/message --> 查詢已存的 messages，提取最新 assistant 訊息
```

### 關鍵改動

1. **`OpenCodeClient._post_message()` → `OpenCodeClient.submit_async()`**
   - 改呼叫 `POST /session/{id}/prompt_async`
   - 不再需要等待回應，立刻返回

2. **`_opencode_sse_listener()` 升級為主要結果管道**
   - 監聽 `message.updated` 事件
   - 當事件中 `info.finish` 存在且不是 `"tool-calls"` 時，視為任務完成
   - 從事件中提取最終文字結果

3. **新增 `_get_session_messages()` fallback**
   - 呼叫 `GET /session/{id}/message`
   - 取最新一筆 role=assistant 且有 finish 的訊息
   - 提取其中的 text parts 作為答案

4. **`_call_opencode_with_thinking()` 重構**
   - 移除 `post_task`（不再需要阻塞式 POST）
   - SSE task 負責監聽結果，310 秒 timeout
   - SSE timeout 後呼叫 GET fallback
   - 若 GET 也沒有結果，才回傳 error string

---

## TODO List

> Code review（claude-sonne + claude-sonne-2 雙人 review）完成於實作後，已標記各項完成狀態。

- [x] **重構 `OpenCodeClient`**
  - [x] 新增 `submit_async()` 方法，呼叫 `POST /:sessionID/prompt_async`（立刻返回 204，timeout=10s）
  - [x] 新增 `get_messages()` 方法，呼叫 `GET /:sessionID/message`，過濾最後一筆 role=assistant 且 finish 非 tool-calls/error 的訊息
  - [x] `_build_body()` 相容 `prompt_async`（body schema 與 `/message` 相同）

- [x] **重構 `_call_opencode_with_thinking()`**
  - [x] 移除 `post_task = asyncio.create_task(oc._post_message(body))`
  - [x] 改為呼叫 `submit_async()` 後立刻啟動 SSE task
  - [x] SSE 310 秒 timeout 後呼叫 `get_messages()` fallback
  - [x] 若 GET fallback 取到結果，正常返回；若無結果，回傳 error string
  - [x] SSE 自然完成時直接使用 `sse_task.result()` 回傳的文字，跳過 GET（消除對 opencode DB 寫入順序的隱性依賴）
  - [x] 新增 `_sse_completed_naturally` flag，區分 SSE 正常完成與 timeout/error 路徑 log

- [x] **升級 `_opencode_sse_listener()`**
  - [x] 移除 `done_task` 參數，改用 local `_engine_done` flag
  - [x] 監聽 `message.updated` 事件，偵測 `info.finish` 存在且非 `"tool-calls"` / `"error"` 時設定 `_engine_done = True`
  - [x] engine 完成後若 10 秒無新事件（`_SSE_IDLE_AFTER_DONE`）自動退出 SSE loop
  - [x] SSE 整體 timeout 由 caller 的 `asyncio.wait_for` 控制（310s）
  - [x] 回傳型別從 `None` 改為 `str | None`，自然完成時攜帶結果文字給 caller
  - [x] `sock_read` 從 60s 改為 `None`，避免 LLM 長時間思考被誤判為連線逾時

- [x] **修正 Bug（code review 發現）**
  - [x] `finally` 中 `_flush_reasoning()` 改用 `asyncio.shield()` + `except BaseException`，防止 cancelled task context 下 `CancelledError` 再次中斷 flush（`except Exception` 無法攔截 `BaseException` 的子類）
  - [x] `finish` 條件改為使用 `_FINISH_BLOCKED` frozenset（`{"tool-calls", "error"}`），避免 engine 錯誤結果被靜默當作正常回覆回傳
  - [x] SSE 310s timeout 後新增 `abort_session()` 呼叫（`POST /:sessionID/abort`），確保背景 engine 停止後再執行 GET fallback，避免 engine 堆積與並行執行
  - [x] SSE Exception 分支也加上 `abort_session()`（不只 TimeoutError）
  - [x] SSE 自然完成與 SSE timeout/GET 路徑改用不同 log prefix（`[SSE path]` / `[GET path]`），方便 debug

- [x] **移除或調整舊有機制**
  - [x] 兩處舊的 `self._opencode.call(prompt_str)` 備援路徑已統一改為 `_call_opencode_with_thinking()`（ws=None 時 thinking 廣播靜默略過，結果仍透過 prompt_async + GET 取得）
  - [x] `_post_message()` / `call()` 方法保留為 dead code（不再被呼叫），可日後清理
  - [x] Session reuse 邏輯正確：session_id 跨次呼叫持久保留，abort 不銷毀 session

- [ ] **測試**
  - [ ] 撰寫 unit test 模擬 opencode serve 的 `prompt_async` + SSE 回應
  - [ ] 測試 SSE 正常完成路徑
  - [ ] 測試 SSE 310 秒 timeout 後 GET fallback 路徑
  - [ ] 測試 GET fallback 無結果時的 error 路徑
  - [ ] 在 OpenParty 環境中手動測試複雜任務（預期需要 > 120 秒的任務）

---

## 驗收標準

1. **基本功能**
   - [ ] 提交 prompt 後 bridge.py 立刻返回（不再阻塞 120 秒）
   - [ ] Engine 正常完成時，bridge.py 能正確從 SSE 事件中提取最終文字答案
   - [ ] 複雜任務（預期執行 2-5 分鐘）能正常完成並取得結果，不被 120 秒 timeout 砍掉

2. **容錯機制**
   - [ ] SSE 連線中斷後，GET fallback 能成功取得已完成的 engine 結果
   - [ ] 310 秒 SSE timeout 後，GET fallback 正確查詢 session messages
   - [ ] 若 engine 在 310 秒內尚未完成，GET fallback 能取得部分結果或明確回傳「尚未完成」狀態

3. **不得退化**
   - [ ] 中間思考過程（reasoning, tool calls）仍然透過 SSE 廣播給 OpenParty 觀察者
   - [ ] Agent thinking 事件仍然正確顯示在 TUI
   - [ ] Session ID 跨次呼叫保持一致，不因任何錯誤而重建 session（除非明確需要）

4. **無重複提交**
   - [ ] 任何情況下，同一個 user prompt 只提交一次給 opencode engine
   - [ ] 確認 engine timeout 後上層不會自動重試（或重試前先 abort 舊任務）

5. **可觀測性**
   - [ ] Log 清楚記錄使用的 endpoint、SSE 事件接收狀況、fallback 觸發原因
   - [ ] Error 情況下 log 包含 sessionID 以便 debug
