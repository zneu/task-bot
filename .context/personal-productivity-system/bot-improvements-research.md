# Bot Improvements — Research

## Overview

The accountability bot currently has: task CRUD via natural language + slash commands, brain dump extraction (text + voice), daily check-in loop (morning/afternoon/evening), weekly summary, Notion sync, and Claude-powered intent routing. This research documents what exists today, what's underutilized, and what concrete improvements would make the bot genuinely useful as a daily tool.

Companion document: `sticky-bot-research.md` covers behavioral science, UX patterns, and differentiated feature ideas in depth. This document focuses on what's buildable given the current architecture.

---

## Key Files

| File | Role | Lines |
|------|------|-------|
| `bot/handlers.py` | Message routing, check-in flows, brain dump confirmation | 324 |
| `bot/commands.py` | CRUD handlers, intent classification dispatch, command router | 502 |
| `bot/scheduler.py` | APScheduler cron jobs for morning/afternoon/evening/weekly | 174 |
| `bot/state.py` | In-memory user state (mode, history, task_map) | 36 |
| `main.py` | FastAPI app, Telegram bot setup, handler registration | 67 |
| `services/claude.py` | Claude API: chat, extract, classify_intent, checkin_context | 134 |
| `services/groq.py` | Groq Whisper voice transcription | 13 |
| `services/notion.py` | One-way Notion push/archive for tasks | 57 |
| `database/models.py` | SQLAlchemy: Task, CheckIn, Capture, Person | 59 |
| `database/connection.py` | Async PostgreSQL engine + session factory | 36 |
| `prompts/brain_dump.txt` | Extraction system prompt | 16 |
| `prompts/morning.txt` | Morning check-in prompt | ~20 |
| `prompts/afternoon.txt` | Afternoon nudge prompt | ~15 |
| `prompts/evening.txt` | Evening recap prompt | ~20 |

---

## Architecture

### Current System Flow

```
User sends text/voice
  → Telegram Bot API (polling mode)
    → Authorization check (single user)
    → State mode check: brain_dump_confirm | morning | evening
    → Command routing:
        Fast path: prefix match (list, done, add, etc.) → handler → DB + Notion
        Slow path: Claude classify_intent → structured JSON → handler → DB + Notion
        Fallback: Claude chat (conversational response)
    → Voice: Groq transcribe → process as text

Scheduler (APScheduler cron):
  → morning_checkin: query open tasks → Claude formats → send + set mode=morning
  → afternoon_checkin: query committed tasks → Claude nudge → send
  → evening_checkin: query committed tasks → Claude recap prompt → set mode=evening
  → weekly_summary: query 7-day checkins → Claude synthesize → send
```

### Data Layer

- **PostgreSQL** (Railway): Task, CheckIn, Capture, Person tables
- **In-memory state**: user mode, conversation history (10 msg), task_map, pending items
- **Notion**: one-way push sync for tasks only (no read-back, no people/checkins)

### External Services

| Service | Purpose | Model/Tier |
|---------|---------|------------|
| Claude API | Chat, extraction, intent classification, check-in context | claude-sonnet-4-6 |
| Groq API | Voice transcription | Whisper |
| Notion API | Task sync (create/update/archive) | - |
| Telegram Bot API | All user interaction | Polling mode |

---

## Data Flow

### Task Lifecycle

```
Created via:
  - Brain dump (/dump) → Claude extract → confirm → INSERT + Notion push
  - Direct add (/add or NL) → INSERT + Notion push

Updated via:
  - /done, /doing, /edit (or NL) → UPDATE + Notion push
  - Morning commitment → committed_today=True + Notion push
  - Evening check-in → Claude parses status → UPDATE + Notion push

Deleted via:
  - /delete → DELETE from DB + Notion archive
```

### Check-In Lifecycle

```
Scheduler fires cron job
  → Query tasks from DB
  → Format with Claude (using system prompt from prompts/)
  → Send message to user
  → Set state mode (morning/evening)
  → User responds
  → Handler processes response
  → Update DB + Notion
  → Reset mode to idle
```

### State Persistence

- **Persistent**: Tasks, CheckIns, Captures, People (PostgreSQL)
- **Ephemeral**: User state, conversation history, task_map (in-memory, lost on restart)

---

## Key Functions/Components

### Scheduler Check-ins (`bot/scheduler.py`)

| Function | Location | Purpose | Calls | Inputs | Outputs |
|----------|----------|---------|-------|--------|---------|
| `morning_checkin` | L24-L65 | List open tasks, ask for commitment | DB query, Claude, Telegram send | None (cron) | Message + state=morning |
| `afternoon_checkin` | L68-L100 | Nudge on committed tasks | DB query, Claude, Telegram send | None (cron) | Message only |
| `evening_checkin` | L103-L140 | Ask for day recap | DB query, Claude, Telegram send | None (cron) | Message + state=evening |
| `weekly_summary` | L143-L170 | Synthesize week | CheckIn query, Claude | None (cron) | Message only |

### Intent Classification (`services/claude.py:classify_intent`)

