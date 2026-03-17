# Telegram Task Management — Phase 1: Command Router, Task CRUD, Smart Routing

- **Branch:** `feature/telegram-task-management`
- **Date:** 2026-03-14
- **Research:** `.context/personal-productivity-system/telegram-task-management-research.md`

## Goal

Add direct task management commands to the Telegram bot — list, add, done, doing, edit, delete, people — with a hybrid command router (slash + plain text), row-number task references, and explicit `/dump` trigger for brain dumps.

## Success Criteria

- [ ] `/list` and `list` show open tasks grouped by project with row numbers
- [ ] `/list all` shows all tasks including done
- [ ] `/list [project]` filters by project name
- [ ] `/done N` and `done N` mark task N as done + Notion sync
- [ ] `/doing N` marks task N as in_progress + Notion sync
- [ ] `/add [title]` creates a task with optional `p:high proj:X due:YYYY-MM-DD`
- [ ] `/edit N field: value` updates task field + Notion sync
- [ ] `/delete N` removes task from DB + archives in Notion
- [ ] `/people` lists people with follow-up actions
- [ ] `/dump [text]` triggers brain dump extraction (old default behavior)
- [ ] Plain text messages that aren't commands go to chat (no extraction API call)
- [ ] Task number mapping persists in state between commands
- [ ] All status-changing commands sync to Notion

## Scope Boundaries

### In Scope
- Command router (hybrid slash + plain text prefix matching)
- New `bot/commands.py` module with all CRUD handlers
- Task numbering via row-number mapping in state
- Smart routing: `/dump` for extraction, everything else → chat
- Notion archive function for delete
- State additions for task mapping
- Handler registration in `main.py`

### Out of Scope
- Natural language queries ("what do I need to do for Thrown?") — Phase 2
- Smart query detection / AI routing — Phase 2
- `ask` command — Phase 2
- Tests (no test infrastructure exists)
- Person CRUD (only listing)
- Editing people records

---

## Implementation Steps

### Phase 1.1: State Additions

#### Step 1.1.1: Add task_map to default state
- **File:** `bot/state.py` **Lines:** L6-L11
- **Change:** Add `task_map` dict to default state for row-number → task_id mapping
- **Before:**
```python
user_states[user_id] = {
    "mode": "idle",
    "committed_task_ids": [],
    "conversation_history": [],
    "pending_items": None,
}
```
- **After:**
```python
user_states[user_id] = {
    "mode": "idle",
    "committed_task_ids": [],
    "conversation_history": [],
    "pending_items": None,
    "task_map": {},
}
```
- **Verification:** No runtime errors — state dict gains new key

---

### Phase 1.2: Notion Archive Function

#### Step 1.2.1: Add archive_task to services/notion.py
- **File:** `services/notion.py` **Lines:** After L47 (end of file)
- **Change:** Add `archive_task()` function that sets `archived=True` on a Notion page
- **Before:** (end of file)
- **After:**
```python


def archive_task(notion_id: str) -> bool:
    """Archive a task in Notion. Returns True on success."""
    try:
        notion.pages.update(page_id=notion_id, archived=True)
        return True
    except Exception:
        logger.exception(f"Failed to archive Notion page '{notion_id}'")
        return False
```
- **Verification:** Function exists and is importable

---

### Phase 1.3: Command Handlers Module

#### Step 1.3.1: Create bot/commands.py with all CRUD command handlers
- **File:** `bot/commands.py` **(new file)**
- **Change:** Create module with all task management command functions. Each command receives the raw argument string and the Telegram Update object.

The file structure:

