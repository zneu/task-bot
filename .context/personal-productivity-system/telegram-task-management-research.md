# Telegram Task Management — Research

## Overview

The accountability bot currently has two ways to interact with tasks: (1) brain dump extraction, which always calls Claude API to parse free-text into structured items, and (2) scheduled check-ins that present tasks and handle morning commitment / evening status flows. There is no way to directly list, view, edit, mark done, delete, or add individual tasks from Telegram. All task management beyond brain dumps requires using Notion or the database directly.

The goal is full CRUD task management from Telegram: list, filter, view, edit, mark done/in-progress, delete, add single tasks, and query by project or person — all without leaving the chat.

---

## Key Files

| File | Role | Lines |
|------|------|-------|
| `bot/handlers.py` | All message routing and response logic | 318 |
| `bot/state.py` | In-memory user state (mode, history, pending items) | 36 |
| `bot/scheduler.py` | Scheduled check-ins (morning/afternoon/evening/weekly) | 175 |
| `main.py` | FastAPI app, Telegram bot setup, handler registration | 64 |
| `database/models.py` | SQLAlchemy models: Task, CheckIn, Capture, Person | 60 |
| `database/connection.py` | Async engine, session factory, init_db | 36 |
| `services/claude.py` | Claude API: chat, extract_from_dump, get_checkin_context | 74 |
| `services/notion.py` | One-way Notion push sync for tasks | 48 |
| `prompts/brain_dump.txt` | Brain dump extraction system prompt | 16 |

---

## Architecture

### Current Message Flow

```
User sends text message
  → handle_message() [bot/handlers.py:286]
    → ping check (line 291)
    → process_input() [line 44]
      → mode check: brain_dump_confirm → handle_confirmation()
      → mode check: morning → handle_morning_response()
      → mode check: evening → handle_evening_response()
      → DEFAULT: extract_from_dump() via Claude API [line 66]
        → if items found: show + ask confirm
        → if no items: chat() via Claude API [line 79]
```

### Key Problem: No Command Routing

Every non-ping text message in idle mode goes through `extract_from_dump()` — a Claude API call. There is no command parser. Messages like "list tasks", "done 3", "what do I need to do for Thrown?" all hit the extraction endpoint. This means:

1. Every casual message costs an API call to the extraction endpoint
2. There's no way to issue direct commands (list, done, edit, delete, add)
3. Natural language queries ("show my Thrown tasks") require a new routing layer

### State Machine

Current modes in `bot/state.py`:
- `idle` — default, routes to brain dump extraction
- `brain_dump_confirm` — awaiting yes/no on extracted items
- `morning` — awaiting task number selection for morning commitment
- `evening` — awaiting status update on committed tasks

No modes exist for task editing, viewing, or management.

### Handler Registration

`main.py` registers two handlers on the Telegram Application:
1. `MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)` — all non-command text
2. `MessageHandler(filters.VOICE, handle_voice)` — voice messages

The `~filters.COMMAND` filter means `/slash` commands are already excluded from the text handler but no `CommandHandler` instances are registered. python-telegram-bot supports `CommandHandler` natively for `/command` style messages.

---

## Data Flow

### Task Lifecycle (Current)

```
Brain dump text → Claude extract_from_dump() → JSON items
  → user confirms → save_items() → Postgres INSERT + Notion push
  → tasks sit in DB with status="not_started"

Morning check-in → user picks numbers → committed_today=True + Notion push
Afternoon check-in → nudge only, no DB changes
Evening check-in → user reports → Claude parses status → UPDATE task status + Notion push
```

### Task Fields Available for Management

From `database/models.py` Task model:
- `id` — UUID string (primary key)
- `title` — task name (String, not null)
- `status` — "not_started" | "in_progress" | "done" | "avoided" (String)
- `priority` — "high" | "medium" | "low" (String)
- `project` — optional project name (String, nullable)
- `due_date` — optional datetime (TIMESTAMP, nullable)
- `committed_today` — boolean
- `notes` — optional text (Text, nullable)
- `notion_id` — Notion page ID for sync (String, nullable)
- `avoided_count` — integer counter
- `created_at`, `updated_at` — timestamps

