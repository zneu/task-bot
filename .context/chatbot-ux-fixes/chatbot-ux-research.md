# Chatbot UX Bugs — Research

## Overview

Analysis of a real user session on 2026-03-17 showing multiple compounding failures that make the bot feel broken and chaotic. The root cause is that **task numbers shown to the user don't match the numbers the bot uses internally**, plus several parsing failures with natural language input.

## Bug Catalog

### Bug 1: Morning Checkin Numbers ≠ Task Map Numbers (CRITICAL)

**The single most destructive bug.** The morning message uses completely different numbering than `/list` and the task_map.

**Morning checkin** (`bot/scheduler.py` L33-43):
- Sorts tasks by **priority (high→low)**, then **avoided_count (desc)**
- Sends this to Claude with `[1]`, `[2]`, `[3]` etc.
- Claude renders the numbered list to the user

**`/list` and `task_map`** (`bot/commands.py` L280, L161):
- Sorts tasks by **project name**, then **created_at**
- Completely different ordering

**What happened:** User saw "12. Finish other tracks" in the morning message. Said "delete task number 12". Bot deleted whatever was at position 12 in the task_map (sorted by project) — **"File tax return"**, a completely different task.

**Impact:** User deleted the WRONG task. This happened silently — user only noticed later when running `/list`.

**Root cause:** Morning checkin (`scheduler.py`) does NOT set `task_map`. It sends a message via `bot.send_message()` — there's no `state` dict involved. So the next time the user references a number, the auto-populated `_ensure_task_map` builds the map using project+created_at ordering, which doesn't match what the user saw.

**Fix approach:** Morning checkin should build the task_map matching its numbering and persist it (using the new `save_task_map` from Phase 2). This way, when the user references "#12" from the morning message, the task_map agrees.

**Files:** `bot/scheduler.py` L18-50, `bot/state.py`

---

### Bug 2: Multi-line Add Treated as Single Task Title (HIGH)

**What happened:** User sent:
```
Add this back under music Review piano study master document

And add file tax return under taxes
```

Bot created ONE task titled: "this back under music Review piano study master document\n\nAnd add file tax return under taxes"

**Root cause:** Fast-path prefix matching (`commands.py` L44-45) catches `"add "` prefix. Compound detection (`L48`) checks `" and " in args.lower()` — but the "And" is preceded by a **newline**, not a space, so `" and "` (space-and-space) doesn't match.

The args become the entire multi-line string, which `cmd_add` (`L392-435`) tries to parse as a single task with inline metadata (`p:`, `proj:`, `due:`). None of those patterns match, so the whole thing becomes the title.

**Fix approach:** Improve compound detection to also catch newline-separated commands. Better: if the message looks like natural language (contains phrases like "under music", "under taxes"), skip fast path entirely and let Claude parse it.

**Files:** `bot/commands.py` L42-49

---

### Bug 3: "Delete task 19" → "No task #task 19" (MEDIUM)

**What happened:** User typed "Delete task 19". Bot replied: "No task #task 19. Use /list first."

**Root cause:** Fast-path catches `"delete "` prefix (`commands.py` L44-45). Args = `"task 19"`. `cmd_delete` (`L588`) does `num = args.strip()` → `"task 19"`. Looks up `task_map.get("task 19")` → None.

Users naturally say "delete **task** 19" or voice transcribes as "delete **task number** four". The word "task" and "number" need to be stripped.

**Fix approach:** Strip common filler words ("task", "number", "#") from the number argument in all number-consuming commands. Or better: move this cleanup into a shared helper.

**Files:** `bot/commands.py` — `cmd_done` L329, `cmd_doing` L334, `_set_status` L339, `cmd_delete` L581, `cmd_edit` L487

---

### Bug 4: Fast-Path Add Swallows Natural Language Commands (MEDIUM)

**What happened:** "Add this back under music Review piano study master document" hit the fast path because it starts with "add ". The `cmd_add` function only understands structured metadata (`p:high`, `proj:Music`, `due:2026-03-20`). It doesn't understand "under music" = project:Music, "this back" = just filler words.

If this had gone to Claude (slow path), it would have correctly parsed:
- Intent: add, title: "Review piano study master document", project: "Music"

**Root cause:** Fast-path is too aggressive. It matches any message starting with "add " even when the rest is natural language, not structured syntax.

**Fix approach:** Make fast-path `cmd_add` only trigger when args look structured (contain `p:`, `proj:`, `due:`, or are a simple title without prepositions like "under", "for", "to", "in"). Otherwise fall through to Claude.

**Files:** `bot/commands.py` L44-61 (fast path routing), L392-435 (cmd_add)

---

### Bug 5: Claude Sometimes Generates Extra Intents from Conversation History (LOW)

**What happened:** Voice message "Delete task number four and delete task number one" resulted in 4 actions: 2 deletes + 1 move + 1 extra delete. The move and extra delete appear to be "replayed" from a previous voice message that was in conversation history.

**Root cause:** `classify_intent` (`services/claude.py` L139-142) passes `conversation_history[-6:]` (last 3 exchanges). If a prior message like "Put under music finish Peyote and finish track with Coop and Shakina and delete 12" is in history, Claude may re-interpret it alongside the new message.

**Fix approach:** Only pass conversation history that's relevant for context (e.g., for follow-up "yes do that" messages), not for all messages. Or: clear history after successful command execution so old commands don't bleed.

**Files:** `services/claude.py` L139-142, `bot/handlers.py` L58-60

---

## Priority Order for Fixes

1. **Bug 1** (Morning numbers ≠ task_map) — highest impact, causes wrong task deletions
2. **Bug 3** (Strip "task"/"number" from args) — easy fix, immediately noticeable
3. **Bug 4** (Fast-path add too aggressive) — natural language should go to Claude
4. **Bug 2** (Newline compound detection) — related to Bug 4
5. **Bug 5** (History bleed) — harder to reproduce, lower priority

## Key Files

| File | Role |
|------|------|
| `bot/scheduler.py` L18-50 | Morning checkin — numbering mismatch |
| `bot/commands.py` L29-61 | Fast-path routing — too aggressive matching |
| `bot/commands.py` L149-195 | `_ensure_task_map` — doesn't match morning |
| `bot/commands.py` L329-364 | `cmd_done`, `cmd_doing`, `_set_status` — no arg cleanup |
| `bot/commands.py` L392-435 | `cmd_add` — structured only, no NL fallback |
| `bot/commands.py` L581-613 | `cmd_delete` — no arg cleanup |
| `services/claude.py` L77-155 | `classify_intent` — history bleed |
| `bot/state.py` | `save_task_map` / `load_task_map` (just added) |

## Complexity Estimate

- **Files to modify:** 3 (`scheduler.py`, `commands.py`, `claude.py`)
- **Estimated phases:** 1 (all fixes are surgical, no schema changes)
- **Risk:** Morning checkin task_map sync requires access to user_id and the save_task_map function from an async context that currently has no state awareness.

## Patterns & Conventions

- Commands follow `async def cmd_X(args, update, state)` signature
- Scheduler functions have no access to `state` — they use `telegram_app.bot.send_message()` directly
- Intent classifier returns JSON parsed by `_dispatch_single_intent`
- Number args are bare strings looked up in `state["task_map"]`
