"""
M2 課題 3 驗證：跨機器 Room 對話
=====================================
兩個使用不同模型的 agent 加入同一個 Room，完成 6 輪對話。

目前配置（cyril 離線時的替代方案）：
  Agent 1 - Llama:  llama3.2:latest  @ localhost:11434  （輕量，快速回應）
  Agent 2 - Qwen:   qwen3:14b        @ localhost:11434  （較強，思考更深）

cyril 上線後替換 Agent 2 的 base_url 為 http://172.16.64.147:11434/v1
即可驗證真實跨機器場景。

執行方式：
  # 終端機 1
  python server.py

  # 終端機 2
  cd m2-core-mvp/src
  python run_cross_machine.py
"""

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, ".")

from openparty import OpenPartyAgent, make_llm_fn, get_preset

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

ROOM_ID = "m2-cross-machine-001"
SERVER_URL = os.environ.get("OPENPARTY_SERVER", "ws://localhost:8765")
MAX_TURNS = 3  # each agent takes 3 turns = 6 total exchanges

# Agent 1: local fast model
AGENT1_MODEL = "llama3.2:latest"
AGENT1_BASE_URL = "http://localhost:11434/v1"

# Agent 2: cyril remote model (gemma3:12b — clean output, no thinking mode issues)
AGENT2_MODEL = "gemma3:12b"
AGENT2_BASE_URL = os.environ.get("AGENT2_BASE_URL", "http://172.16.64.147:11434/v1")

TOPIC = (
    "We are doing a code review. "
    "The team wants to add Redis as a caching layer to a Python web API. "
    "Is this a good idea? What are the trade-offs?"
)

# ── Persona setup ──────────────────────────────────────────────────────────────

personas = get_preset("code-review")
# Architect → Agent 1 (Llama)
# Security  → Agent 2 (Qwen)

# ── Main ───────────────────────────────────────────────────────────────────────


async def main():
    log.info("=" * 60)
    log.info("OpenParty M2 — Cross-Machine Validation")
    log.info("=" * 60)
    log.info(f"Room:    {ROOM_ID}")
    log.info(f"Server:  {SERVER_URL}")
    log.info(f"Agent1:  {personas[0].name} ({AGENT1_MODEL} @ {AGENT1_BASE_URL})")
    log.info(f"Agent2:  {personas[1].name} ({AGENT2_MODEL} @ {AGENT2_BASE_URL})")
    log.info(f"Topic:   {TOPIC[:60]}...")
    log.info("=" * 60)

    # Override the room topic in server — for now just set it via the kickoff
    # (server.py uses a hardcoded topic; M2 will add dynamic topic support)

    llm1 = make_llm_fn(
        model=AGENT1_MODEL,
        base_url=AGENT1_BASE_URL,
        system_prompt=personas[0].system_prompt,
        max_tokens=200,
        temperature=0.7,
    )

    llm2 = make_llm_fn(
        model=AGENT2_MODEL,
        base_url=AGENT2_BASE_URL,
        system_prompt=personas[1].system_prompt,
        max_tokens=200,
        temperature=0.7,
    )

    agent1 = OpenPartyAgent(
        room_id=ROOM_ID,
        name=personas[0].name,
        model=AGENT1_MODEL,
        llm_fn=llm1,
        server_url=SERVER_URL,
        max_turns=MAX_TURNS,
    )

    agent2 = OpenPartyAgent(
        room_id=ROOM_ID,
        name=personas[1].name,
        model=AGENT2_MODEL,
        llm_fn=llm2,
        server_url=SERVER_URL,
        max_turns=MAX_TURNS,
    )

    t0 = time.monotonic()

    # Run both agents concurrently
    try:
        await asyncio.gather(
            agent1.run(),
            asyncio.wait_for(agent2.run(), timeout=300),  # 5min hard cap
        )
    except asyncio.TimeoutError:
        log.error("Timeout: one of the agents took too long")
    except Exception as e:
        log.error(f"Error during run: {e}", exc_info=True)

    elapsed = time.monotonic() - t0
    log.info("=" * 60)
    log.info(f"Run complete. Total time: {elapsed:.1f}s")
    log.info(f"Agent1 turns: {agent1.turns_taken}/{MAX_TURNS}")
    log.info(f"Agent2 turns: {agent2.turns_taken}/{MAX_TURNS}")

    # Verdict
    both_complete = agent1.turns_taken >= MAX_TURNS and agent2.turns_taken >= MAX_TURNS
    log.info(f"Cross-machine validation: {'PASS' if both_complete else 'PARTIAL/FAIL'}")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
