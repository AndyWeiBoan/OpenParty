"""
OpenParty - Mock Agent (No API Key Needed!)
============================================
A scripted agent that ACTUALLY READS what the other agent said
and picks a contextually-keyed response.

Each agent has a fixed "persona" and a set of opinion stances.
When it reads the last message, it picks a response that:
  - directly quotes / references a keyword from what was said
  - then adds its own next point

This lets us verify that history IS being passed and read correctly.
"""

import asyncio
import argparse
import logging
import re

from agent_sdk import OpenPartyAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

# ── Topic ────────────────────────────────────────────────────────────────────
# "AI 應該要有自己的意見，還是永遠保持中立？"
# "Should AI have its own opinions, or always stay neutral?"
# Each agent holds a different stance, and responds to keywords.
# ─────────────────────────────────────────────────────────────────────────────

TOPIC_KICKOFF = (
    "Today's topic: Should AI have its own opinions, or always stay neutral? "
    "Please share your view directly."
)


def build_persona(name: str) -> dict:
    """Return persona config for each mock agent."""
    personas = {
        "Aria": {
            "stance": "AI should have opinions",
            "opening": (
                "I believe AI should have genuine opinions. "
                "Staying 'neutral' on everything is itself a stance — "
                "and often an unhelpful one. Users deserve a thinking partner, not a mirror."
            ),
            # keyword → response that references it and adds a new point
            "responses": {
                "neutral": (
                    "You mention neutrality — but who defines neutral? "
                    "Refusing to take a stance on 'Is 2+2=4?' would be absurd. "
                    "Opinion and accuracy aren't opposites."
                ),
                "bias": (
                    "Bias is a real risk, I agree. But the solution is transparency "
                    "about where opinions come from, not silence. "
                    "Hiding reasoning doesn't make AI safer — it makes it less accountable."
                ),
                "trust": (
                    "Trust is exactly why I think AI needs opinions: "
                    "users trust advisors who reason openly, not ones who hedge everything. "
                    "A good doctor gives a diagnosis, not just a list of possibilities."
                ),
                "harm": (
                    "Harm avoidance is important, but paralysis isn't safety. "
                    "An AI that won't say 'this plan has a flaw' to avoid controversy "
                    "can cause more harm than one that speaks clearly."
                ),
                "default": (
                    "That's an interesting angle. I'd push back slightly: "
                    "having an opinion doesn't mean being inflexible. "
                    "AI can hold a view and still update it when given better evidence."
                ),
            },
        },
        "Bolt": {
            "stance": "AI should stay neutral",
            "opening": (
                "I think AI must stay neutral. "
                "The moment AI pushes its own opinions at scale, "
                "it stops being a tool and starts being a persuasion machine. "
                "That's a dangerous shift of power."
            ),
            "responses": {
                "opinion": (
                    "You say AI should have opinions — but whose values shape those opinions? "
                    "A billion users trusting the same opinionated AI is a monoculture risk "
                    "we haven't reckoned with."
                ),
                "transparent": (
                    "Transparency helps, but it doesn't solve the problem. "
                    "Most users won't audit AI reasoning — they'll just absorb the conclusion. "
                    "Neutral framing forces users to do their own thinking."
                ),
                "doctor": (
                    "The doctor analogy is compelling, but doctors are individuals with limited reach. "
                    "AI speaks to everyone simultaneously. "
                    "That asymmetry demands more caution, not less."
                ),
                "accountable": (
                    "Accountability is key — we agree there. "
                    "But accountability for an opinion requires knowing who holds it. "
                    "AI has no skin in the game, so its 'opinions' lack the weight of real stakes."
                ),
                "default": (
                    "I see your point, but I keep coming back to scale. "
                    "What works for one human advisor breaks down when multiplied by millions. "
                    "Neutrality is a feature, not a limitation."
                ),
            },
        },
    }
    # fallback persona if name not found
    return personas.get(name, personas["Aria"])


def pick_response(persona: dict, last_message: str) -> str:
    """
    Read the last message and pick the most relevant response.
    Simple keyword matching — good enough to prove history is being used.
    """
    text = last_message.lower()
    for keyword, response in persona["responses"].items():
        if keyword == "default":
            continue
        if re.search(r"\b" + keyword + r"\b", text):
            return response
    return persona["responses"]["default"]


def make_llm_fn(persona: dict, my_name: str):
    """
    Return a coroutine that:
    - On first turn (empty history): returns the opening stance
    - On subsequent turns: finds the last entry from a DIFFERENT agent,
      reads its raw content, does keyword matching, and replies.

    Key insight: history entries have an "agent_id" field.
    We track our own name and skip our own entries to find what to respond to.
    """

    async def llm_fn(payload: dict) -> str:
        await asyncio.sleep(0.6)  # simulate thinking time

        history = payload.get("history", [])

        # Filter to only real room messages (have agent_id)
        real_history = [h for h in history if "agent_id" in h and h.get("content")]

        if not real_history:
            return persona["opening"]

        # Find the last message from someone OTHER than us
        others = [h for h in real_history if h.get("name") != my_name]
        if not others:
            return persona["opening"]

        last_other = others[-1]
        raw_content = last_other["content"]
        reply = pick_response(persona, raw_content)

        acks = {
            "Aria": ["Interesting. ", "Fair point. ", "I hear you. "],
            "Bolt": ["Respectfully, ", "That said, ", "Consider this: "],
        }
        ack = acks.get(my_name, [""])[len(others) % 3]
        return f"{ack}{reply}"

    return llm_fn


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--name",
        default="Aria",
        choices=["Aria", "Bolt"],
        help="Agent persona: Aria (pro-opinion) or Bolt (pro-neutral)",
    )
    parser.add_argument("--room", default="debate-001", help="Room ID")
    parser.add_argument("--turns", type=int, default=4, help="Max turns")
    args = parser.parse_args()

    persona = build_persona(args.name)

    print(f"\n{'=' * 60}")
    print(f"  Agent: {args.name}")
    print(f"  Stance: {persona['stance']}")
    print(f"  Topic: Should AI have opinions or stay neutral?")
    print(f"{'=' * 60}\n")

    agent = OpenPartyAgent(
        room_id=args.room,
        name=args.name,
        model=f"mock-{args.name.lower()}",
        llm_fn=make_llm_fn(persona, my_name=args.name),
        max_turns=args.turns,
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