- **Location**: L49-L108
- **Purpose**: Parse natural language into structured command JSON
- **Inputs**: user text, project names list, task_map
- **Outputs**: `{"intent": "...", ...params}`
- **Context provided**: today's date, project list, task numbers
- **Not provided**: task titles, people names, due dates, completion history

### Voice Pipeline

```
handle_voice (handlers.py:298)
  → context.bot.get_file → download_as_bytearray
  → transcribe_voice (groq.py) — synchronous, blocks
  → reply "I heard: {text}"
  → process_input(text) — same as text flow
```

---

## Dependencies

### Internal Dependency Graph

```
main.py
  ├── bot/handlers.py
  │     ├── services/claude.py (chat)
  │     ├── services/groq.py (transcribe)
  │     ├── bot/state.py
  │     └── bot/commands.py (route_command)
  │           ├── services/claude.py (classify_intent)
  │           ├── services/notion.py (push_task, archive_task)
  │           ├── database/models.py
  │           └── database/connection.py
  ├── bot/scheduler.py
  │     ├── services/claude.py (get_response, get_checkin_context)
  │     ├── database/models.py
  │     └── database/connection.py
  └── database/connection.py (init_db)
```

### External Dependencies

```
python-telegram-bot >=21.0  — Bot API, handlers, filters
anthropic                   — Claude API client
groq                        — Whisper transcription
sqlalchemy[asyncio]         — ORM + async
asyncpg                     — PostgreSQL async driver
notion-client               — Notion API
apscheduler                 — Cron scheduling
pytz                        — Timezone handling
fastapi + uvicorn           — HTTP server
python-dotenv               — .env loading
```

No version pinning in requirements.txt.

---

## Patterns & Conventions

1. **Lazy imports** — Heavy imports (database, notion) done inside functions
2. **State machine routing** — `state["mode"]` determines handler
3. **Hybrid command routing** — Prefix match (fast, free) → Claude classification (slow, costs tokens)
4. **Synchronous Notion calls** — `push_task()` blocks inside async handlers
5. **Session pattern** — `async with AsyncSessionLocal() as session:` with explicit commit
6. **UUID primary keys** — Row-number task_map for user-facing references
7. **Single-user auth** — `AUTHORIZED_USER_ID` env var, checked in every handler
8. **System prompts in files** — `prompts/` directory for Claude context

---

## Testing

No automated tests exist. All testing is manual via Telegram messages.

---

## Related Systems

- **Notion database** — One-way push sync for tasks
- **Railway PostgreSQL** — Primary data store
- **Hetzner VPS** — Deployment target (systemd service)

---

## What's Underutilized in the Current System

### Telegram API Features Not Used

| Feature | What It Does | Current Use |
|---------|-------------|-------------|
| Inline keyboards | Buttons below messages, edit in-place | Not used |
| Bot reactions | React to messages with emoji | Not used |
| Message pinning | Pin commitments to chat top | Not used |
| Message editing | Update existing messages in-place | Not used |
| Inline mode (`@bot query`) | Capture tasks from any chat | Not used |
| Reply keyboards | Replace keyboard with options | Not used |
| Reply-to threading | Reply to specific messages | Not used |
| Formatted text (HTML/Markdown) | Bold, italic, monospace | Minimal |

### Data Not Being Leveraged

| Data | Available | Used For |
|------|-----------|----------|
| Task completion timestamps | updated_at in DB | Nothing |
| Avoided count per task | avoided_count field | Evening callout only |
| Brain dump raw text | Capture.raw_text | Never read back |
| Check-in summaries | CheckIn.summary | Weekly summary context only |
| Person follow_up_date | Person.follow_up_date | Never surfaced proactively |
| Task creation patterns | created_at timestamps | Nothing |
| Commitment accuracy | committed_today + status | Not tracked over time |

### Claude Context Not Provided

