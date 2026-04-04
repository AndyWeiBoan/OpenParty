"""
Test: Does resume=session_id preserve conversation history?

Round 1: Tell Claude a secret number, get session_id
Round 2: Resume session, ask what the number was — WITHOUT including history in prompt
If Claude answers correctly → session history IS preserved → build_prompt's history[-8:] is redundant
"""

import asyncio
from claude_agent_sdk import ClaudeAgentOptions, query, AssistantMessage, ResultMessage, SystemMessage


async def call_claude(prompt: str, session_id: str | None) -> tuple[str, str | None]:
    """Returns (result_text, session_id)"""
    options = ClaudeAgentOptions(
        allowed_tools=[],
        permission_mode="bypassPermissions",
        resume=session_id,
        max_turns=1,
    )

    result_text = ""
    new_session_id = session_id

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, SystemMessage):
            sid = getattr(message, "session_id", None)
            if sid and new_session_id is None:
                new_session_id = sid
                print(f"  [session from SystemMessage: {new_session_id}]")
        elif isinstance(message, ResultMessage):
            result_text = getattr(message, "result", "") or ""
            sid = getattr(message, "session_id", None)
            if sid and new_session_id is None:
                new_session_id = sid
                print(f"  [session from ResultMessage: {new_session_id}]")

    return result_text, new_session_id


async def main():
    print("=== Test: Session Resume History ===\n")

    # Round 1: establish session
    print("Round 1: Tell Claude a secret number (no history passed)")
    prompt1 = "Remember this secret number: 7391. Just acknowledge you got it."
    reply1, session_id = await call_claude(prompt1, session_id=None)
    print(f"  Prompt : {prompt1}")
    print(f"  Reply  : {reply1}")
    print(f"  Session: {session_id}\n")

    if not session_id:
        print("ERROR: No session_id received. Cannot test resume.")
        return

    # Round 2: resume session, ask WITHOUT including history in prompt
    print("Round 2: Ask about the number — prompt has NO history, only the question")
    prompt2 = "What was the secret number I just told you?"
    reply2, _ = await call_claude(prompt2, session_id=session_id)
    print(f"  Prompt : {prompt2}")
    print(f"  Reply  : {reply2}\n")

    # Evaluate
    if "7391" in reply2:
        print("RESULT: PASS — Claude remembered '7391' via session resume alone.")
        print("=> build_prompt's history[-8:] is REDUNDANT when using resume.")
    else:
        print("RESULT: FAIL — Claude did NOT remember the number.")
        print("=> Session history is NOT preserved by resume. The history[-8:] in build_prompt is NECESSARY.")


if __name__ == "__main__":
    asyncio.run(main())
