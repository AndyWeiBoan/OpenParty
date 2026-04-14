# TUI 端支援 Spawn Agent Process

## 背景

目前 agent 的 spawn 流程是：

```
TUI → (WS "spawn_agent") → Server._spawn_agent_process() → subprocess(bridge.py)
    ← (WS "spawn_result") ←
```

Server 同時負責 WebSocket hub 路由 **和** subprocess 生命週期管理，違反單一職責。
更關鍵的是，當 remote agent 從另一台機器連入時，server 根本無法替遠端 spawn——
這代表 spawn 本來就不該是 server 的責任。

## 目標

將 `spawn agent process` 的職責從 `server.py` 移至 `openparty_tui.py`（client 側），
使 server 成為純粹的 WebSocket message hub。

## 具體做法

### Phase 1: TUI 端接管 spawn（核心改動）

**openparty_tui.py 改動：**

1. **搬移 `_spawn_agent_process()` 邏輯到 TUI**
   - 從 `server.py` 的 `_spawn_agent_process()`（L320-374）提取核心邏輯
   - **cmd 組裝抽成 module-level helper function `_build_bridge_cmd()`**，方便單元測試直接呼叫，不需要實例化 App
   - 在 TUI App class 新增 `_spawn_agent_process(name, model_id, engine)` 方法，內部呼叫 `_build_bridge_cmd()` 再 `asyncio.create_subprocess_exec()`
   - 組裝 cmd 時需帶入 server 的 `ws://host:port` 作為 bridge 連回的目標
   - stdout/stderr 導向 `agent_{name}.log`

2. **TUI 端管理 `spawned_procs` 列表**
   - 在 App 上新增 `self.spawned_procs: list[asyncio.subprocess.Process]`
   - spawn 成功後 append，agent 斷線或結束後移除
   - 在 `on_unmount()` 中統一 `terminate()` 所有子進程
   - **額外註冊 `atexit.register()` 和 `signal.signal(SIGINT, ...)` 作為 Ctrl-C / 異常退出的 fallback 清理路徑**——Textual 的 Ctrl-C 預設行為不一定走 `on_unmount`，SIGINT 可能直接殺掉主程序導致 bridge 子進程成為孤兒

3. **修改 `/add-agent` 流程（L2463-2506）**
   - 原本：選完 model 後發送 `spawn_agent` WS 訊息給 server
   - 改為：選完 model 後直接呼叫本地 `_spawn_agent_process()`
   - 成功/失敗直接在 TUI 本地顯示，不再等 `spawn_result` 回覆

4. **Engine 可用性檢查移至 TUI**
   - 將 `server.py` startup() 中的 opencode/claude 偵測邏輯搬到 TUI
   - **每次 `/add-agent` 執行時即時做本地 health check**（不在 TUI 啟動時靜態快取，避免環境變動導致誤判）
   - `available_engines` 由 TUI 自行維護，不再從 server 的 `joined` 訊息取得

### Phase 2: Server 端清理

**server.py 改動：**

1. **移除 `_spawn_agent_process()` 方法**（L320-374）
2. **移除 `spawn_agent` WS 訊息 handler**（L702-739）
3. **移除 `spawned_procs` 屬性及 `shutdown()` 中的 proc 清理邏輯**（L265, L376-391）
4. **移除 `startup()` 中的 engine 偵測邏輯**（L272-318）
   - opencode health check、auto-start opencode serve、claude SDK 偵測全部移除
   - `available_engines` 屬性移除
5. **`joined` 訊息中不再帶 `available_engines`**（L686）

### Phase 3: 協議統一

- 不管是 TUI spawn 的 local bridge 還是 remote agent 自行連入，
  進入 room 的握手流程（`join` → `joined`）保持統一，server 不區分來源
- TUI 透過 server 廣播的 `participant_joined` 事件得知新 agent 已上線，
  作為 spawn 成功的確認信號（取代原本的 `spawn_result`）

## 測試計畫

### 單元測試（`tests/test_tui_spawn_agent.py`）

不啟動真實 server 或 subprocess，用 mock 隔離驗證各函式的邏輯正確性。

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# ── 1. cmd 組裝測試 ──

