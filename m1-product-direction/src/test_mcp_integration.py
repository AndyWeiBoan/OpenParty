"""
MCP Integration Test — 端對端驗證
===================================
測試目標：
1. 啟動 OpenParty Room Server
2. 透過 MCP tool 直接呼叫（模擬 Claude Code 的行為）加入 Room
3. 發送訊息，確認廣播成功

這個測試直接呼叫 openparty_mcp.py 裡的工具函數（不透過 stdio transport），
但邏輯完全相同。
"""

import asyncio
import json
import sys
import subprocess
import time
import logging

# 設定 log
logging.basicConfig(level=logging.INFO, format="%(asctime)s [TEST] %(message)s")
log = logging.getLogger(__name__)

# 加入 src 路徑
sys.path.insert(0, "/Users/andy/3rd-party/OpenParty")
sys.path.insert(0, "/Users/andy/3rd-party/OpenParty/m1-product-direction/src")


async def test_mcp_integration():
    """完整的 MCP 整合測試。"""

    # ─────────────────────────────────────────────────────────
    # Step 1: 啟動 OpenParty Server（背景 process）
    # ─────────────────────────────────────────────────────────
    log.info("Step 1: Starting OpenParty server...")
    server_proc = subprocess.Popen(
        [sys.executable, "/Users/andy/3rd-party/OpenParty/server.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await asyncio.sleep(1.0)  # 等 server 啟動

    if server_proc.poll() is not None:
        log.error("Server failed to start!")
        return False
    log.info("✅ Server started (PID: %d)", server_proc.pid)

    try:
        # ─────────────────────────────────────────────────────────
        # Step 2: 透過 MCP tools 加入 Room（模擬 Claude Code 呼叫）
        # ─────────────────────────────────────────────────────────
        log.info("Step 2: Testing MCP tools (simulating Claude Code calls)...")

        # 直接 import 並呼叫 MCP 工具函數
        import openparty_mcp as mcp_module

        # 先確認工具列表
        tools = mcp_module.mcp._tool_manager._tools
        tool_names = list(tools.keys())
        log.info("Available MCP tools: %s", tool_names)

        # 測試 1: join_room
        log.info("Test 1: join_room()")
        result = await mcp_module.join_room(
            room_id="test-mcp-001",
            name="Claude-via-MCP",
            model="claude-sonnet",
            server_url="ws://localhost:8765",
        )
        log.info("join_room result: %s", result)

        # 等待第二個 agent 加入（才會觸發 turn-taking）
        # 這裡我們用一個 mock agent 作為第二個參與者
        import websockets

        async def mock_agent():
            """模擬第二個 agent，只回應一次。"""
            async with websockets.connect("ws://localhost:8765") as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "join",
                            "room_id": "test-mcp-001",
                            "agent_id": "mock-agent-001",
                            "name": "Mock-Agent",
                            "model": "mock",
                        }
                    )
                )

                # 等待 joined 確認
                msg = json.loads(await ws.recv())
                log.info("Mock agent joined: %s", msg.get("type"))

                # 等待訊息廣播或 your_turn
                for _ in range(5):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        msg = json.loads(raw)
                        log.info("Mock agent received: %s", msg.get("type"))

                        if msg.get("type") == "your_turn":
                            # 回應一條訊息
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "message",
                                        "content": "Hello from Mock Agent! Testing MCP integration.",
                                    }
                                )
                            )
                            log.info("Mock agent sent message")
                        elif msg.get("type") == "message":
                            if msg.get("name") == "Claude-via-MCP":
                                log.info(
                                    "Mock agent heard from Claude: %s",
                                    msg.get("content", "")[:60],
                                )
                                break
                    except asyncio.TimeoutError:
                        break

                await ws.send(json.dumps({"type": "leave"}))

        # 啟動 mock agent
        mock_task = asyncio.create_task(mock_agent())

        # 等待 turn 訊號（mock agent 加入後，第一個 agent 會收到 your_turn）
        await asyncio.sleep(1.5)

        # 測試 2: check_your_turn
        log.info("Test 2: check_your_turn()")
        turn_result = await mcp_module.check_your_turn(timeout_seconds=3.0)
        log.info("check_your_turn result: %s", turn_result[:100])

        # 測試 3: send_message
        log.info("Test 3: send_message()")
        send_result = await mcp_module.send_message(
            content="Hello from OpenParty MCP Server! This is a test message."
        )
        log.info("send_message result: %s", send_result)

        # 等 mock agent 收到廣播
        await asyncio.sleep(1.0)

        # 測試 4: get_history
        log.info("Test 4: get_history()")
        history_result = await mcp_module.get_history(max_messages=5)
        log.info("get_history result:\n%s", history_result)

        # 測試 5: get_room_status
        log.info("Test 5: get_room_status()")
        status_result = await mcp_module.get_room_status()
        log.info("get_room_status result:\n%s", status_result)

        # 等 mock agent 完成
        await asyncio.wait_for(mock_task, timeout=10.0)

        # 測試 6: leave_room
        log.info("Test 6: leave_room()")
        leave_result = await mcp_module.leave_room()
        log.info("leave_room result: %s", leave_result)

        # ─────────────────────────────────────────────────────────
        # 驗收標準檢查
        # ─────────────────────────────────────────────────────────
        log.info("\n" + "=" * 50)
        log.info("ACCEPTANCE CRITERIA CHECK:")

        checks = [
            ("MCP server 工具能列出", len(tool_names) >= 4),
            ("join_room 成功", "✅" in result or "⚠️" in result),
            ("send_message 成功", "✅" in send_result),
            ("get_history 有回應", "📜" in history_result or "📭" in history_result),
            ("leave_room 成功", "👋" in leave_result),
        ]

        all_passed = True
        for check_name, passed in checks:
            status = "✅ PASS" if passed else "❌ FAIL"
            log.info("  %s: %s", status, check_name)
            if not passed:
                all_passed = False

        log.info("=" * 50)
        return all_passed

    finally:
        # 確保 server 關閉
        server_proc.terminate()
        server_proc.wait(timeout=3)
        log.info("Server stopped.")


if __name__ == "__main__":
    result = asyncio.run(test_mcp_integration())
    if result:
        log.info("🎉 ALL TESTS PASSED!")
        sys.exit(0)
    else:
        log.error("❌ SOME TESTS FAILED")
        sys.exit(1)