### Notion Sync

`services/notion.py:push_task()` handles both create (no notion_id) and update (has notion_id). Syncs: Name, Status, Priority, Committed Today, Project, Notes, Due Date. Deletion is not implemented — Notion pages would need to be archived via the API.

---

## Key Functions/Components

### process_input() [bot/handlers.py:44]
- **Purpose:** Central routing for all text input
- **Inputs:** text string, Update object, source string
- **Flow:** Mode check → extract_from_dump → chat fallback
- **Called by:** handle_message(), handle_voice()
- **Calls:** handle_confirmation(), handle_morning_response(), handle_evening_response(), extract_from_dump(), chat()
- **This is the function that needs a command routing layer inserted before the extraction call**

### save_items() [bot/handlers.py:102]
- **Purpose:** Persist extracted brain dump items to Postgres + Notion
- **Creates:** Task, Person, Capture records
- **Pattern:** session.add → flush → push_task → commit

### push_task() [services/notion.py:11]
- **Purpose:** Create or update Notion page for a task
- **Handles:** Both insert (no notion_id) and update (has notion_id)
- **Does not handle:** Deletion/archiving

---

## Dependencies

### Internal
- `bot/handlers.py` imports from: `services/claude`, `services/groq`, `bot/state`
- `bot/handlers.py` lazy-imports from: `database/connection`, `database/models`, `services/notion`
- `bot/scheduler.py` imports from: `database/connection`, `database/models`, `services/claude`
- `main.py` imports from: `bot/handlers`, `database/connection`, `bot/scheduler`

### External (relevant to this feature)
- `python-telegram-bot` — supports `CommandHandler` for `/slash` commands, `MessageHandler` for free text
- `sqlalchemy` — async queries with `select`, `update`, `delete`
- `notion-client` — `pages.update()` for archiving (set `archived=True`)

---

## Patterns & Conventions

1. **Lazy imports** — Heavy imports (database, notion) done inside functions, not at module top
2. **State-based routing** — `state["mode"]` determines which handler processes input
3. **No command framework** — Everything routes through `process_input()` as free text
4. **Sync Notion calls** — `push_task()` is synchronous (blocking), called inside async handlers
5. **Session pattern** — `async with AsyncSessionLocal() as session:` with explicit commit
6. **UUID primary keys** — `id[:8]` used as short display IDs in morning check-in

---

## Testing

No automated tests exist. All testing is manual via Telegram messages.

---

## Related Systems

- **Notion database** — Tasks are synced one-way. Any management commands that change task state must also sync to Notion.
- **Scheduler** — Morning/afternoon/evening check-ins query tasks from DB. New management commands don't affect the scheduler directly but the data they modify will appear in check-ins.
- **Person model** — People extracted from brain dumps. Currently no management commands for people, but querying "who do I need to follow up with?" would need to query the Person table.

---

## What Needs to Be Built

### 1. Command Router
A layer in `process_input()` (or before it) that intercepts known commands before hitting Claude API. Two approaches:

**Option A: Slash commands** (`/list`, `/done 3`, `/edit 3 title: New Title`)
- Uses python-telegram-bot's `CommandHandler` — clean separation
- Requires registering each handler in `main.py`
- User must type `/` prefix