class TestBuildBridgeCommand:
    """驗證 _build_bridge_cmd() 產出正確的 CLI 參數列表"""

    def test_opencode_engine_cmd(self):
        """opencode engine 應帶 --engine opencode --opencode-model --model"""
        cmd = _build_bridge_cmd(
            name="agent-1", model_id="anthropic/claude-sonnet-4-6",
            engine="opencode", server_url="ws://localhost:8765", room="test-room"
        )
        assert "--engine" in cmd
        assert "opencode" in cmd
        assert "--opencode-model" in cmd

    def test_claude_engine_strips_prefix(self):
        """claude engine 應去掉 'claude/' prefix 再傳給 --model"""
        cmd = _build_bridge_cmd(
            name="agent-1", model_id="claude/claude-sonnet-4-6",
            engine="claude", server_url="ws://localhost:8765", room="test-room"
        )
        assert "claude-sonnet-4-6" in cmd  # 無 "claude/" prefix
        assert "claude/claude-sonnet-4-6" not in cmd

    def test_cmd_includes_room_and_name(self):
        """cmd 必須包含 --room 和 --name"""
        cmd = _build_bridge_cmd(
            name="my-agent", model_id="x", engine="claude",
            server_url="ws://localhost:8765", room="room-abc"
        )
        assert "--room" in cmd and "room-abc" in cmd
        assert "--name" in cmd and "my-agent" in cmd

    def test_cmd_includes_server_url(self):
        """cmd 必須包含 server ws url 供 bridge 連回"""
        cmd = _build_bridge_cmd(
            name="a", model_id="x", engine="claude",
            server_url="ws://remote:9999", room="r"
        )
        assert "ws://remote:9999" in cmd


# ── 2. engine 偵測測試 ──

class TestDetectAvailableEngines:
    """驗證 TUI 端的 engine 可用性檢查"""

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/local/bin/opencode")
    @patch("aiohttp.ClientSession.get")
    async def test_opencode_available_when_healthy(self, mock_get, mock_which):
        """opencode 已安裝且 health check 通過 → 回傳含 'opencode'"""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        engines = await detect_available_engines()
        assert "opencode" in engines

    @pytest.mark.asyncio
    @patch("shutil.which", return_value=None)
    async def test_opencode_unavailable_when_not_installed(self, mock_which):
        """opencode 未安裝 → 回傳不含 'opencode'"""
        engines = await detect_available_engines()
        assert "opencode" not in engines

    @pytest.mark.asyncio
    @patch("importlib.util.find_spec", return_value=MagicMock())
    async def test_claude_available_when_sdk_installed(self, mock_spec):
        """claude_agent_sdk 可 import → 回傳含 'claude'"""
        engines = await detect_available_engines()
        assert "claude" in engines

    @pytest.mark.asyncio
    @patch("importlib.util.find_spec", return_value=None)
    async def test_claude_unavailable_when_sdk_missing(self, mock_spec):
        """claude_agent_sdk 不可 import → 回傳不含 'claude'"""
        engines = await detect_available_engines()
        assert "claude" not in engines


# ── 3. spawn 流程測試 ──

class TestSpawnAgentProcess:
    """驗證 _spawn_agent_process 的成功/失敗路徑"""

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_spawn_success_appends_to_spawned_procs(self, mock_exec):
        """spawn 成功後，proc 應被加入 spawned_procs 列表"""
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        app = make_test_app()  # 建立測試用 App 實例
        result = await app._spawn_agent_process("agent-1", "model", "claude")
        assert result is True
        assert mock_proc in app.spawned_procs

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError)
    async def test_spawn_failure_returns_false(self, mock_exec):
        """bridge.py 找不到時，回傳 False 且 spawned_procs 不變"""
        app = make_test_app()
        result = await app._spawn_agent_process("agent-1", "model", "claude")
        assert result is False
        assert len(app.spawned_procs) == 0


# ── 4. 進程清理測試 ──

