# Chat Memory & Conversation Continuity — Research

## Overview

When the user sends a complex compound command (5+ actions), `classify_intent` can't produce a clean JSON array and returns `{"intent": "chat"}`. The chat fallback then responds conversationally — it understands the request, asks smart clarifying questions, but **cannot execute**. If the user then says "yes do it all" or corrects task numbers, the next message goes through `classify_intent` again with no memory of the previous exchange. The classifier sees "yes do it all" as a standalone message with no context, so it returns `"chat"` again, and the conversation loops without ever executing.

Two problems:
1. `classify_intent` has no conversation history — it sees each message in isolation
2. There's no way to reset/clear the conversation when it gets stuck

---

## Key Files

| File | Role | Lines |
|------|------|-------|
| `bot/handlers.py` | process_input, chat fallback with enriched history | 69 |
| `bot/commands.py` | route_command, _classify_and_dispatch | ~600 |
| `services/claude.py` | classify_intent (no history), chat (has history) | 150 |
| `bot/state.py` | In-memory state: conversation_history (10 msg limit) | 32 |

---

## Architecture

### Current Message Flow

```
User message
  → process_input()
    → route_command()
      → fast path (prefix match) → execute
      → slow path: classify_intent(text, projects, task_map, task_context, people)
        → SINGLE message, NO conversation history
        → returns intent JSON or "chat"
      → if "chat": return False
    → chat fallback:
      → builds enriched_history = [task_context] + conversation_history (last 10)
      → chat(text, enriched_history) → Claude responds conversationally
      → saves user msg + response to conversation_history
```

### The Gap

`classify_intent` and `chat` are two separate Claude calls with different context:

| | classify_intent | chat |
|---|---|---|
| Sees conversation history | No | Yes (last 10) |
| Sees task/people context | Yes | Yes |
| Can execute commands | Yes (returns structured JSON) | No (returns text) |
| Handles ambiguity | Poorly (returns "chat" if unsure) | Well (asks clarifying questions) |

The problem: the thing that can execute has no memory, and the thing that has memory can't execute.

---

## Data Flow

### Conversation History

```
add_to_history(user_id, "user", text)      → appends to state["conversation_history"]
add_to_history(user_id, "assistant", text)  → appends
Trim: if len > 10, keep last 10
```

History is only added in the chat fallback path (handlers.py L67-68). Command executions (done, add, list, etc.) do NOT add to conversation history.

### What classify_intent Receives

```python
classify_intent(
    text,                    # Just the current message
    projects,                # ["Thrown", "Music", ...]
    task_map,                # {"1": "uuid", "2": "uuid", ...}
    task_context,            # "- Buy groceries | status:not_started | priority:medium\n..."
    people                   # ["Sarah", "Mike", ...]
)
```

No conversation history. No previous messages. No context of what was discussed before.

---

## Key Functions/Components

### classify_intent (services/claude.py L77-149)
- **Inputs:** Single text message + task/people context
- **Outputs:** Single intent dict or array of intent dicts
- **Messages sent to Claude:** `[{"role": "user", "content": text}]` — one message only
- **max_tokens:** 200 — tight limit, may truncate complex multi-action arrays

### chat (services/claude.py L21-28)
- **Inputs:** User message + conversation history (enriched with task context)
- **Outputs:** Conversational text response
- **System prompt:** "You are an accountability partner. Be direct, warm, and brief."
- **No execution capability** — just returns text

### conversation_history (bot/state.py L27-31)
- **Storage:** In-memory list of `{"role": "user|assistant", "content": "..."}`
- **Limit:** 10 messages (5 exchanges)
- **Persistence:** Lost on restart
- **Only updated by:** Chat fallback (not by command executions)

---

## Dependencies

### What Needs to Change

1. **classify_intent needs conversation history** — so "yes do it all" can be understood in context
2. **classify_intent needs higher max_tokens** — 200 is too low for complex multi-action arrays
3. **Command executions should update conversation history** — so Claude knows what was just done
4. **Need a `/clear` or `/reset` command** — to start fresh when conversation gets confused
5. **Chat fallback could detect pending commands** — if Claude's conversational response included structured intents, they could be extracted and executed

---

## Patterns & Conventions

- Lazy imports inside functions
- State stored as plain dict per user_id
- Claude responses parsed by stripping code fences and JSON.loads
- All command handlers follow `cmd_*(args, update, state)` signature

---

## Testing

No automated tests. Manual testing via Telegram.

---

## Related Systems

- **Brain dump confirmation** — The one place where multi-turn execution exists. Brain dump → show items → user confirms → execute. This pattern could be generalized.
- **Passive check-ins** — No response expected, no conversation state needed.

---

## What Needs to Be Built

### 1. Pass conversation history to classify_intent

Currently `classify_intent` sends a single user message. It should send the last few exchanges so Claude can understand follow-ups like "yes do it all" or "I meant task 3 not 11".

Change: Instead of `messages=[{"role": "user", "content": text}]`, send the recent conversation history + current message. This costs a few hundred more tokens per call but enables conversational command execution.

### 2. Increase max_tokens for classify_intent

200 tokens is tight for a single intent. For an array of 5+ intents, it truncates. Bump to 500-800.

### 3. Add command execution to conversation history

When `done 3` executes and replies "Done: Buy groceries", that exchange should be saved to conversation_history. Currently only chat messages are saved. This gives both the classifier and chat a complete picture of what's happened.

### 4. Add `/clear` command

Simple: reset `conversation_history` to empty list. Fast path command, no Claude call needed.

### 5. Update chat system prompt

The chat system prompt is generic ("accountability partner"). It should instruct Claude that when the user is clearly asking to execute task commands, it should respond with structured JSON that can be parsed, rather than just conversational text. Or: the chat fallback should re-attempt classification after a clarification exchange.

### Alternative Approach: Re-classify After Chat Clarification

Instead of giving classify_intent history, keep the separation but add a detection step:

1. Chat responds with clarification
2. User answers
3. Before routing to chat again, concatenate the last exchange + new message and send the whole thing to classify_intent as a single block
4. If classify_intent returns structured intents → execute
5. If still "chat" → continue conversation

This avoids changing the classifier's prompt/behavior and just gives it more text to work with on the retry.

---

## Complexity Estimate

- **Files to modify:** 4 (services/claude.py, bot/commands.py, bot/handlers.py, main.py)
- **Estimated phases:** 1
- **Rationale:** Changes are small and self-contained. Pass history to classifier, bump max_tokens, save command results to history, add /clear command. No new models or tables.