The intent classifier (`classify_intent`) currently receives: today's date, project names, task numbers. It does NOT receive:
- Task titles (can't say "mark the groceries task done")
- People names (can't say "what do I need to do for Sarah?")
- Due dates (can't say "what's due this week?")
- Task statuses (can't ask "what am I working on?")

The chat fallback (`chat()`) receives: only conversation history (last 10 messages). It has no knowledge of tasks, projects, people, or check-in history.

---

## Improvement Categories

### Category 1: Telegram UX (Inline Keyboards + Reactions)

**Current state**: All interaction is text-based. User types commands or natural language, bot replies with text.

**What exists in the Telegram API**: Inline keyboards (buttons below messages), message editing (update messages in-place), bot reactions (emoji on messages), message pinning.

**Key files affected**: `bot/commands.py` (all cmd_* functions that send replies), `bot/scheduler.py` (check-in messages), `bot/handlers.py` (confirmation flows).

**Specific opportunities**:
- `cmd_list` could return tasks with inline `[Done]` `[Doing]` buttons per task instead of requiring "done 3"
- Morning check-in could use checkboxes (inline keyboard) instead of "send me numbers"
- Brain dump confirmation could use `[Save]` `[Discard]` buttons instead of yes/no text
- Task completion could trigger a bot reaction (fire emoji) instead of a text reply
- Morning commitments could be pinned to chat

### Category 2: Smarter Claude Context

**Current state**: Intent classifier gets project names + today's date. Chat gets conversation history only.

**What could be provided**:
- Task titles + statuses → enables "mark the groceries task done" and "what am I working on?"
- People names → enables "what do I need to do for Sarah?"
- Due dates → enables "what's due this week?"
- Check-in history → enables "how did I do yesterday?" in chat

**Key files affected**: `services/claude.py` (classify_intent system prompt, chat system prompt), `bot/commands.py` (_classify_and_dispatch to build richer context).

**Token cost**: Adding task titles/statuses to the classify_intent prompt adds ~50-200 tokens depending on task count. Manageable for <100 tasks.

### Category 3: Proactive Surfacing

**Current state**: Bot only acts on cron schedule (morning/afternoon/evening/weekly) or in response to user messages. Never surfaces things on its own based on data.

**What could be proactive**:
- Person follow-up dates: "You were going to follow up with Sarah by today"
- Approaching due dates: "3 tasks due this week" surfaced Monday morning
- Stale tasks: Weekly "these 4 tasks are 2+ weeks old — still want them?"
- Waiting-for tracking: "You've been waiting on Mike for 5 days"
- Inactivity: "Haven't heard from you in 2 days — everything okay?"

**Key files affected**: `bot/scheduler.py` (new cron jobs or enhancements to existing ones), `database/models.py` (may need new fields for tracking).

### Category 4: Richer Check-in Flows

**Current state**: Morning asks for numbers, evening asks for free-text status. Weekly is a single Claude summary message.

**What could be improved**:
- Morning: Include due dates and priority in the task list. Ask "how's your energy?" to suggest task ordering.
- Evening: Show what was committed vs. what was done side-by-side. Track commitment accuracy over time.
- Weekly: Interactive review (one question at a time, not a wall of text). Surface patterns: completion rate trends, stale projects, people mentioned without tasks.

**Key files affected**: `bot/scheduler.py`, `prompts/*.txt`, `services/claude.py`, possibly new DB queries.

### Category 5: State Persistence

**Current state**: All user state is in-memory Python dicts. Lost on restart. Includes: current mode, conversation history, task_map, committed_task_ids, pending brain dump items.

**Impact**: After every deploy/restart, user loses: which tasks map to which numbers, pending confirmations, conversation context, morning commitments.

**Fix**: Store state in PostgreSQL (new UserState table with JSON column) or Redis. Load on first interaction, save after mutations.

**Key files affected**: `bot/state.py` (rewrite to use DB), `database/models.py` (new model).

### Category 6: Follow-Up / People Intelligence

**Current state**: People are extracted from brain dumps with name, context, follow_up_action, follow_up_date. They can be listed with `/people`. That's it.

**What's missing**:
- follow_up_date is never checked proactively
- No way to add/edit people directly
- No connection between people and tasks
- No "what do I need to discuss with Mike?" query
- No notification when follow-up date arrives

**Key files affected**: `bot/scheduler.py` (new proactive check), `bot/commands.py` (people management commands), `services/claude.py` (add people names to context).

### Category 7: Task Intelligence

**Current state**: Tasks have status, priority, project, due_date, avoided_count. The evening check-in flags chronic avoidance (3+ times). No other intelligence.

**What could exist**:
- Recurring tasks (weekly, daily, custom)
- Subtask decomposition ("This seems complex — want to break it into smaller tasks?")
- Due date warnings surfaced proactively
- Completion time estimation based on historical data
- Priority auto-adjustment based on due date proximity
- "Waiting for" status with follow-up surfacing

**Key files affected**: `database/models.py` (new fields: recurrence, parent_task_id, waiting_on), `bot/scheduler.py`, `bot/commands.py`.

---

## Complexity Estimate

Each category above is independently buildable. Recommended phasing:

- **Phase 1: Smarter Claude context** — 2 files, small change, high impact. Give classify_intent task titles/statuses/people names so NL commands actually work well.
- **Phase 2: Inline keyboards** — 3 files, medium change. Add buttons to list output and check-in messages. Biggest UX improvement.
- **Phase 3: Proactive surfacing** — 2 files, medium change. Due date warnings, follow-up date alerts, stale task cleanup.
- **Phase 4: State persistence** — 2 files, medium change. Move state to DB so restarts don't lose context.
- **Phase 5: Richer check-ins** — 3 files, medium change. Interactive weekly review, energy tracking, commitment accuracy.
- **Phase 6: People intelligence** — 3 files, medium change. CRUD for people, connect to tasks, proactive follow-up.
- **Phase 7: Task intelligence** — 3 files, larger change. Recurring tasks, subtasks, waiting-for tracking.

**Files to modify per phase**: 2-3
**Estimated total phases**: 7 (each independently shippable)
**Rationale**: Each phase adds standalone value. Order is by impact-to-effort ratio. Phases 1-3 are the highest ROI — they make the existing features work significantly better with minimal code changes.