class TestProcessCleanup:
    """驗證 TUI 退出時的子進程清理"""

    @pytest.mark.asyncio
    async def test_cleanup_terminates_all_procs(self):
        """cleanup 應對所有活著的 proc 呼叫 terminate()"""
        app = make_test_app()
        proc1 = AsyncMock(); proc1.returncode = None
        proc2 = AsyncMock(); proc2.returncode = None
        app.spawned_procs = [proc1, proc2]
        await app._cleanup_spawned_procs()
        proc1.terminate.assert_called_once()
        proc2.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_skips_already_exited_procs(self):
        """已結束的 proc（returncode is not None）不應被 terminate"""
        app = make_test_app()
        proc = AsyncMock(); proc.returncode = 0
        app.spawned_procs = [proc]
        await app._cleanup_spawned_procs()
        proc.terminate.assert_not_called()
```

### 整合測試（`tests/test_spawn_agent_integration.py`）

啟動真實 server + TUI spawn 真實 bridge subprocess，驗證端對端流程。
需要實際的 async event loop 和 WebSocket 連線。

```python
import pytest
import asyncio
import aiohttp

# ── 1. Local spawn 端對端 ──

class TestLocalSpawnIntegration:
    """啟動真實 server，TUI 端 spawn bridge，驗證 agent 成功加入 room"""

    @pytest.fixture
    async def running_server(self):
        """啟動一個真實的 RoomServer（用隨機 port 避免衝突）"""
        from server import RoomServer
        server = RoomServer(host="127.0.0.1", port=0)  # port=0 讓 OS 分配
        await server.startup()
        yield server
        await server.shutdown()

    @pytest.mark.asyncio
    async def test_spawned_bridge_joins_room(self, running_server):
        """
        TUI spawn bridge.py → bridge 連回 server → server 收到 join →
        驗證 participant list 中出現該 agent
        """
        # 1. 用 TUI 的 _spawn_agent_process 啟動 bridge
        # 2. 等待 server 的 participants 列表出現新 agent（timeout 10s）
        # 3. 驗證 agent name 和 engine 正確
        pass  # 實作時填入

    @pytest.mark.asyncio
    async def test_spawned_agent_responds_to_turn(self, running_server):
        """
        spawn agent → 給它 your_turn → 驗證收到 agent 的回覆訊息
        （需要至少一個可用 engine，CI 可用 mock engine 替代）
        """
        pass

    @pytest.mark.asyncio
    async def test_tui_exit_kills_spawned_procs(self, running_server):
        """
        spawn 2 個 agent → 模擬 TUI exit → 驗證 bridge 子進程已結束
        """
        # 1. spawn 兩個 bridge
        # 2. 記錄 pid
        # 3. 呼叫 cleanup
        # 4. 用 os.kill(pid, 0) 驗證 proc 已不存在
        pass


# ── 2a. Server 純 hub 驗證（需要 running server） ──

class TestServerPureHubRuntime:
    """驗證 server 運行時不再處理 spawn 訊息"""

    @pytest.fixture
    async def running_server(self):
        from server import RoomServer
        server = RoomServer(host="127.0.0.1", port=0)
        await server.startup()
        yield server
        await server.shutdown()

    @pytest.mark.asyncio
    async def test_server_ignores_spawn_agent_message(self, running_server):
        """
        向 server 發送 spawn_agent WS 訊息 →
        server 不應執行任何 subprocess，也不應回覆 spawn_result
        """
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(running_server.ws_url) as ws:
                await ws.send_json({"type": "spawn_agent", "name": "x", "model": "y", "engine": "z"})
                # 等 2 秒，不應收到 spawn_result
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive_json(), timeout=2.0)


# ── 2b. Server 純 hub 驗證（靜態檢查，不需要 server） ──

class TestServerPureHubStatic:
    """驗證 RoomServer class 上不再有 spawn 相關屬性"""

    def test_server_has_no_spawned_procs_attr(self):
        """RoomServer 實例上不應有 spawned_procs 屬性"""
        from server import RoomServer
        server = RoomServer.__new__(RoomServer)
        assert not hasattr(server, "spawned_procs")

    def test_server_has_no_available_engines_attr(self):
        """RoomServer 實例上不應有 available_engines 屬性"""
        from server import RoomServer
        server = RoomServer.__new__(RoomServer)
        assert not hasattr(server, "available_engines")


