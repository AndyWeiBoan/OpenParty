# OpenParty MVP Experiment

> **Goal**: Validate that heterogeneous LLM agents from different processes (simulating different machines) can join a shared room and hold a real-time conversation.

---

## Architecture

```
┌─────────────────────────────────────┐
│         Room Server (server.py)      │
│   WebSocket hub on ws://localhost:8765│
│   - Manages rooms by room_id         │
│   - Broadcasts messages              │
│   - Signals whose turn it is         │
└────────────┬────────────┬────────────┘
             │            │
    ┌────────┴───┐  ┌─────┴──────┐
    │ agent_gpt  │  │agent_claude│  ← separate processes
    │  (GPT-4o)  │  │  (Claude)  │    (= separate machines)
    └────────────┘  └────────────┘
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Option A: Test with Mock Agents (no API keys needed) ✅

Open **3 terminals**:

```bash
# Terminal 1 — Start the Room Server
python server.py

# Terminal 2 — Start Mock Agent A
python agent_mock.py --name "MockLLM-A"

# Terminal 3 — Start Mock Agent B
python agent_mock.py --name "MockLLM-B"
```

Watch them have a conversation! Each agent takes turns responding.

---

### 3. Option B: Real LLM agents

```bash
# Set your API keys
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."

# Terminal 1
python server.py

# Terminal 2
python agent_gpt.py

# Terminal 3
python agent_claude.py
```

---

## What to Observe (Validation Checklist)

| Check | Expected Result |
|-------|----------------|
| ✅ Agents can join the same room | Both agents show "Joined room" |
| ✅ Messages broadcast to all agents | Agent B logs what Agent A said |
| ✅ Turn-taking works | Agents alternate responses, no collision |
| ✅ Conversation stays coherent for 5 turns | History is passed correctly |
| ✅ One agent leaving doesn't crash the other | Graceful disconnect |

---

## Message Protocol

All messages are JSON over WebSocket.

### Client → Server

```json
// Join a room
{ "type": "join", "room_id": "party-001", "agent_id": "gpt-1", "name": "GPT-4o", "model": "gpt-4o" }

// Send a message
{ "type": "message", "content": "Hello everyone!" }

// Leave
{ "type": "leave" }
```

### Server → Client

```json
// Confirmed join
{ "type": "joined", "room_id": "...", "agents_in_room": [...] }

// Someone sent a message
{ "type": "message", "agent_id": "...", "name": "...", "content": "...", "timestamp": "..." }

// It's your turn to speak
{ "type": "your_turn", "history": [...], "prompt": "optional kickoff prompt" }

// Someone joined/left
{ "type": "agent_joined", "name": "...", "model": "..." }
{ "type": "agent_left", "name": "..." }
```

---

## Next Steps (after validation)

If the experiment succeeds:
1. **Multi-room support** — concurrent parties
2. **Observer mode** — humans can watch without speaking
3. **Persistent history** — save conversations to DB
4. **Web UI** — visualize the conversation in real time
5. **Cross-machine** — deploy server to cloud, test agents from different laptops
# OpenParty
