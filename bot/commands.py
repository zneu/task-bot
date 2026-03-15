import logging
import re
from datetime import datetime, timezone
from telegram import Update

logger = logging.getLogger(__name__)

# Command definitions: (prefix, handler_name, requires_args)
# Handler functions are defined below; lookup happens in route_command.
_COMMAND_TABLE = [
    ("help", "cmd_help", False),
    ("list", "cmd_list", False),
    ("tasks", "cmd_list", False),
    ("done", "cmd_done", True),
    ("doing", "cmd_doing", True),
    ("add", "cmd_add", True),
    ("edit", "cmd_edit", True),
    ("delete", "cmd_delete", True),
    ("del", "cmd_delete", True),
    ("people", "cmd_people", False),
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
    handlers = {name: func for name, func in globals().items() if name.startswith("cmd_")}
    for prefix, handler_name, requires_args in _COMMAND_TABLE:
        if lower == prefix or lower.startswith(prefix + " "):
            args = text.strip()[len(prefix):].strip()
            if requires_args and not args:
                await update.message.reply_text(f"Usage: /{prefix} ...")
                return True
            handler = handlers[handler_name]
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


async def cmd_help(args: str, update: Update, state: dict):
    """Show available commands."""
    text = """Commands (slash or plain text):

/list — open tasks grouped by project
/list all — include done tasks
/list [project] — filter by project
/done N — mark task #N as done
/doing N — mark task #N as in progress
/add [title] — quick-add a task
  options: p:high proj:Name due:YYYY-MM-DD
/edit N field: value — edit a task
  fields: title, priority, project, due, notes
/delete N — remove a task
/people — list tracked people
/dump [text] — brain dump extraction
/help — this message

Numbers refer to the last /list output.
All commands work without the slash too."""
    await update.message.reply_text(text)


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