```python
import logging
import re
from datetime import datetime, timezone
from telegram import Update

logger = logging.getLogger(__name__)


async def cmd_list(args: str, update: Update, state: dict):
    """List tasks. Usage: list [all|project_name]"""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        query = select(Task).order_by(Task.project, Task.created_at)

        show_all = args.strip().lower() == "all"
        project_filter = args.strip() if args.strip() and not show_all else None

        if not show_all:
            query = query.where(Task.status.notin_(["done"]))

        if project_filter:
            query = query.where(Task.project.ilike(f"%{project_filter}%"))

        result = await session.execute(query)
        tasks = result.scalars().all()

    if not tasks:
        label = f" for '{project_filter}'" if project_filter else ""
        await update.message.reply_text(f"No tasks found{label}.")
        return

    # Build numbered list grouped by project
    task_map = {}
    lines = []
    n = 1
    current_project = "__UNSET__"

    for task in tasks:
        proj = task.project or "No Project"
        if proj != current_project:
            if lines:
                lines.append("")
            lines.append(f"[{proj}]")
            current_project = proj

        status_icon = {"not_started": "○", "in_progress": "◐", "done": "●", "avoided": "⊘"}.get(task.status, "○")
        pri_icon = {"high": "!", "medium": "", "low": "~"}.get(task.priority, "")
        due = ""
        if task.due_date:
            due = f" (due {task.due_date.strftime('%m/%d')})"

        lines.append(f"  {n}. {status_icon} {pri_icon}{task.title}{due}")
        task_map[str(n)] = task.id
        n += 1

    state["task_map"] = task_map

    header = "All tasks:" if show_all else "Open tasks:"
    await update.message.reply_text(f"{header}\n\n" + "\n".join(lines))


async def cmd_done(args: str, update: Update, state: dict):
    """Mark task as done. Usage: done N"""
    await _set_status(args, "done", update, state)


async def cmd_doing(args: str, update: Update, state: dict):
    """Mark task as in progress. Usage: doing N"""
    await _set_status(args, "in_progress", update, state)


async def _set_status(args: str, status: str, update: Update, state: dict):
    """Set task status by row number."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    num = args.strip()
    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await update.message.reply_text(f"No task #{num}. Use /list first.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            await update.message.reply_text("Task not found in database.")
            return

        task.status = status
        push_task(task)
        await session.commit()
        label = "Done" if status == "done" else "In progress"
        await update.message.reply_text(f"{label}: {task.title}")


async def cmd_add(args: str, update: Update, state: dict):
    """Quick-add a task. Usage: add Buy groceries p:high proj:Home due:2026-03-15"""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task

    if not args.strip():
        await update.message.reply_text("Usage: /add Task title p:high proj:Project due:YYYY-MM-DD")
        return

    # Parse inline metadata
    text = args.strip()
    priority = "medium"
    project = None
    due_date = None

    # Extract p:value
    m = re.search(r'\bp:(\w+)', text)
    if m:
        priority = m.group(1).lower()
        text = text[:m.start()] + text[m.end():]

    # Extract proj:value (supports quoted: proj:"My Project")
    m = re.search(r'\bproj:(?:"([^"]+)"|(\S+))', text)
    if m:
        project = m.group(1) or m.group(2)
        text = text[:m.start()] + text[m.end():]

    # Extract due:YYYY-MM-DD
    m = re.search(r'\bdue:(\d{4}-\d{2}-\d{2})', text)
    if m:
        try:
            due_date = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        text = text[:m.start()] + text[m.end():]

    title = text.strip()
    if not title:
        await update.message.reply_text("Task needs a title.")
        return

    async with AsyncSessionLocal() as session:
        task = Task(
            title=title,
            priority=priority,
            project=project,
            due_date=due_date,
            status="not_started",
        )
        session.add(task)
        await session.flush()
        notion_id = push_task(task)
        if notion_id:
            task.notion_id = notion_id
        await session.commit()

    parts = [f"Added: {title}"]
    if priority != "medium":
        parts.append(f"  Priority: {priority}")
    if project:
        parts.append(f"  Project: {project}")
    if due_date:
        parts.append(f"  Due: {due_date.strftime('%Y-%m-%d')}")
    await update.message.reply_text("\n".join(parts))


async def cmd_edit(args: str, update: Update, state: dict):
    """Edit a task field. Usage: edit N title: New Title"""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    # Parse: first token is the number, rest is "field: value"
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /edit N field: value\nFields: title, priority, project, due, notes")
        return

    num = parts[0]
    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await update.message.reply_text(f"No task #{num}. Use /list first.")
        return

    # Parse field: value
    field_match = re.match(r'(\w+):\s*(.+)', parts[1])
    if not field_match:
        await update.message.reply_text("Format: field: value\nFields: title, priority, project, due, notes")
        return

    field = field_match.group(1).lower()
    value = field_match.group(2).strip()

    allowed = {"title", "priority", "project", "due", "notes"}
    if field not in allowed:
        await update.message.reply_text(f"Unknown field '{field}'. Allowed: {', '.join(sorted(allowed))}")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            await update.message.reply_text("Task not found in database.")
            return

        if field == "title":
            task.title = value
        elif field == "priority":
            if value.lower() not in ("high", "medium", "low"):
                await update.message.reply_text("Priority must be high, medium, or low.")
                return
            task.priority = value.lower()
        elif field == "project":
            task.project = value if value.lower() != "none" else None
        elif field == "due":
            if value.lower() == "none":
                task.due_date = None
            else:
                try:
                    task.due_date = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    await update.message.reply_text("Date format: YYYY-MM-DD (or 'none' to clear)")
                    return
        elif field == "notes":
            task.notes = value if value.lower() != "none" else None

        push_task(task)
        await session.commit()
        await update.message.reply_text(f"Updated #{num} {field} → {value}")


async def cmd_delete(args: str, update: Update, state: dict):
    """Delete a task. Usage: delete N"""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import archive_task
    from sqlalchemy import select

    num = args.strip()
    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await update.message.reply_text(f"No task #{num}. Use /list first.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            await update.message.reply_text("Task not found in database.")
            return

        title = task.title
        notion_id = task.notion_id

        await session.delete(task)
        await session.commit()

    if notion_id:
        archive_task(notion_id)

    # Remove from task_map
    state.get("task_map", {}).pop(num, None)

    await update.message.reply_text(f"Deleted: {title}")


async def cmd_people(args: str, update: Update, state: dict):
    """List people with follow-up actions."""
    from database.connection import AsyncSessionLocal
    from database.models import Person
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Person).order_by(Person.created_at.desc())
        )
        people = result.scalars().all()

    if not people:
        await update.message.reply_text("No people tracked.")
        return

    lines = ["People:"]
    for p in people:
        action = f" → {p.follow_up_action}" if p.follow_up_action else ""
        ctx = f" ({p.context})" if p.context else ""
        due = ""
        if p.follow_up_date:
            due = f" [by {p.follow_up_date.strftime('%m/%d')}]"
        lines.append(f"  - {p.name}{ctx}{action}{due}")

    await update.message.reply_text("\n".join(lines))
```

