# Reminder Flow & Weekly Review — Research

## Overview

Zachary's Telegram accountability bot has three daily check-ins (morning, afternoon, evening) and a weekly summary. Two problems:

1. **Weekly Review says "nothing to review"** — because check-in history only gets created when the user responds to evening check-ins. No responses = no history = weekly review has nothing to work with.
2. **Check-ins demand responses** — morning and evening check-ins set a state mode that intercepts all subsequent messages, forcing the user into a structured response flow before they can do anything else. Zachary wants passive Mon-Fri reminders he can ignore.

## Key Files

| File | Role | Relevant Lines |
|------|------|----------------|
| `bot/scheduler.py` | Schedules and sends all check-ins + weekly summary | 18-174 |
| `bot/handlers.py` | Routes messages by state mode; handles morning/evening responses | 44-75, 153-275 |
| `bot/state.py` | In-memory state machine (mode: idle/morning/evening/brain_dump_confirm) | 1-36 |
| `services/claude.py` | Claude API calls, `get_checkin_context()` for check-in history | 110-134 |
| `database/models.py` | `CheckIn` model — only created during `handle_evening_response` | 32-38 |
| `prompts/morning.txt` | Morning prompt — asks user to pick task numbers | 1-31 |
| `prompts/afternoon.txt` | Afternoon prompt — asks for status update | 1-9 |
| `prompts/evening.txt` | Evening prompt — asks done/in_progress/avoided per task | 1-9 |
| `main.py` | Wires up scheduler at startup | 53-58 |
| `.env.example` | Config for check-in times and timezone | 19-22 |

## Architecture

### Scheduling

APScheduler fires three daily CronTrigger jobs + one weekly (Sunday) job:

```
start_scheduler(telegram_app)  [scheduler.py:135]
  ├── morning_checkin   — every day at MORNING_CHECK_IN_TIME
  ├── afternoon_checkin — every day at AFTERNOON_CHECK_IN_TIME
  ├── evening_checkin   — every day at EVENING_CHECK_IN_TIME
  └── weekly_summary    — Sundays, 30 min before evening
```

No day-of-week filtering on daily jobs — they fire 7 days/week. Zachary wants Mon-Fri only.

### State Machine & Response Requirement

The core issue lives in how check-ins interact with the state machine:

```
morning_checkin() [scheduler.py:18-57]
  1. Fetches open tasks from DB
  2. Sends Claude-generated message listing tasks
  3. Sets state["mode"] = "morning"     ← THIS IS THE PROBLEM
  4. Stores task number→ID mapping

evening_checkin() [scheduler.py:85-113]
  1. Fetches committed tasks from DB
  2. Sends Claude-generated message asking what happened
  3. Sets state["mode"] = "evening"     ← THIS IS THE PROBLEM
  4. Stores committed_task_ids

afternoon_checkin() [scheduler.py:60-82]
  1. Fetches committed tasks
  2. Sends Claude-generated nudge
  3. Does NOT change state              ← This one is fine
```

When `state["mode"]` is "morning" or "evening", the message handler (`handlers.py:44-62`) intercepts **all incoming messages** and routes them to the corresponding handler:

```python
# handlers.py:54-62
if state["mode"] == "morning":
    await handle_morning_response(text, update)
    return
if state["mode"] == "evening":
    await handle_evening_response(text, update)
    return
```

This means:
- After a morning check-in fires, the bot is **stuck in "morning" mode** until the user sends task numbers
- After an evening check-in fires, the bot is **stuck in "evening" mode** until the user sends a status update
- Any other message (like a task command) gets misrouted to the check-in handler
- If the user ignores the morning check-in, the evening check-in sets mode to "evening", overwriting "morning" — but the user is still trapped in a response-required state

### Weekly Review Empty State

`weekly_summary()` [scheduler.py:116-132]:
1. Calls `get_checkin_context()` → queries `CheckIn` table for last 7 days
2. Passes history to Claude with a review prompt
3. Sends Claude's response prefixed with "Weekly Review\n\n"

`get_checkin_context()` [services/claude.py:110-134]:
- Returns `"No check-in history yet."` when there are zero `CheckIn` records

`CheckIn` records are ONLY created in `handle_evening_response()` [handlers.py:251-256]:
```python
checkin = CheckIn(
    type="evening",
    committed_task_ids=state["committed_task_ids"],
    summary=text,
)
```

**Root cause chain:**
1. Check-ins require responses → Zachary doesn't respond → no `CheckIn` records created
2. No `CheckIn` records → `get_checkin_context()` returns "No check-in history yet."
3. Claude sees empty history → generates the "nothing to review" message

## Data Flow

### Current Flow (Interactive)
```
Scheduler fires check-in
  → Claude generates personalized message based on tasks
  → Bot sends message to Telegram
  → state["mode"] set to "morning"/"evening"
  → User MUST respond with structured input
  → Handler processes response → creates DB records
  → state["mode"] reset to "idle"
```

### Desired Flow (Passive)
```
Scheduler fires reminder (Mon-Fri only)
  → Claude generates personalized message based on tasks
  → Bot sends message to Telegram
  → state["mode"] stays "idle" (no response expected)
  → User can optionally respond or ignore
```

## Dependencies

- **CheckIn data depends on evening responses**: The entire weekly review and check-in context system depends on users completing the evening check-in response flow. Making reminders passive breaks this data pipeline.
- **Morning task commitment depends on user response**: `committed_today` flags are only set when the user responds to morning check-in with task numbers. The afternoon and evening check-ins then use `committed_today == True` to find what to follow up on.
- **Avoidance tracking depends on evening responses**: `avoided_count` increments happen in `handle_evening_response`. Passive reminders mean no avoidance tracking.

## Patterns & Conventions

- All scheduled functions receive `telegram_app` as argument, use `telegram_app.bot.send_message(chat_id, ...)` to send
- Claude prompts live in `prompts/` directory as `.txt` files
- State is in-memory dict keyed by user_id
- Async SQLAlchemy with PostgreSQL
- Single authorized user (AUTHORIZED_USER_ID env var)

## Testing

No test files found in the repository.

## Related Systems

- Notion sync: `services/notion.py` — `push_task()` syncs task changes to Notion
- Voice transcription: `services/groq.py` — transcribes voice to text, then routes through same `process_input`

## Complexity Estimate

- **Files to modify:** 3-4 (`bot/scheduler.py`, `bot/handlers.py`, possibly `prompts/morning.txt`, `prompts/evening.txt`)
- **Estimated phases:** 1
- **Rationale:** Single-phase because the changes are focused: (1) stop setting mode on check-ins so responses are optional, (2) add day-of-week filtering to scheduler, (3) handle the empty check-in history case gracefully for weekly review. Design decision needed on whether to keep optional response handling or remove it entirely.
- **Key design decisions for planning:**
  - Should check-ins still *allow* responses (but not require them)? Or should the response handlers be removed?
  - If responses are optional, how should state["mode"] timeout work? (e.g., auto-reset to idle after N minutes, or never set mode at all)
  - Without evening responses, how does check-in history get populated for weekly reviews? Options: (a) auto-create CheckIn records from the scheduled message itself, (b) pull task status changes from DB instead, (c) accept that weekly review needs responses to work
  - Should `committed_today` be set automatically (e.g., all high-priority tasks) instead of requiring user selection?