**Option B: Natural language prefix matching** (`list`, `done 3`, `tasks for Thrown`)
- Parsed in `process_input()` before extraction
- No `/` prefix needed — more natural
- Risk of false positives (message starting with "list" that's actually a brain dump)

**Option C: Hybrid** — Support both `/list` and plain `list`. Register `CommandHandler` for slash versions, add prefix matching in `process_input()` for natural versions.

### 2. Task CRUD Operations (new module: `bot/commands.py`)

| Command | Action | DB Operation | Notion Sync |
|---------|--------|-------------|-------------|
| `list` / `tasks` | Show all open tasks grouped by project | SELECT where status != done | No |
| `list all` | Show all tasks including done | SELECT all | No |
| `list [project]` | Filter by project | SELECT where project = X | No |
| `done [n]` | Mark task complete | UPDATE status = "done" | Yes |
| `doing [n]` | Mark task in progress | UPDATE status = "in_progress" | Yes |
| `edit [n] title: X` | Edit task title | UPDATE title | Yes |
| `edit [n] priority: high` | Edit priority | UPDATE priority | Yes |
| `edit [n] project: X` | Edit project | UPDATE project | Yes |
| `edit [n] due: YYYY-MM-DD` | Set due date | UPDATE due_date | Yes |
| `edit [n] notes: X` | Set notes | UPDATE notes | Yes |
| `delete [n]` | Remove task | DELETE from DB | Archive in Notion |
| `add [title]` | Quick-add single task | INSERT | Yes |
| `add [title] p:high proj:X` | Add with metadata | INSERT | Yes |
| `people` | List people with follow-ups | SELECT from people | No |
| `ask [query]` | Natural language query via Claude with DB context | SELECT + Claude | No |

### 3. Task Numbering System

Current problem: Tasks use UUID primary keys. The morning check-in creates a temporary numbered mapping (`_morning_tasks` in state), but this is ephemeral and only exists during the morning flow.

For management commands, tasks need persistent short identifiers. Options:
- **Row number** — Number tasks 1-N on every list display, store mapping in state. Mapping changes whenever tasks are added/deleted.
- **Sequential integer ID** — Add an auto-increment `seq` column. Permanent, but gaps appear on deletion.
- **Short UUID** — Use `id[:8]` as already done in morning check-in. Permanent, no gaps, but less readable.

Row number approach is simplest — regenerate the mapping on each `list` call and store in state. Commands like `done 3` reference the last-displayed list.

### 4. Smart Routing / Brain Dump Detection

Currently every idle message goes to `extract_from_dump()`. With commands handled first, the remaining question is: when should remaining free text go to extraction vs. plain chat?

Options:
- **Explicit trigger** — Only extract on `/dump` command or "dump:" prefix. Everything else is chat.
- **Length heuristic** — Short messages (< 20 words) → chat. Long messages → extraction.
- **Let Claude decide** — Add a cheap classification step (or use the extraction response — if empty, fall back to chat). This is the current behavior.
- **Keyword detection** — Messages containing "I need to", "remind me", "follow up" etc. → extraction.

The explicit `/dump` trigger is simplest and most predictable. Current behavior (always extract) wastes API calls on "hey" and "thanks".

### 5. Notion Deletion

`services/notion.py` has no delete/archive function. The Notion API supports archiving pages:
```python
notion.pages.update(page_id=notion_id, archived=True)
```

### 6. Natural Language Queries

"What do I need to do for Thrown?" or "Who do I need to follow up with?" — these require:
1. Querying the DB for relevant data
2. Passing results to Claude with a system prompt
3. Returning Claude's natural language response

This is a new pattern — DB query → Claude interpretation → response.

---

## Complexity Estimate

- **Files to modify:** 6 (bot/handlers.py, bot/state.py, bot/commands.py [new], main.py, services/notion.py, services/claude.py)
- **Estimated phases:** 2
- **Rationale:** Phase 1 covers the command router + all CRUD operations (list, done, edit, delete, add) which are straightforward DB operations. Phase 2 covers natural language queries ("what do I need to do for Thrown?") which require a new Claude integration pattern. Separating these allows testing basic management before adding the AI query layer.
- **Phase boundaries:**
  - **Phase 1:** Command router, task numbering, list/done/doing/edit/delete/add commands, Notion archive, smart routing (explicit /dump), people list
  - **Phase 2:** Natural language queries with DB context ("ask" command), smart query detection for conversational questions about tasks/people
