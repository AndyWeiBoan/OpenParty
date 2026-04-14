"""
Unit tests for TUI-side spawn agent logic.

Tests cover:
1. _build_bridge_cmd() — cmd 組裝驗證
2. detect_available_engines() — engine 可用性偵測
3. OpenPartyApp._spawn_agent_process() — spawn 成功/失敗路徑
4. OpenPartyApp._cleanup_spawned_procs() — 子進程清理

這些測試全部使用 mock，不需要真實 server 或 subprocess。
"""

import asyncio
import sys
import os

# 確保專案根目錄在 import path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from openparty_tui import _build_bridge_cmd, detect_available_engines


# ── 輔助函式：建立一個最小可用的 OpenPartyApp 實例（不啟動 Textual UI）────────


def make_test_app():
    """建立 OpenPartyApp 實例供測試使用，不啟動 Textual event loop。"""
    from openparty_tui import OpenPartyApp

    # 用 __new__ 繞過 Textual App.__init__ 的複雜初始化
    app = OpenPartyApp.__new__(OpenPartyApp)
    # 手動初始化測試所需的屬性
    app.room_id = "test-room"
    app.server_url = "ws://localhost:8765"
    app.owner_name = "TestOwner"
    app.owner = True
    app.ws = None
    app.agents = []
    app.available_engines = []
    app.spawned_procs = []
    app._thinking = set()
    app._turn_complete = set()
    app._topic = ""
    app._last_round = 0
    app._completing = False
    app._completing_type = "command"
    app._completion_items = []
    app._pending_images = []
    app._session_id = "test-sess"
    app._image_save_dir = "/tmp/openparty/images/test-sess"
    return app


# ── 1. cmd 組裝測試 ─────────────────────────────────────────────────────────────


class TestBuildBridgeCommand:
    """驗證 _build_bridge_cmd() 產出正確的 CLI 參數列表"""

    def test_opencode_engine_cmd(self):
        """opencode engine 應帶 --engine opencode --opencode-model --model"""
        cmd = _build_bridge_cmd(
            name="agent-1",
            model_id="anthropic/claude-sonnet-4-6",
            engine="opencode",
            server_url="ws://localhost:8765",
            room="test-room",
        )
        assert "--engine" in cmd
        assert "opencode" in cmd
        assert "--opencode-model" in cmd
        assert "--model" in cmd

    def test_opencode_engine_model_id_passed_to_both_flags(self):
        """opencode engine 的 model_id 同時作為 --opencode-model 和 --model 的值"""
        model_id = "anthropic/claude-sonnet-4-6"
        cmd = _build_bridge_cmd(
            name="a",
            model_id=model_id,
            engine="opencode",
            server_url="ws://localhost:8765",
            room="r",
        )
        oc_idx = cmd.index("--opencode-model")
        assert cmd[oc_idx + 1] == model_id
        m_idx = cmd.index("--model")
        assert cmd[m_idx + 1] == model_id

    def test_claude_engine_strips_prefix(self):
        """claude engine 應去掉 'claude/' prefix 再傳給 --model"""
        cmd = _build_bridge_cmd(
            name="agent-1",
            model_id="claude/claude-sonnet-4-6",
            engine="claude",
            server_url="ws://localhost:8765",
            room="test-room",
        )
        assert "claude-sonnet-4-6" in cmd  # 無 "claude/" prefix
        assert "claude/claude-sonnet-4-6" not in cmd

    def test_claude_engine_no_prefix_unchanged(self):
        """claude engine 若 model_id 無 '/' 前綴，直接使用原值"""
        cmd = _build_bridge_cmd(
            name="a",
            model_id="claude-opus-4-6",
            engine="claude",
            server_url="ws://localhost:8765",
            room="r",
        )
        assert "claude-opus-4-6" in cmd

    def test_claude_engine_no_opencode_model_flag(self):
        """claude engine 不應包含 --opencode-model 旗標"""
        cmd = _build_bridge_cmd(
            name="a",
            model_id="claude/claude-sonnet-4-6",
            engine="claude",
            server_url="ws://localhost:8765",
            room="r",
        )
        assert "--opencode-model" not in cmd

    def test_cmd_includes_room_and_name(self):
        """cmd 必須包含 --room 和 --name"""
        cmd = _build_bridge_cmd(
            name="my-agent",
            model_id="x",
            engine="claude",
            server_url="ws://localhost:8765",
            room="room-abc",
        )
        assert "--room" in cmd and "room-abc" in cmd
        assert "--name" in cmd and "my-agent" in cmd

    def test_cmd_includes_server_url(self):
        """cmd 必須包含 server ws url 供 bridge 連回"""
        cmd = _build_bridge_cmd(
            name="a",
            model_id="x",
            engine="claude",
            server_url="ws://remote:9999",
            room="r",
        )
        assert "ws://remote:9999" in cmd

    def test_cmd_is_list_of_strings(self):
        """回傳值必須是字串列表"""
        cmd = _build_bridge_cmd(
            name="a",
            model_id="x",
            engine="claude",
            server_url="ws://localhost:8765",
            room="r",
        )
        assert isinstance(cmd, list)
        assert all(isinstance(s, str) for s in cmd)

    def test_cmd_starts_with_python_executable(self):
        """cmd 第一個元素應為 Python 直譯器路徑"""
        cmd = _build_bridge_cmd(
            name="a",
            model_id="x",
            engine="claude",
            server_url="ws://localhost:8765",
            room="r",
        )
        assert cmd[0] == sys.executable


