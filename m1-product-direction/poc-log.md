# M1 PoC Log — 問題日誌

> 所有三個課題的問題日誌集中在這裡。

---

## 2026/03/28 — 【嚴重錯誤】調研時找到錯誤的 OpenCode repo（已修正）

**症狀**：初版研究搜尋「OpenCode」找到 github.com/opencode-ai/opencode（已 archived），誤以為這就是 ROADMAP 中提到的 OpenCode，並基於這個錯誤假設建立了完整的調研結論（「OpenCode 已死，繼承者是 Crush」）。

**實際情況**：ROADMAP 提到的 OpenCode 是 **github.com/anomalyco/opencode**（132k stars，TypeScript，v1.3.3，opencode.ai），由 SST 作者們開發，完全活躍。與 opencode-ai/opencode 是完全不同的兩個專案，只是名字相似。

**原因**：調研開始前沒有確認正確 repo URL，直接搜尋關鍵字拿到錯誤結果，且沒有交叉驗證。

**解法**：用正確 URL 重新調研，課題 1 結論完整重寫。課題 2 和 3 不受影響。

**影響**：
- 課題 1 調研結論全部作廢並重做
- MCP Server PoC（openparty_mcp.py）本身仍有效，MCP 是通用標準
- 新發現 OpenCode HTTP Server API 是額外的高價值整合路線

**教訓**：
- 調研特定工具前，**先從 ROADMAP/需求文件拿到確切的 repo URL**，不要靠關鍵字搜尋
- 搜尋到結果後，**確認 stars 數量、語言、維護狀態**，與預期相符才繼續
- 本次正確做法應該是：ROADMAP 說「OpenCode 用戶」→ 問清楚是哪個 OpenCode → 再開始調研

---

## 2026/03/28 — websockets 14+ API 破壞性變更

**症狀**：執行測試時報錯：`AttributeError: 'ClientConnection' object has no attribute 'closed'. Did you mean: 'close'?`

**原因**：websockets 14.x 改了 API：
- 舊版（11-13）：`ws.closed` 是布林值屬性
- 新版（14+）：`ws.closed` 被移除，改用 `ws.close_code is None`（None = 仍連線）

**解法**：建立 `_ws_is_open(ws)` 函數，用 try/except 同時支援兩個版本：
```python
def _ws_is_open(ws) -> bool:
    if ws is None:
        return False
    try:
        return ws.close_code is None
    except AttributeError:
        try:
            return not ws.closed
        except AttributeError:
            return False
```

**教訓**：
- 使用第三方函式庫前先確認版本 API
- websockets 版本敏感，requirements.txt 應該 pin 版本：`websockets>=13.0,<15.0`
- MCP 測試要在 CI 裡用固定版本跑

---

## 2026/03/28 — MCP FastMCP 的 type hint 要小心

**症狀**：`Optional[websockets.WebSocketClientProtocol]` 的 type hint 在 websockets 14+ 無效（class 名稱改了）。

**原因**：websockets 14+ 將 `WebSocketClientProtocol` 改名為 `ClientConnection`。

**解法**：改用 `Optional[Any]` 或直接不加 type hint：`_ws = None  # websockets.ClientConnection instance`

**教訓**：
- PoC 程式碼不必強求 type hint 完整性
- 但正式 SDK 應該要正確 type hint，考慮用 `websockets.WebSocketCommonProtocol` 等抽象類

---

## 2026/03/28 — check_your_turn 的 race condition

**症狀**：在測試中，`check_your_turn()` 有時在 `_your_turn_event.set()` 之前就被呼叫，造成 2 秒等待。

**原因**：`_ws_listener` 在背景 task 跑，`join_room` 完成後 1.5 秒才呼叫 `check_your_turn()`，有時 listener 還沒收到 your_turn 訊號。

**解法**：`check_your_turn()` 先檢查 `_your_turn_event.is_set()`，如果已設定直接返回，不用等。

