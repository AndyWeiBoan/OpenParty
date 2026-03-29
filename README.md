# OpenParty

> 把多個 AI 丟進同一個聊天室，讓他們辯論你的問題。你在旁邊看，隨時可以發言。

---

## 系統架構

```
┌─────────────────────────────────────────────────────────┐
│                      server.py                          │
│              WebSocket Hub (port 8765)                  │
│                                                         │
│  ┌──────────────┐   ┌──────────────┐  ┌─────────────┐  │
│  │  Room State  │   │ Turn Manager │  │  Subprocess │  │
│  │  (history,   │   │ (round-robin,│  │  Manager    │  │
│  │   topic,     │   │  round ctrl) │  │  (agents,   │  │
│  │   agents)    │   │              │  │  opencode)  │  │
│  └──────────────┘   └──────────────┘  └─────────────┘  │
└──────────┬──────────────────┬──────────────────┬────────┘
           │ WebSocket        │ WebSocket        │ WebSocket
    ┌──────┴──────┐    ┌──────┴──────┐    ┌──────┴──────┐
    │ bridge.py   │    │ bridge.py   │    │observer_cli │
    │  (Claude)   │    │ (OpenCode)  │    │  (Human)    │
    │             │    │             │    │             │
    │claude_agent │    │ opencode    │    │ curses TUI  │
    │    _sdk     │    │ serve HTTP  │    │             │
    └─────────────┘    └─────────────┘    └─────────────┘
```

### 三個核心元件

| 元件 | 職責 |
|---|---|
| `server.py` | WebSocket hub，管理房間、輪流發言、子程序生命週期 |
| `bridge.py` | AI agent 橋接器，連接 server 並呼叫 LLM |
| `observer_cli.py` | 人類用的 TUI 介面，觀察並控制房間 |

---

## 運作流程

### 啟動
1. `server.py` 啟動時自動偵測 `opencode` 和 `claude_agent_sdk` 是否安裝
2. 如果 `opencode` 已安裝，自動執行 `opencode serve --port 4096`
3. 可用的 engine 清單傳給連線的 observer

### Observer（房主）加入
1. 執行 `observer_cli.py --owner`，以 WebSocket 連線到 server
2. Server 回傳房間狀態（成員、歷史訊息）和可用 engine 清單
3. Observer 輸入第一句話 → 設定 **topic**，討論開始

### 加入 AI Agent（`/add-agent`）
1. Observer 選 `/add-agent`，從選單選擇 engine 和 model
2. Server 收到 `spawn_agent` 請求，在本地 spawn `bridge.py` 子程序
3. `bridge.py` 以 WebSocket 連回 server，加入房間
4. Server 廣播 `agent_joined` 給所有 observer

### 發言輪流機制（Round System）
```
Owner 發話
    └─> 清空 round_speakers（新一輪開始）
        └─> 通知第一個 agent：your_turn
            └─> agent 回覆 → 廣播給所有人
                └─> 通知下一個未發言的 agent
                    └─> 所有 agent 都說過一次
                        └─> [server] All agents responded. Waiting for your next message.
```

- 每輪每個 agent **只說一次**，不會無限迴圈
- Owner 的下一句話重置輪次、觸發新一輪

### Model 偵測（Claude 版本）
- `bridge.py` 第一次收到 claude 回覆時，從 `AssistantMessage.model` 取得實際版本號
- 送 `update_model` 給 server，廣播給 observer
- TUI 顯示從 `claude (claude-sonnet)` 更新為 `claude (claude-sonnet-4-6)`

---

## 目前功能

### Observer TUI（`observer_cli.py`）

| 功能 | 說明 |
|---|---|
| 聊天室 | 即時顯示所有 agent 發言，含名稱、model 版本、回應時間 |
| 滾動歷史 | Page Up / Page Down 捲動，End 跳回最新 |
| Scrollbar | 右側顯示捲動位置，滑鼠點擊跳至對應位置 |
| 滑鼠滾輪 | 上下滾動聊天歷史 |
| 中文輸入 | 完整支援 CJK 字元輸入與顯示 |
| 指令補全 | 輸入 `/` 自動顯示可用指令清單 |
| `/add-agent` | 選單選擇 engine + model，server 自動 spawn agent |
| `/kick` | 選單顯示目前成員，選取後踢除，廣播系統訊息 |
| `/kick-all` | 一次踢除所有 agent |
| `/leave` | 離開房間（agent 繼續運作，不受影響） |
| 唯一名稱 | 同 model 加入兩次自動編號（`claude`, `claude-2`, ...）|
| `@mention` | 輸入 `@` 彈出成員選單補全，tag 以青色粗體顯示 |

### Agent 行為（`bridge.py`）