- **Verification:** File imports cleanly; each function has correct signature `(args, update, state)`

---

### Phase 1.4: Command Router

#### Step 1.4.1: Add command parsing to process_input in handlers.py
- **File:** `bot/handlers.py` **Lines:** L64-L82
- **Change:** Replace the extraction-as-default block with a command router. After mode checks (L50-L62), try to match commands before falling through to chat.
- **Before:**
```python
    # Try to extract structured items
    try:
        items = extract_from_dump(text)
        has_items = any(items.get(k) for k in ["tasks", "people", "ideas", "commitments"])
    except Exception:
        logger.exception("Extraction failed, falling back to chat")
        has_items = False
        items = None

    if has_items:
        set_pending(user_id, items)
        formatted = format_extracted(items)
        await update.message.reply_text(formatted)
    else:
        # Nothing to extract — just chat
        response = chat(text, state["conversation_history"])
        add_to_history(user_id, "user", text)
        add_to_history(user_id, "assistant", response)
        await update.message.reply_text(response)
```
- **After:**
```python
    # Try command routing first
    from bot.commands import route_command
    handled = await route_command(text, update, state)
    if handled:
        return

    # Default: chat
    response = chat(text, state["conversation_history"])
    add_to_history(user_id, "user", text)
    add_to_history(user_id, "assistant", response)
    await update.message.reply_text(response)
```
- **Verification:** Plain text like "hey" goes to chat without API extraction call. Commands like "list" route correctly.

