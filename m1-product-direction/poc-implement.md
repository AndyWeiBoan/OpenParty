# M1 PoC Implement — 實作記錄

> 本文件記錄 M1 各課題的實作過程。

---

## 課題 1：OpenCode / Claude Code Plugin 機制研究 + MCP Server PoC

### 實作思路

調研發現三個關鍵事實：
1. **opencode-ai/opencode 已 archived（2025/09/18）**，已轉為 Crush（charmbracelet/crush）
2. **Claude Code 有完整 Plugin 系統**（v1.0.33+）：Skills、Hooks、Agents、MCP
3. **MCP 是跨工具的通用整合機制**，Claude Code、Crush、Cursor 都支援

決定驗證最核心的問題：**用 MCP Server 讓 AI 加入 OpenParty Room，技術上可行嗎？**

實作了 `openparty_mcp.py`，它：
- 用 `FastMCP` 框架建立 MCP server（stdio transport）
- 在背景維護 WebSocket 連線到 OpenParty Room Server
- 提供 6 個工具讓 AI 呼叫

### 檔案結構

```
m1-product-direction/
├── poc-design.md        ← 三個研究課題的設計文件
├── poc-implement.md     ← 本文件
├── poc-result.md        ← 結果記錄
├── poc-log.md           ← 問題日誌
└── src/
    ├── openparty_mcp.py         ← MCP Server 主程式
    └── test_mcp_integration.py  ← 端對端測試
```

### 關鍵設計決策

**1. 使用 FastMCP 而非低層 MCP Server API**
- FastMCP 用 Python type hints 和 docstring 自動生成工具定義
- 比手寫 JSON schema 快 10 倍
- 文件字串直接成為 AI 看到的工具描述（很重要！描述越清楚 AI 越會用）

**2. 全域狀態管理（而非 class instance）**
- MCP server 在同一個 process 裡只會有一個 session
- 全域變數比 class 更簡單，PoC 夠用
- 生產版本可以換成 context-based 設計

**3. `_ws_is_open()` 相容函數**
- websockets 14+ 改了 API，`_ws.closed` 被移除，要用 `_ws.close_code is None`
- 做了相容函數同時支援兩個版本

**4. 背景 asyncio.Task 監聽 WebSocket**
- MCP 的工具函數是 async，但是是按需呼叫的（不是 long-running loop）
- 需要一個背景 task 持續接收 WebSocket 訊息，放到 `_pending_messages` 暫存
- `check_your_turn()` 等 `asyncio.Event`，而不是 polling

**5. 訊息佇列設計**
- `_pending_messages`：存放一般廣播訊息（message、joined、agent_left 等）
- `_your_turn_event`：專門的 asyncio.Event，收到 your_turn 就 set()
- `_your_turn_payload`：存最新的 your_turn payload（history + context）

### 如何執行

**前置條件：**
```bash
pip install "mcp[cli]" websockets
```

**方法一：執行測試**
```bash
cd /path/to/OpenParty
python m1-product-direction/src/test_mcp_integration.py
```

**方法二：在 Claude Code 中使用**
```bash
# 在 Claude Code 中設定 MCP
claude mcp add --transport stdio openparty -- python /path/to/m1-product-direction/src/openparty_mcp.py

# 然後在 Claude Code session 中說：
# "Join the OpenParty room 'debate-001' as Claude and discuss the AI debate topic"
```

**方法三：在 Crush 中使用（crush.json）**
```json
{
  "mcp": {
    "openparty": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/m1-product-direction/src/openparty_mcp.py"]
    }
  }
}
```

**方法四：啟動 server + 跑 PoC**
```bash
# 終端機 1：啟動 Room Server
python server.py

# 終端機 2：啟動 MCP server（等待 stdio 輸入）
python m1-product-direction/src/openparty_mcp.py

# 終端機 3：在 Claude Code 裡使用
claude  # 啟動 Claude Code，它會自動連接 MCP server
```

---

## 課題 2 & 3 實作

> 待完成（見 poc-design.md 的後續章節）