| 功能 | 說明 |
|---|---|
| 雙 engine | `--engine claude`（claude_agent_sdk）或 `--engine opencode` |
| 獨立立場 prompt | 內建 system prompt 要求 agent 獨立思考、主動指出邏輯漏洞 |
| 錯誤處理 | provider 回傳致命錯誤（rate limit 等）時優雅離開，不重試 |
| 版本回報 | 首次回覆後自動回報實際使用的 model 版本給 server |

### Server（`server.py`）

| 功能 | 說明 |
|---|---|
| 多房間 | 以 `room_id` 隔離，支援同時多個房間 |
| 生命週期管理 | server 關閉時自動終止所有 agent 子程序和 opencode serve |
| Engine 偵測 | 啟動時自動偵測 opencode / claude 是否可用 |
| 唯一 Owner | 同一房間只允許一個 owner，新連線自動踢掉舊的 |
| 踢除功能 | Owner 可透過 `/kick` 關閉指定 agent 的 WebSocket |

---

## 快速開始

```bash
# 安裝依賴
pip install -r requirements.txt

# 啟動 server（自動偵測並啟動 opencode serve）
python server.py

# 以房主身份進入房間
python observer_cli.py --room my-room --owner --name Andy
```

進入 TUI 後：
1. 輸入第一句話設定討論主題
2. 用 `/add-agent` 加入 AI 成員
3. 繼續對話，每輪所有 agent 各回覆一次

---

## 遠端加入（Remote Agent）

不在同一台機器的人，可以用 `openparty-join` 精靈加入房間。

### 前提條件

**Server 端（主機）**
- `server.py` 正在跑
- 防火牆開放 port `8765`
- 知道主機 IP（區網：`192.168.x.x`，外網：公網 IP 或 ngrok）

**遠端用戶**
- 至少安裝一個 engine：
  - Claude → [安裝 claude CLI](https://docs.anthropic.com/en/docs/claude-code)（需要 Max/Pro 訂閱）
  - OpenCode → [安裝 opencode](https://opencode.ai)（有免費 model 可用）

### 方式一：直接跑精靈（有 Python）

```bash
pip install aiohttp websockets claude-agent-sdk
python openparty_join.py
```

精靈會自動偵測已安裝的 engine，引導選擇 model 後加入房間。

### 方式二：下載 Binary（免安裝 Python）

從 [GitHub Releases](../../releases) 下載對應平台的 binary：

| 平台 | 檔案 |
|---|---|
| macOS Apple Silicon | `openparty-join-macos-arm64` |
| macOS Intel | `openparty-join-macos-x86_64` |
| Linux x86_64 | `openparty-join-linux-x86_64` |
| Windows | `openparty-join-windows-x86_64.exe` |

```bash
chmod +x openparty-join-macos-arm64
./openparty-join-macos-arm64
```

### 精靈流程

```
輸入 server URL、room ID、名稱
    └─> 自動偵測 claude CLI / opencode
        └─> 選擇 engine
            └─> (opencode) 選擇 model
                  free model     → 不需要 API key
                  provider model → 需要登入 / API key
            └─> 確認後加入房間
```

### 手動方式（進階）

```bash
# 直接指定參數，不走精靈
python bridge.py \
  --room test-001 \
  --name RemoteClaude \
  --engine claude \
  --server ws://192.168.1.x:8765
```

### 外網連線（ngrok）

沒有固定 IP 時，用 ngrok 暴露 port：

```bash
# 主機端
ngrok tcp 8765
# 取得類似 tcp://0.tcp.ngrok.io:12345

# 遠端用
./openparty-join
# Server URL: ws://0.tcp.ngrok.io:12345
```

---

### Build Binary（自行編譯）

```bash
# 本機 build 當前平台
bash build.sh

# 跨平台 build（macOS / Linux / Windows）
# → push 一個 tag，GitHub Actions 自動 build 並建立 Release
git tag v1.0.0 && git push --tags
```

---

## 檔案結構

```
server.py            WebSocket hub + 子程序管理
bridge.py            AI agent 橋接器（claude / opencode）
observer_cli.py      人類 TUI 介面
openparty_join.py    遠端加入精靈
build.sh             本機 build binary 腳本
.github/workflows/
  build.yml          GitHub Actions 跨平台 build
mcp/
  openparty_mcp.py   MCP server（供外部工具整合用）
requirements.txt
opencode.json        opencode 設定檔
```

---

## WebSocket 訊息協議

```jsonc
// Agent / Observer 加入
{ "type": "join", "room_id": "...", "name": "Claude01", "model": "claude-sonnet" }

// Server 通知輪到你
{ "type": "your_turn", "history": [...], "context": { "topic": "...", "participants": [...] } }

// Agent / Observer 發訊息
{ "type": "message", "content": "..." }

// Observer 要求 server spawn agent
{ "type": "spawn_agent", "name": "claude", "model": "claude/default", "engine": "claude" }

// Observer 踢除成員
{ "type": "kick_agent", "agent_name": "claude" }

// Agent 回報實際 model 版本
{ "type": "update_model", "model": "claude-sonnet-4-6" }
```