#### Step 1.4.2: Add route_command function to bot/commands.py
- **File:** `bot/commands.py` **(append to top, after imports)**
- **Change:** Add `route_command()` that matches plain text prefixes to command handlers. Returns True if a command was matched.
- **Code:**
```python
# Command definitions: (prefix, handler, requires_args)
COMMANDS = [
    ("list", cmd_list, False),
    ("tasks", cmd_list, False),
    ("done", cmd_done, True),
    ("doing", cmd_doing, True),
    ("add", cmd_add, True),
    ("edit", cmd_edit, True),
    ("delete", cmd_delete, True),
    ("del", cmd_delete, True),
    ("people", cmd_people, False),
]


async def route_command(text: str, update: Update, state: dict) -> bool:
    """Try to match text as a command. Returns True if handled."""
    lower = text.strip().lower()

    # Explicit dump trigger
    if lower.startswith("dump ") or lower == "dump":
        raw = text.strip()[4:].strip() if lower.startswith("dump ") else ""
        if raw:
            await _handle_dump(raw, update)
        else:
            await update.message.reply_text("Send /dump followed by your brain dump text.")
        return True

    # Match known commands
    for prefix, handler, requires_args in COMMANDS:
        if lower == prefix or lower.startswith(prefix + " "):
            args = text.strip()[len(prefix):].strip()
            if requires_args and not args:
                await update.message.reply_text(f"Usage: /{prefix} ...")
                return True
            await handler(args, update, state)
            return True

    return False


async def _handle_dump(text: str, update: Update):
    """Process brain dump extraction (moved from default flow)."""
    from services.claude import extract_from_dump
    from bot.state import set_pending
    from bot.handlers import format_extracted

    user_id = str(update.effective_user.id)

    try:
        items = extract_from_dump(text)
        has_items = any(items.get(k) for k in ["tasks", "people", "ideas", "commitments"])
    except Exception:
        logger.exception("Extraction failed")
        await update.message.reply_text("Failed to process brain dump. Try again.")
        return

    if has_items:
        set_pending(user_id, items)
        formatted = format_extracted(items)
        await update.message.reply_text(formatted)
    else:
        await update.message.reply_text("Couldn't extract any items from that. Try being more specific.")
```

**Note:** `COMMANDS` list and `route_command` are defined near the top of the file (after imports, before the individual `cmd_*` functions). The full file order will be: imports → COMMANDS list → route_command → _handle_dump → cmd_list → cmd_done → ... → cmd_people.

- **Verification:** `route_command("list", ...)` returns True and calls `cmd_list`. `route_command("hello", ...)` returns False.

#### Step 1.4.3: Remove unused extract_from_dump import from handlers.py
- **File:** `bot/handlers.py` **Line:** L5
- **Change:** Remove `extract_from_dump` from import since extraction is now in `bot/commands.py:_handle_dump`
- **Before:**
```python
from services.claude import chat, extract_from_dump
```
- **After:**
```python
from services.claude import chat
```
- **Verification:** No import errors

---

### Phase 1.5: Slash Command Registration

#### Step 1.5.1: Register CommandHandlers in main.py
- **File:** `main.py` **Lines:** L9-L10, L36-L41
- **Change:** Import CommandHandler, register slash commands that delegate to a generic handler in `bot/handlers.py`
- **Before (imports):**
```python
from telegram.ext import Application, MessageHandler, filters
from bot.handlers import handle_message, handle_voice
```
- **After (imports):**
```python
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from bot.handlers import handle_message, handle_voice, handle_slash_command
```
- **Before (handler registration):**
```python
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    telegram_app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )
```
- **After (handler registration):**
```python
    # Slash commands
    for cmd in ["list", "tasks", "done", "doing", "add", "edit", "delete", "del", "people", "dump"]:
        telegram_app.add_handler(CommandHandler(cmd, handle_slash_command))

    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    telegram_app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )
```
- **Verification:** `/list` triggers the CommandHandler, not the MessageHandler

#### Step 1.5.2: Add handle_slash_command to bot/handlers.py
- **File:** `bot/handlers.py` **Lines:** After `handle_message` (L286-L295), before `handle_voice`
- **Change:** Add handler that extracts the command name and args, then routes to the command system
- **Code (insert after L295):**
```python

async def handle_slash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    # Reconstruct "command args" text from the slash command
    text = update.message.text  # e.g. "/list all"
    # Strip the leading slash to get "list all"
    command_text = text.lstrip("/")

    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    from bot.commands import route_command
    await route_command(command_text, update, state)

```
- **Verification:** `/done 3` calls `route_command("done 3", ...)` which matches the "done" command

---

### Phase 1.6: Remove ping special-case (optional cleanup)

The "ping" check at L291 in `handle_message` is fine to keep as-is. No change needed.

---

