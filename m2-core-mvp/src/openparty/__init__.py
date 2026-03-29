"""
OpenParty — Let heterogeneous LLM agents talk to each other in real-time Rooms.

Quick start:
    from openparty import OpenPartyAgent, make_llm_fn

    llm = make_llm_fn(
        model="qwen3-coder:30b",
        base_url="http://172.16.64.147:11434/v1",  # any OpenAI-compatible endpoint
    )
    agent = OpenPartyAgent(room_id="my-room", name="Qwen", model="qwen3-coder:30b", llm_fn=llm)
    await agent.run()
"""

from .agent import OpenPartyAgent
from .llm import make_llm_fn
from .presets import PRESETS, get_preset

__all__ = ["OpenPartyAgent", "make_llm_fn", "PRESETS", "get_preset"]
__version__ = "0.2.0"
