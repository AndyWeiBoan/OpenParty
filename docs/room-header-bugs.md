# Room Header Agent Status Bugs

## Background

OpenParty's `RoomHeader` widget displays per-agent status in the TUI. For agents running on the **OpenCode engine**, the status is stuck at either `thinking...` or `standby` — intermediate states like `Read(foo.py)` or `Bash(ls)` never appear.

Investigation confirmed the root cause is **not** in `server.py` (which correctly handles and broadcasts `agent_thinking` events at lines 687–700). The problems are in `bridge.py`'s SSE listener and `openparty_tui.py`'s `update_block()`.

---

## Findings

### Finding 1: SSE Events Are Batch-Flushed, Not Streamed in Real-Time

**File:** `bridge.py` — `_opencode_sse_listener()`

The `reasoning_buf` accumulates `reasoning-delta` / `message.part.delta[field=reasoning]` fragments and only flushes them as a `{"type": "thinking"}` block when `reasoning-end` or `message.part.stop[field=reasoning]` arrives. Similarly, `tool-call` events are the only tool type handled. This means the TUI receives **no updates** during the agent's active thinking period — only after a complete block is assembled. The user sees a static `thinking...` the entire time.

### Finding 2: `reasoning_buf` Is Never Flushed on SSE Listener Exit

**File:** `bridge.py` — `_opencode_sse_listener()`

If the SSE stream is cancelled or terminated before a `reasoning-end` event arrives (e.g. the POST task completes first), the contents of `reasoning_buf` are silently discarded. There is no `finally` block to flush remaining buffered content.

### Finding 3: Race Condition — SSE Listener Exits Before Buffer Is Drained

**File:** `bridge.py` — `_opencode_sse_listener()`, line ~621

The SSE listener checks `done_task.done()` on every line read. When the POST task completes (model finishes generating), the listener may `break` immediately while the SSE stream still has buffered `tool-call` or `reasoning` events. The 5-second grace period in `_call_opencode_with_thinking()` partially mitigates this but is not guaranteed to be sufficient.

### Finding 4: `update_block()` Does Not Handle `"text"` Block Type

**File:** `openparty_tui.py` — `RoomHeader.update_block()`, lines 207–220

The method only matches `btype == "thinking"` and `btype == "tool_use"`. If the OpenCode SSE path produces a block list where the last block is `{"type": "text", ...}` (e.g. a final response text), the reversed iteration hits the `"text"` block first, falls through both `if`/`elif` branches with no `break`, and exits without updating `_agent_status`. The status remains stale instead of reflecting completion. Note: this also affects the Claude SDK path, which explicitly appends `TextBlock` items to the block list (`bridge.py` lines 750–751).

### Finding 5: Only `tool-call` SSE Event Type Is Handled

**File:** `bridge.py` — `_opencode_sse_listener()`

The listener handles `tool-call` but not `tool-input-start`, `tool-result`, or `tool-error`. This means only the initiation of a tool call can ever update the header; result/error states are invisible.

### Finding 6 (Minor): Double Render on `turn_start`

**File:** `openparty_tui.py` — `on_server_message`, lines 886–892

`header.update_info()` calls `_refresh_display()`, then `header.start_thinking()` immediately calls `_refresh_display()` again. Minor redundancy with no functional impact.

---

## Todo List

- [ ] **[bridge.py] Add `finally` flush for `reasoning_buf`**
  In `_opencode_sse_listener()`, add a `finally` block that flushes any remaining `reasoning_buf` content as a `{"type": "thinking"}` block before the coroutine exits.

- [ ] **[bridge.py] Fix SSE listener early-exit race condition**
  Do not `break` immediately when `done_task.done()` is true. Instead, continue draining the SSE stream until it naturally ends or the buffer is empty before exiting the loop.

- [ ] **[bridge.py] Add handlers for `tool-input-start`, `tool-result`, `tool-error` SSE events**
  Map these to appropriate `{"type": "tool_use"}` blocks so the header can show richer intermediate states (e.g. `Bash(ls)` completing, tool errors).

- [ ] **[bridge.py] Consider real-time delta streaming for reasoning**
  Instead of accumulating the full `reasoning_buf` before flushing, send incremental `agent_thinking` events on each `reasoning-delta` so the header updates continuously during thinking.

- [ ] **[openparty_tui.py] Fix `update_block()` to handle `"text"` block type**
  Add an `elif btype == "text":` branch (e.g. set status to `"responding..."`) or add a final `else` fallback so the method always produces a meaningful status update regardless of block type.

- [ ] **[bridge.py] Increase or make configurable the SSE grace period**
  The current 5-second `SSE_TIMEOUT` in `_call_opencode_with_thinking()` may be insufficient for slow tool calls. Consider increasing or tying it to observed SSE activity.

- [ ] **[openparty_tui.py] (Minor) Eliminate double render in `turn_start` handler**
  Remove the redundant `header.update_info()` call before `header.start_thinking()`, or merge them into a single method that performs one render.
