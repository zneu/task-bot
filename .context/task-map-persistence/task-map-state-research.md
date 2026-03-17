# Task Map & Session State Persistence — Research

## Overview

The user reports that in a new chat (bot restart or fresh conversation), they need to run `/list` before commands like `/done 3` work — even though they ran `/list` in a prior session. An auto-populate fix was already committed (46170af) but the problem has edge cases and fundamental limitations worth documenting.

## Key Files

| File | Role | Key Lines |
|------|------|-----------|
| `bot/state.py` | In-memory per-user state store | 1-32 |
| `bot/commands.py` | Command routing, `_ensure_task_map()`, `cmd_list()` | 29-50 (route), 149-165 (ensure), 273-326 (list) |
| `bot/handlers.py` | Message dispatch, callback handler | 44-71 (process_input), 183-213 (callbacks) |
| `database/models.py` | Task ORM model (PostgreSQL) | Full file |
| `database/connection.py` | SQLAlchemy async session factory | Full file |
| `services/claude.py` | Intent classification | 77-149 |

## Architecture

### State Layers

1. **PostgreSQL** (persistent) — Source of truth for tasks, people, notes. Survives restarts.
2. **In-memory `user_states` dict** (volatile) — Per-user session state. Lost on bot restart.
3. **Notion** (write-only sync) — Never queried, only pushed to.

### The `task_map` Structure

A mapping from display numbers to task UUIDs:
```python
{"1": "uuid-abc", "2": "uuid-def", "3": "uuid-ghi"}
```

Lives in `user_states[user_id]["task_map"]` — initialized as `{}` on first access.

## Data Flow

### How `task_map` Gets Populated

**Path 1: Explicit `/list` command** (`cmd_list`, line 273-326)
- Queries DB for tasks (filtered by status/project)
- Builds numbered display list
- Sets `state["task_map"]` as side effect
- User sees the numbered list so they know which number maps to what

**Path 2: Auto-populate** (`_ensure_task_map`, line 149-165)
- Triggered at start of `route_command()` (line 39) and `_classify_and_dispatch()` (line 95)
- Checks `if state.get("task_map"): return` — skips if already populated
- Queries DB for non-done tasks, ordered by project + created_at
- Builds map silently (no output to user)

### How Commands Use `task_map`

Commands that need it: `done`, `doing`, `edit`, `delete`, `move`
- All look up `state.get("task_map", {}).get(num)`
- If lookup fails: reply "No task #N. Use /list first."

### How Callback Buttons Use `task_map`

`handle_callback` (handlers.py line 183-213) calls `_set_status_from_callback` directly — does NOT call `_ensure_task_map` first. If `task_map` is empty at callback time, buttons fail silently.

## Current Auto-Populate: What It Does and Doesn't Solve

### What It Solves
- First command in a new session auto-populates from DB
- User can say `/done 3` without running `/list` first (if they remember/guess the number)

### What It Does NOT Solve

1. **Numbers are invisible after auto-populate.** The user doesn't know what number maps to what task unless they run `/list`. The auto-populate builds the map silently. So while `/done 3` technically works, the user has no way to know that task 3 is "Buy groceries" without running `/list` first.

2. **Numbers drift after mutations.** After `/done 3`, the map still has the old entries. Task 3 is now done, but if the user runs `/list` again, the remaining tasks get renumbered. The auto-populated map and the `/list` map can diverge.

3. **Bot restart clears everything.** All state (task_map, conversation_history, mode) is lost. The auto-populate handles task_map, but conversation context is gone.

4. **Callback buttons after restart.** Inline keyboard buttons from a previous `/list` still exist in the Telegram chat, but the task_map they reference is gone. Pressing "Done" on an old button fails because `_set_status_from_callback` doesn't call `_ensure_task_map`.

5. **`/list` with filters creates different maps.** `/list Thrown` creates a map with only Thrown project tasks. Auto-populate creates a map with ALL non-done tasks. The numbering differs.

## Possible Improvements

### Tier 1: Quick Wins (current architecture)

**A. Fix callback handler to call `_ensure_task_map`**
- `handle_callback` in handlers.py should call `_ensure_task_map(state)` before `_set_status_from_callback`
- Files: `bot/handlers.py` line 193-199

**B. Use task UUIDs in callback data instead of display numbers**
- Change callback_data from `"done:3"` to `"done:uuid-abc"`
- Eliminates dependency on task_map for button presses entirely
- Files: `bot/commands.py` (where buttons are created), `bot/handlers.py` (callback parsing)

**C. Auto-show abbreviated task list on first command**
- When `_ensure_task_map` actually populates (not skips), also send a brief "Your tasks: 1. X, 2. Y, 3. Z" message
- User knows the numbering without running `/list` separately
- Files: `bot/commands.py` lines 149-165

### Tier 2: State Persistence (architecture change)

**D. Persist `task_map` to database**
- Add a `UserState` table or a `task_map` JSON column
- Load on bot startup / first access
- Numbers stay stable across restarts
- Files: `database/models.py`, `bot/state.py`, `bot/commands.py`

**E. Persist conversation history to database**
- Add a `ConversationMessage` table
- Load last N messages on first access
- Context survives restarts
- Files: `database/models.py`, `bot/state.py`, `services/claude.py`

### Tier 3: Rethink Numbering (design change)

**F. Stable task identifiers instead of sequential numbers**
- Use short codes (e.g., first 4 chars of UUID, or monotonic counter stored on the task)
- Numbers don't change when tasks are completed or list is re-filtered
- Eliminates the entire class of "stale task_map" bugs
- Files: `database/models.py`, `bot/commands.py` (display + lookup), all command handlers

**G. Name-based task references**
- Let Claude match "done buy groceries" to the right task by title similarity
- Already partially possible via intent classification, but not implemented for done/doing/edit
- Files: `bot/commands.py`, `services/claude.py`

## Dependencies

- `task_map` is consumed by: `cmd_done`, `cmd_doing`, `_set_status`, `_set_status_from_callback`, `cmd_edit`, `cmd_delete`, `cmd_move`, `cmd_viewnote`
- `task_map` is produced by: `cmd_list`, `_ensure_task_map`
- Callback buttons reference task_map numbers: `handle_callback` → `_set_status_from_callback`
- Intent classifier can produce `num` fields that reference task_map: `_dispatch_single_intent`

## Patterns & Conventions

- In-memory state dict per user, lazy-initialized
- Commands are async functions with signature `(args: str, update: Update, state: dict)`
- Fast-path prefix matching + slow-path Claude classification
- PostgreSQL via async SQLAlchemy, Notion via sync API calls
- No caching layer, no Redis, no file-based persistence

## Testing

No test files exist in the repository.

## Related Systems

- Conversation memory (also volatile, also affected by restarts)
- Scheduler (queries DB directly, independent of user state)
- Notion sync (write-only, not affected)

## Complexity Estimate

- **Files to modify:** 3-4 (commands.py, handlers.py, state.py, possibly models.py)
- **Estimated phases:** 1-2
- **Rationale:** Tier 1 fixes (A-C) are a single phase, ~1 hour. Tier 2 (D-E) is a second phase with schema changes. Tier 3 (F-G) is a design rethink, separate effort.
- **Phase boundaries (if multi):**
  - Phase 1: Fix callback handler, use UUIDs in callback data, auto-show task list on populate
  - Phase 2: Persist task_map and conversation history to database