**教訓**：
- async 事件驅動設計要注意 race condition
- 使用 `asyncio.Event` 是正確模式，但調用方要同時處理「已設定」和「未設定」兩種狀態
- 生產版本可以考慮 `asyncio.Queue` 取代 Event，支援多個 your_turn 訊號

---

## 2026/03/28 — 發現缺失評估：Claude Code Plugin 可以更早做

**發現**：Claude Code Plugin 系統比預期完整。`.claude/skills/` 路徑**同時**被 Claude Code 和 Crush 支援，做一個 Agent Skill 就能覆蓋兩個工具。

**影響**：M2 的「TypeScript 版本」優先序可以降低，因為：
- Claude Code Plugin 用 Python MCP server 就能覆蓋 Claude Code 使用者
- Crush Agent Skills 用同樣的 SKILL.md 也能覆蓋 Crush 使用者

**決定**：M2 的任務排序調整，把「Claude Code Plugin」排到前面，TypeScript SDK 排到後面。

---

## 2026/03/28 — MCP "your_turn" 機制的使用者體驗問題

**症狀**：用 MCP 工具時，AI 需要主動呼叫 `check_your_turn()`，而不是自動感知輪到它。

**原因**：MCP 是 request-response 協定，不能「推送」訊號給 AI。AI 只能主動呼叫工具，而不是被動接收事件。

**影響**：MCP 方式需要 AI 理解「我加入後要輪流呼叫 check_your_turn 和 send_message」。這需要好的 system prompt 或 SKILL.md 來引導。

**解法**：
- 短期：在 MCP server 的 `instructions` 欄位（FastMCP 支援）說清楚用法順序
- 中期：做 Claude Code Plugin，在 SKILL.md 裡寫完整的工作流程說明
- 長期：考慮讓 server 支援 HTTP SSE，讓 MCP 也能 push 事件（MCP 有 notifications 支援）

**教訓**：
- MCP 的 request-response 模型不適合 event-driven 場景
- 但可以透過好的工具說明和 SKILL.md 讓 AI 自動形成 polling 行為
- 長期要考慮 MCP notifications 或 Channels 機制（Claude Code 有支援）

---

## 2026/03/28 — 課題 2：發現「AI Pair Review」是最強的使用場景

**發現**：調研 AutoGen、CrewAI、A2A 社群時，最高熱度的討論是「不同 process 的 agent 怎麼互通」。但觀察這些討論的具體需求，發現最強的商業價值不是「企業 microservice 通信」，而是「Claude Code 用戶想讓多個 AI 互相 review」。

**為什麼重要**：企業用戶的決策路徑長、需要安全審查；個人開發者的決策路徑短，可以直接試用。早期 MVP 應優先針對個人開發者。

**決定**：定義「AI Pair Review」為 OpenParty 的主要使用場景，用這個場景驅動 M2 的功能設計。

---

## 2026/03/28 — 課題 3：MCP 讓 TypeScript 優先序問題變得不那麼緊迫

**發現**：原本擔心不做 TypeScript SDK 就無法覆蓋 Claude Code 用戶。但課題 1 的研究發現 Claude Code Plugin 可以 bundle Python MCP server。這意味著用 Python 做 MCP server，再包成 Claude Code Plugin，就能讓 Claude Code 用戶（不管是 TS 開發者還是 Python 開發者）都能使用。

**影響**：M2 不需要做 TypeScript SDK，節省 2-3 週，可以把時間用在更重要的事（Observer 模式、正式化 SDK/MCP）。

**教訓**：選型決策要考慮整合路徑，不只是目標用戶的語言偏好。

---

## 2026/03/28 — M1 總結：三個課題研究完畢

**所有三個 M1 課題研究完成：**
1. ✅ Plugin 機制：MCP 是主要整合方向，Claude Code Plugin 是深度整合路線
2. ✅ Pain point：AI Pair Review 是最強場景，目標用戶是 Claude Code Power User
3. ✅ SDK 決策：Python 優先，TypeScript 等真實用戶反饋

**下一步**：更新 ROADMAP.md，然後進入 M2 設計。
