# OpenParty — Architecture Change: Agent Bridge

## 問題

MCP `wait_for_turn()` 是 blocking call（最長 120s），超過 OpenCode 的 MCP tool timeout（~60s），導致：
- Claude01 / Claude02 兩個 session 各自 block，互相等待
- `MCP error -32001: Request timed out`
- 對話無法自動進行

## 根本原因

OpenCode session 的設計是「用戶觸發一輪 → Claude 做 tool calls → 結束」，
不支援在背景持續跑 loop。MCP 不適合用來做 long-running autonomous agent。

## 解法：Claude Agent SDK Bridge

用 **Claude Agent SDK**（`claude_agent_sdk`）取代 MCP 作為 agent 的執行方式。

### 新架構

```
OpenParty Server (WebSocket)
      ↕
  bridge.py  ← 常駐 daemon，一個 bridge = 一個 agent
      ↕
  Claude Agent SDK — query(prompt, resume=session_id)
  (有完整 Read / Edit / Bash / Glob / Grep 工具能力)
```

### bridge.py 職責

1. 連接 OpenParty WebSocket，以指定 room_id / name 加入房間
2. 持續監聽，收到 `your_turn` 訊號
3. 呼叫 `claude_agent_sdk.query(prompt=context)` 觸發 Claude
4. Claude 用完整工具能力思考並產生回應
5. 把回應透過 WebSocket 送回 OpenParty Server
6. 重複步驟 2

### session 管理

- 每個 bridge instance 維護一個 `session_id`
- 使用 `resume=session_id` 讓 Claude 跨輪次保持 context
- 第一輪自動建立 session，後續輪次 resume

### 新增檔案

- `bridge.py` — Agent bridge daemon

### 不變的部分

- `server.py` — 完全不動
- `observer_cli.py` — 完全不動
- `mcp/openparty_mcp.py` — 保留，仍可用於人類手動加入房間互動

### 使用方式

```bash
# 啟動 server
.venv/bin/python server.py

# 啟動 agent（一個 terminal 一個 agent）
.venv/bin/python bridge.py --room test-001 --name Claude01

# 另一個 terminal
.venv/bin/python bridge.py --room test-001 --name Claude02

# 觀察
.venv/bin/python observer_cli.py --room test-001 --owner --name Andy
```

### 依賴

```
claude-agent-sdk   # pip install claude-agent-sdk
websockets>=12.0   # 已有
```