# ── 2. engine 偵測測試 ──────────────────────────────────────────────────────────


class TestDetectAvailableEngines:
    """驗證 TUI 端的 engine 可用性檢查"""

    @pytest.mark.asyncio
    @patch("openparty_tui.shutil.which", return_value="/usr/local/bin/opencode")
    @patch("openparty_tui.aiohttp.ClientSession")
    async def test_opencode_available_when_healthy(self, mock_session_cls, mock_which):
        """opencode 已安裝且 health check 通過 → 回傳含 'opencode'"""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_get_ctx = MagicMock()
        mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get_ctx)
        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session_ctx

        # Also mock find_spec to return None so claude doesn't appear
        with patch("openparty_tui.importlib.util.find_spec", return_value=None):
            engines = await detect_available_engines()

        assert "opencode" in engines

    @pytest.mark.asyncio
    @patch("openparty_tui.shutil.which", return_value=None)
    async def test_opencode_unavailable_when_not_installed(self, mock_which):
        """opencode 未安裝 → 回傳不含 'opencode'"""
        with patch("openparty_tui.importlib.util.find_spec", return_value=None):
            engines = await detect_available_engines()
        assert "opencode" not in engines

    @pytest.mark.asyncio
    @patch("openparty_tui.shutil.which", return_value="/usr/local/bin/opencode")
    @patch("openparty_tui.aiohttp.ClientSession")
    async def test_opencode_unavailable_when_unhealthy(self, mock_session_cls, mock_which):
        """opencode 已安裝但 health check 失敗 → 回傳不含 'opencode'"""
        mock_session_cls.return_value.__aenter__ = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("openparty_tui.importlib.util.find_spec", return_value=None):
            engines = await detect_available_engines()

        assert "opencode" not in engines

    @pytest.mark.asyncio
    @patch("openparty_tui.shutil.which", return_value=None)
    async def test_claude_available_when_sdk_installed(self, mock_which):
        """claude_agent_sdk 可 import → 回傳含 'claude'"""
        with patch("openparty_tui.importlib.util.find_spec", return_value=MagicMock()):
            engines = await detect_available_engines()
        assert "claude" in engines

    @pytest.mark.asyncio
    @patch("openparty_tui.shutil.which", return_value=None)
    async def test_claude_unavailable_when_sdk_missing(self, mock_which):
        """claude_agent_sdk 不可 import → 回傳不含 'claude'"""
        with patch("openparty_tui.importlib.util.find_spec", return_value=None):
            engines = await detect_available_engines()
        assert "claude" not in engines

    @pytest.mark.asyncio
    @patch("openparty_tui.shutil.which", return_value=None)
    async def test_returns_empty_list_when_nothing_available(self, mock_which):
        """兩種 engine 都不可用時，回傳空列表"""
        with patch("openparty_tui.importlib.util.find_spec", return_value=None):
            engines = await detect_available_engines()
        assert engines == []

    @pytest.mark.asyncio
    @patch("openparty_tui.shutil.which", return_value=None)
    async def test_returns_list_type(self, mock_which):
        """回傳值永遠是 list"""
        with patch("openparty_tui.importlib.util.find_spec", return_value=None):
            engines = await detect_available_engines()
        assert isinstance(engines, list)