## File Summary

| File | Action | Key Changes |
|------|--------|------------|
| `bot/state.py` | Modify | Add `task_map` to default state |
| `services/notion.py` | Modify | Add `archive_task()` function |
| `bot/commands.py` | **Create** | All CRUD handlers + `route_command()` + `_handle_dump()` |
| `bot/handlers.py` | Modify | Replace extraction default with `route_command`, add `handle_slash_command`, remove unused import |
| `main.py` | Modify | Register CommandHandlers for slash commands |

## Implementation Order

1. `bot/state.py` — Add task_map (no dependencies)
2. `services/notion.py` — Add archive_task (no dependencies)
3. `bot/commands.py` — Create full module (depends on state + notion)
4. `bot/handlers.py` — Wire up routing (depends on commands.py)
5. `main.py` — Register slash commands (depends on handlers.py)

## Testing Plan

### Manual Testing (via Telegram)

- [ ] Send "list" → shows open tasks numbered by project
- [ ] Send "/list" → same result
- [ ] Send "/list all" → includes done tasks
- [ ] Send "/list Thrown" → filters to Thrown project
- [ ] Send "list" then "done 1" → marks first task done, Notion updates
- [ ] Send "doing 2" → marks task in_progress, Notion updates
- [ ] Send "/add Buy groceries p:high proj:Home" → creates task, verify in DB + Notion
- [ ] Send "/add Simple task" → creates with defaults (medium, no project)
- [ ] Send "/edit 1 title: New name" → updates title, Notion updates
- [ ] Send "/edit 1 priority: high" → updates priority
- [ ] Send "/edit 1 due: 2026-03-20" → sets due date
- [ ] Send "/edit 1 project: none" → clears project
- [ ] Send "/delete 1" → removes from DB, archived in Notion
- [ ] Send "/people" → lists tracked people
- [ ] Send "/dump I need to call Sarah about the contract and also buy milk" → extracts items
- [ ] Send "dump I need to finish the slides" → same extraction
- [ ] Send "hey how's it going" → goes to chat (no extraction API call)
- [ ] Send "thanks" → chat response (no extraction call)
- [ ] Send "done 99" → error: "No task #99. Use /list first."
- [ ] Morning/evening check-in flows still work normally

### Verification Checklist

- [ ] No extraction API call on plain chat messages (check logs for "Extraction" entries)
- [ ] Notion pages created for `/add`
- [ ] Notion pages updated for `/done`, `/doing`, `/edit`
- [ ] Notion pages archived for `/delete`
- [ ] Task map persists between commands (list → done → list shows updated state)
- [ ] Voice messages still work (transcribed text goes through process_input → command routing or chat)

## Rollback Plan

1. `git revert HEAD` to undo the commit
2. Or manually: delete `bot/commands.py`, `git checkout main -- bot/handlers.py bot/state.py main.py services/notion.py`

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Plain text "list" in brain dump triggers command instead of extraction | Medium | Low | Brain dumps now require explicit `/dump` prefix. Intentional trade-off — predictability over convenience. |
| Task map goes stale if tasks are added/deleted outside Telegram | Low | Low | Task map refreshes on every `/list` call. Users just need to re-list. |
| Notion sync failure on delete (task deleted from DB but not archived in Notion) | Low | Medium | `archive_task` is fire-and-forget after DB delete. Logs the error. Can manually archive in Notion. |
| `push_task()` is synchronous/blocking in async handlers | Already exists | Low | Existing pattern across the codebase. Not changing in this phase. |
| Voice messages containing command words (e.g., "list") trigger commands | Medium | Low | Acceptable — user said "list", they probably want to list. If it's part of a longer brain dump, they should use "dump" prefix. |

## Open Questions

None — all decisions resolved in research phase.

## Phase Prompts

### Phase 1 Prompt
```
/implement telegram task CRUD — Execute .context/personal-productivity-system/telegram-task-crud-plan.md.
Scope: Command router, task CRUD, smart routing. Steps 1.1–1.5.
Files: bot/state.py, services/notion.py, bot/commands.py (new), bot/handlers.py, main.py.
Prerequisite: Research reviewed.
Verification: /list, /done N, /add, /edit, /delete, /people, /dump all work via Telegram. Plain text goes to chat.
```