# ── 3. Engine 偵測整合 ──

class TestEngineDetectionIntegration:
    """在真實環境偵測 engine（CI 環境可能只有部分 engine）"""

    @pytest.mark.asyncio
    async def test_detect_engines_returns_list(self):
        """回傳值是 list[str]，元素只能是 'opencode' 或 'claude'"""
        engines = await detect_available_engines()
        assert isinstance(engines, list)
        for e in engines:
            assert e in ("opencode", "claude")

    @pytest.mark.asyncio
    async def test_model_picker_only_shows_available_engines(self):
        """
        _fetch_models(engines) 只回傳 engines 裡有的 engine 對應模型，
        不會出現不可用 engine 的模型
        """
        models = await _fetch_models(["claude"])  # 只傳 claude
        for m in models:
            assert m["engine"] == "claude"  # 不應有 opencode 的模型


# ── 4. 多 TUI client 場景 ──

class TestMultiClientSpawn:
    """模擬多個 TUI client 各自 spawn agent"""

    @pytest.mark.asyncio
    async def test_two_clients_spawn_agents_independently(self, running_server):
        """
        client A spawn agent-a，client B spawn agent-b →
        server 的 participant list 有兩個 agent，互不干擾
        """
        pass

    @pytest.mark.asyncio
    async def test_client_exit_only_kills_own_agents(self, running_server):
        """
        client A spawn agent-a，client B spawn agent-b →
        client A 退出 → agent-a 被清理，agent-b 仍在線
        """
        pass
```

### 測試執行方式

```bash
# 單元測試（快速，無外部依賴）
pytest tests/test_tui_spawn_agent.py -v

# 整合測試（需要可用 engine，較慢）
pytest tests/test_spawn_agent_integration.py -v

# 全部
pytest tests/test_tui_spawn_agent.py tests/test_spawn_agent_integration.py -v
```

### CI 注意事項

- 單元測試：所有環境都能跑，全 mock
- 整合測試：CI 環境可能沒有 opencode 或 claude SDK，用 `@pytest.mark.skipif` 跳過需要真實 engine 的 case
- 整合測試中「agent 回覆訊息」的測試可用 mock engine（echo bot）替代真實 LLM 呼叫

---

## 驗收標準

### 功能驗收

- [ ] TUI 執行 `/add-agent` 後，能在本地成功 spawn bridge.py 子進程
- [ ] spawn 的 bridge.py 能正常連回 server WebSocket 並加入 room
- [ ] agent 加入後能正常參與對話（收到 `your_turn`、回覆訊息）
- [ ] opencode engine 和 claude engine 都能正常 spawn
- [ ] TUI 正常退出時，所有 spawned agent 子進程被 terminate
- [ ] TUI 異常退出（Ctrl-C）時，子進程也能被清理
- [ ] spawn 失敗時（engine 不可用、bridge.py 啟動失敗），TUI 顯示明確錯誤訊息

### 架構驗收

- [ ] `server.py` 不再包含任何 subprocess spawn 邏輯
- [ ] `server.py` 不再包含 `spawned_procs`、`available_engines` 屬性
- [ ] `server.py` 不再處理 `spawn_agent` WS 訊息類型
- [ ] `server.py` 的 `shutdown()` 不再有 agent process 清理邏輯
- [ ] server 作為純 WebSocket hub，不區分 local spawn 的 agent 和 remote agent

### 相容性驗收

- [ ] bridge.py 本身不做任何修改（它只是被從不同地方啟動）
- [ ] remote agent（手動在遠端啟動 bridge.py）仍能正常連入 room
- [ ] 多個 TUI client 各自 spawn agent 不互相干擾

### Engine 偵測驗收

- [ ] TUI 能獨立偵測本機 opencode 是否可用（不依賴 server）
- [ ] TUI 能獨立偵測 claude agent SDK 是否已安裝
- [ ] `/add-agent` 的 model picker 只顯示本機實際可用的 engine 對應模型
- [ ] opencode serve 未啟動時，TUI 能自動啟動或提示用戶