# ── 3. spawn 流程測試 ────────────────────────────────────────────────────────────


class TestSpawnAgentProcess:
    """驗證 _spawn_agent_process 的成功/失敗路徑"""

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_spawn_success_appends_to_spawned_procs(self, mock_exec):
        """spawn 成功後，proc 應被加入 spawned_procs 列表"""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        app = make_test_app()

        with patch("builtins.open", MagicMock()):
            result = await app._spawn_agent_process("agent-1", "claude/claude-sonnet-4-6", "claude")

        assert result is True
        assert mock_proc in app.spawned_procs

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("bridge.py not found"))
    async def test_spawn_failure_returns_false(self, mock_exec):
        """bridge.py 找不到時，回傳 False 且 spawned_procs 不變"""
        app = make_test_app()
        # Patch _chat to avoid Textual widget access
        app._chat = MagicMock()

        with patch("builtins.open", MagicMock()):
            result = await app._spawn_agent_process("agent-1", "claude/model", "claude")

        assert result is False
        assert len(app.spawned_procs) == 0

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec", side_effect=PermissionError("no permission"))
    async def test_spawn_failure_on_exception_returns_false(self, mock_exec):
        """任何 Exception 都應回傳 False"""
        app = make_test_app()
        app._chat = MagicMock()

        with patch("builtins.open", MagicMock()):
            result = await app._spawn_agent_process("agent-2", "model", "opencode")

        assert result is False
        assert len(app.spawned_procs) == 0

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_spawn_uses_correct_server_url(self, mock_exec):
        """spawn 時應將 server_url 傳給 _build_bridge_cmd"""
        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        app = make_test_app()
        app.server_url = "ws://custom-host:9999"

        with patch("builtins.open", MagicMock()):
            await app._spawn_agent_process("a", "model", "claude")

        # 確認 create_subprocess_exec 的第一個引數（cmd）包含 server_url
        call_args = mock_exec.call_args[0]  # positional args (cmd spread with *)
        assert "ws://custom-host:9999" in call_args


# ── 4. 進程清理測試 ──────────────────────────────────────────────────────────────


class TestProcessCleanup:
    """驗證 TUI 退出時的子進程清理"""

    @pytest.mark.asyncio
    async def test_cleanup_terminates_all_procs(self):
        """cleanup 應對所有活著的 proc 呼叫 terminate()"""
        app = make_test_app()
        proc1 = MagicMock()
        proc1.returncode = None
        proc2 = MagicMock()
        proc2.returncode = None
        app.spawned_procs = [proc1, proc2]

        app._cleanup_spawned_procs()

        proc1.terminate.assert_called_once()
        proc2.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_skips_already_exited_procs(self):
        """已結束的 proc（returncode is not None）不應被 terminate"""
        app = make_test_app()
        proc = MagicMock()
        proc.returncode = 0
        app.spawned_procs = [proc]

        app._cleanup_spawned_procs()

        proc.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_clears_spawned_procs_list(self):
        """cleanup 完成後 spawned_procs 應為空列表"""
        app = make_test_app()
        proc = MagicMock()
        proc.returncode = None
        app.spawned_procs = [proc]

        app._cleanup_spawned_procs()

        assert app.spawned_procs == []

    @pytest.mark.asyncio
    async def test_cleanup_handles_terminate_exception(self):
        """terminate() 拋出例外時不應讓 cleanup 崩潰"""
        app = make_test_app()
        proc = MagicMock()
        proc.returncode = None
        proc.terminate.side_effect = ProcessLookupError("already dead")
        app.spawned_procs = [proc]

        # Should not raise
        app._cleanup_spawned_procs()

    @pytest.mark.asyncio
    async def test_cleanup_empty_list_is_noop(self):
        """空列表時 cleanup 應正常完成（no-op）"""
        app = make_test_app()
        app.spawned_procs = []
        # Should not raise
        app._cleanup_spawned_procs()


# ── 5. Server pure hub 靜態驗證 ─────────────────────────────────────────────────


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

    def test_server_has_no_spawn_agent_process_method(self):
        """RoomServer 上不應有 _spawn_agent_process 方法"""
        from server import RoomServer

        assert not hasattr(RoomServer, "_spawn_agent_process")
