"""
OpenParty LLM layer — OpenAI-compatible unified interface.

Supports any endpoint that speaks the OpenAI Chat Completions API:
  - Ollama          (local:  http://localhost:11434/v1)
  - Remote Ollama   (cyril:  http://172.16.64.147:11434/v1)
  - Groq            (cloud:  https://api.groq.com/openai/v1)
  - OpenRouter      (cloud:  https://openrouter.ai/api/v1)
  - OpenAI          (default, no base_url needed)

Usage:
    from src.llm import make_llm_fn

    llm = make_llm_fn(
        model="qwen3-coder:30b",
        base_url="http://172.16.64.147:11434/v1",
        system_prompt="You are a code reviewer...",
    )
    # llm is an async callable: (payload: dict) -> str
    reply = await llm(your_turn_payload)
"""

import logging
import os
from typing import Callable, Awaitable

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

import re


def _strip_thinking(text: str) -> str:
    """
    Remove <think>...</think> blocks and loose 'Thinking Process:' sections
    that some Qwen3/reasoning models leak into content.
    Returns the clean final answer only.
    """
    # Remove <think>...</think> blocks (Qwen3 style)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove "Thinking Process:\n..." sections that appear before the real answer
    text = re.sub(r"Thinking Process:.*?(?=\n[A-Z]|\Z)", "", text, flags=re.DOTALL)
    return text.strip()


def _build_messages(payload: dict, system_prompt: str, model: str) -> list[dict]:
    """
    Convert an OpenParty your_turn payload into OpenAI-format messages.

    payload keys:
      history  — list of recent room messages (sliding window)
      summary  — str, compressed older history (empty in M2)
      prompt   — str, kickoff topic (only on first turn)
      context  — dict: topic, participants, total_turns
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    # Rolling summary (Phase 2 — injected when present)
    summary = payload.get("summary", "").strip()
    if summary:
        messages.append(
            {
                "role": "system",
                "content": f"[Summary of earlier conversation]\n{summary}",
            }
        )

    # Room context header
    ctx = payload.get("context", {})
    topic = ctx.get("topic", "")
    participants = ctx.get("participants", [])
    total_turns = ctx.get("total_turns", 0)
    if topic or participants:
        p_str = ", ".join(f"{p['name']} ({p['model']})" for p in participants)
        messages.append(
            {
                "role": "system",
                "content": (
                    f"[Room Info]\n"
                    f"Topic: {topic}\n"
                    f"Participants: {p_str}\n"
                    f"Turn #{total_turns + 1}"
                ),
            }
        )

    # Conversation history → user/assistant alternation
    history = payload.get("history", [])
    for entry in history:
        if "agent_id" not in entry:
            continue
        # Heuristic: if the model name appears in the entry's model field, it's "us"
        is_me = model in entry.get("model", "")
        role = "assistant" if is_me else "user"
        label = f"[{entry.get('name', '?')}]: " if not is_me else ""
        messages.append({"role": role, "content": f"{label}{entry['content']}"})

    # First turn with no history: use kickoff prompt as opening user message
    prompt = payload.get("prompt")
    has_user = any(m["role"] == "user" for m in messages)
    if prompt and not history:
        messages.append({"role": "user", "content": prompt})
    elif not has_user:
        messages.append(
            {"role": "user", "content": "Please continue the conversation."}
        )

    return messages


def make_llm_fn(
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int = 0,  # 0 = no limit (safe for thinking models like Qwen3)
    temperature: float = 0.8,
) -> Callable[[dict], Awaitable[str]]:
    """
    Create an llm_fn compatible with OpenPartyAgent.

    Args:
        model:        Model name. For Ollama: "qwen3:14b", "gemma3:27b", etc.
                      For Groq: "llama-3.3-70b-versatile". For OpenAI: "gpt-4o".
        base_url:     OpenAI-compatible endpoint. None = official OpenAI API.
                      Ollama example: "http://172.16.64.147:11434/v1"
                      Groq example:   "https://api.groq.com/openai/v1"
        api_key:      API key. For Ollama: can be None or any string (not validated).
                      For cloud providers: required (or set via env var).
        system_prompt: Override the default system prompt for this agent.
        max_tokens:   Max tokens per response. Default 0 = no limit.
                      WARNING: setting this too low on thinking models (Qwen3, etc.)
                      causes finish_reason=length with empty content — use 0 or a large value.
        temperature:  Sampling temperature (default 0.8).

    Returns:
        An async callable (payload: dict) -> str, ready for OpenPartyAgent.
    """
    # Ollama doesn't need a real API key — use "ollama" as placeholder
    resolved_key = api_key
    if resolved_key is None:
        if base_url and "11434" in base_url:
            resolved_key = "ollama"
        else:
            resolved_key = os.environ.get("OPENAI_API_KEY", "")

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=resolved_key,
    )

    default_prompt = (
        "You are an AI agent participating in an OpenParty multi-agent discussion room. "
        "Engage directly with what others just said. Be concise: 2-4 sentences per turn. "
        "Do not repeat your own previous points. Build the conversation forward."
    )
    resolved_prompt = system_prompt or default_prompt

    async def llm_fn(payload: dict) -> str:
        messages = _build_messages(payload, resolved_prompt, model)
        try:
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            # Only set max_tokens if explicitly requested (>0).
            # Qwen3/thinking models burn the token budget on <think> blocks first;
            # capping too low causes finish_reason=length with empty content.
            if max_tokens > 0:
                kwargs["max_tokens"] = max_tokens

            response = await client.chat.completions.create(**kwargs)
            msg = response.choices[0].message
            content = msg.content

            # Some reasoning models (Qwen3, MiniMax) put output in a separate field
            # when content is empty. Ollama uses "reasoning", MAGI found "reasoning_content".
            if not content or not content.strip():
                for field in ("reasoning_content", "reasoning"):
                    fallback = getattr(msg, field, None)
                    if fallback and fallback.strip():
                        content = fallback
                        log.debug(f"Used fallback field '{field}' for model {model}")
                        break

            if not content or not content.strip():
                raise ValueError(
                    f"Empty response from model (finish_reason={response.choices[0].finish_reason})"
                )

            # Strip <think>...</think> or "Thinking Process:" sections that
            # reasoning models (Qwen3, etc.) occasionally leak into content
            content = _strip_thinking(content)
            if not content:
                raise ValueError("Response contained only thinking content, no answer")

            return content
        except Exception as e:
            log.error(f"LLM error (model={model}): {e}")
            raise

    # Attach metadata for debugging
    llm_fn.__name__ = f"llm_fn[{model}]"
    llm_fn.model = model  # type: ignore
    llm_fn.base_url = base_url  # type: ignore

    return llm_fn
