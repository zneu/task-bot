import logging
import re
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


async def _confirm_action(action: dict, message: str, update: Update, state: dict):
    """Store a pending action and show confirmation buttons."""
    user_id = str(update.effective_user.id)
    action["_user_id"] = user_id

    # Use incrementing IDs so compound commands each get their own button
    if "pending_actions" not in state:
        state["pending_actions"] = {}
    counter = state.get("_action_counter", 0) + 1
    state["_action_counter"] = counter
    action_id = str(counter)
    state["pending_actions"][action_id] = action

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm", callback_data=f"confirm:{action_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:{action_id}"),
        ]
    ])
    await update.message.reply_text(message, reply_markup=keyboard)


async def execute_pending_action(action_id: str, state: dict, message):
    """Execute a confirmed pending action and show updated task list."""
    actions = state.get("pending_actions", {})
    action = actions.pop(action_id, None)
    if not action:
        await message.reply_text("Nothing to confirm.")
        return

    action_type = action["type"]

    if action_type == "delete":
        await _exec_delete(action, message, state)
    elif action_type == "set_status":
        await _exec_set_status(action, message)
    elif action_type == "edit":
        await _exec_edit(action, message)
    elif action_type == "add":
        await _exec_add(action, message)
    elif action_type == "move":
        await _exec_move(action, message, state)

    # Show updated task list only after the last pending action
    if not actions:
        await _show_updated_list(message, state)


async def _show_updated_list(message, state: dict):
    """Show abbreviated task list after a mutation."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select, case

    pri_order = case(
        (Task.priority == "high", 1),
        (Task.priority == "medium", 2),
        (Task.priority == "low", 3),
        else_=4,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.notin_(["done"]))
            .order_by(Task.project, Task.due_date.asc().nullslast(), pri_order, Task.created_at)
        )
        tasks = result.scalars().all()

    if not tasks:
        await message.reply_text("No open tasks.")
        return

    task_map = {}
    lines = []
    n = 1
    current_project = "__UNSET__"
    for t in tasks:
        proj = t.project or "No Project"
        if proj != current_project:
            if lines:
                lines.append("")
            lines.append(f"[{proj}]")
            current_project = proj
        status_icon = {"not_started": "○", "in_progress": "◐", "done": "●", "avoided": "⊘"}.get(t.status, "○")
        pri_icon = {"high": "!", "medium": "", "low": "~"}.get(t.priority, "")
        due = ""
        if t.due_date:
            due = f" (due {t.due_date.strftime('%m/%d')})"
        lines.append(f"  {n}. {status_icon} {pri_icon}{t.title}{due}")
        task_map[str(n)] = t.id
        n += 1

    state["task_map"] = task_map

    # Persist updated map
    from bot.state import save_task_map, user_states
    user_id = None
    for uid, s in user_states.items():
        if s is state:
            user_id = uid
            break
    if user_id:
        await save_task_map(user_id, task_map)

    await message.reply_text("Open tasks:\n\n" + "\n".join(lines))


def _clean_num(raw: str) -> str:
    """Strip filler words from number arguments: 'task 19' → '19', 'number four' → '4'."""
    word_nums = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
        "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
        "nineteen": "19", "twenty": "20",
        # Ordinals
        "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
        "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
        "eleventh": "11", "twelfth": "12", "thirteenth": "13", "fourteenth": "14",
        "fifteenth": "15", "sixteenth": "16", "seventeenth": "17", "eighteenth": "18",
        "nineteenth": "19", "twentieth": "20",
    }
    cleaned = raw.strip().lower()
    cleaned = re.sub(r'[^\w\s]', '', cleaned)  # strip punctuation
    for word in ("task", "number", "please", "the", "that", "um", "uh", "like"):
        cleaned = re.sub(rf'\b{word}\b', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if cleaned in word_nums:
        return word_nums[cleaned]
    # Try individual tokens (e.g. "third one" → "third" → "3")
    for token in cleaned.split():
        if token in word_nums:
            return word_nums[token]
        if token.isdigit():
            return token
    return cleaned

# Prefix table for fast-path matching (no API call needed)
_COMMAND_TABLE = [
    ("clear", "cmd_clear", False),
    ("help", "cmd_help", False),
    ("list", "cmd_list", False),
    ("tasks", "cmd_list", False),
    ("done", "cmd_done", True),
    ("doing", "cmd_doing", True),
    ("add", "cmd_add", True),
    ("edit", "cmd_edit", True),
    ("delete", "cmd_delete", True),
    ("del", "cmd_delete", True),
    ("move", "cmd_move", True),
    ("people", "cmd_people", False),
    ("viewnote", "cmd_viewnote", True),
    ("notes", "cmd_notes", False),
    ("note", "cmd_note", True),
    ("dump", "_dump", True),
]


async def route_command(text: str, update: Update, state: dict, source: str = "text") -> bool:
    """Route user message to the right handler.

    Fast path: exact prefix match (e.g. "done 3", "list all").
    Slow path: Claude classifies intent (e.g. "show me my Thrown tasks").
    Returns True if handled (not chat).
    """
    lower = text.strip().lower()

    # Clear stale pending actions from previous commands so the
    # "show list after last action" check isn't blocked by old entries
    state.pop("pending_actions", None)
    state.pop("_action_counter", None)

    # Auto-populate task_map if empty
    # Suppress auto-show for /list since it will display its own full list
    user_id = str(update.effective_user.id)
    is_list_cmd = lower == "list" or lower == "tasks" or lower.startswith("list ") or lower.startswith("tasks ")
    await _ensure_task_map(state, update=None if is_list_cmd else update, user_id=user_id)

    # Fast path: prefix match (only for clean, simple commands)
    # Skip fast path if args contain "and" — likely a compound command for Claude
    handlers = {name: func for name, func in globals().items() if name.startswith("cmd_")}
    for prefix, handler_name, requires_args in _COMMAND_TABLE:
        if lower == prefix or lower.startswith(prefix + " "):
            args = text.strip()[len(prefix):].strip()
            # Compound command detection: skip fast path, let Claude parse it
            if " and " in args.lower() or "\nand " in args.lower() or "\n" in args:
                break
            # Natural language add: let Claude parse project/context references
            if prefix == "add" and re.search(r'\b(under|back|for the|to the|in the|into)\b', args.lower()):
                break
            # Edit: only fast-path "N field: value" — natural language goes to Claude
            if prefix == "edit" and not re.match(r'^\d+\s+\w+:', args.strip()):
                break
            # Voice: skip fast path unless args are basically just a number
            # "delete 3" or "done one please" → fast path (≤3 words, clean digit)
            # "edit task 11 to say confirm ecstatic dance" → Claude (long natural language)
            if source == "voice" and requires_args:
                clean = _clean_num(args)
                if not clean.isdigit() or len(args.split()) > 3:
                    break
            if prefix == "dump":
                if args:
                    await _handle_dump(args, update)
                else:
                    await update.message.reply_text("Send /dump followed by your brain dump text.")
                return True
            if requires_args and not args:
                await update.message.reply_text(f"Usage: /{prefix} ...")
                return True
            handler = handlers[handler_name]
            await handler(args, update, state)
            return True

    # Slow path: Claude intent classification
    return await _classify_and_dispatch(text, update, state, source=source)


async def _classify_and_dispatch(text: str, update: Update, state: dict, source: str = "text") -> bool:
    """Use Claude to classify intent and dispatch to handler.

    Supports single intents (dict) and compound intents (list of dicts).
    """
    from services.claude import classify_intent

    # Get full task and people context for Claude
    projects, task_context = await _get_task_context()
    people = await _get_people_names()

    # Build linked task number → title map for the classifier
    task_map = state.get("task_map", {})
    task_map_titles = await _get_task_map_titles(task_map)

    try:
        result = classify_intent(text, projects, task_map, task_context, people, state.get("conversation_history", []), task_map_titles=task_map_titles)
    except Exception:
        logger.exception("Intent classification failed")
        return False  # Fall through to chat

    # Normalize to list of intents
    if isinstance(result, list):
        intents = result
    else:
        intents = [result]

    # If the only intent is chat, fall through
    if len(intents) == 1 and intents[0].get("intent") == "chat":
        return False

    # Ensure task_map is populated before dispatching
    # Check if this is a list intent — suppress auto-show
    user_id = str(update.effective_user.id)
    is_list_intent = len(intents) == 1 and intents[0].get("intent") == "list"
    await _ensure_task_map(state, update=None if is_list_intent else update, user_id=user_id)

    for intent_data in intents:
        await _dispatch_single_intent(intent_data, text, update, state)

    return True


async def _dispatch_single_intent(result: dict, text: str, update: Update, state: dict):
    """Dispatch a single classified intent to its handler."""
    intent = result.get("intent", "chat")

    if intent == "chat":
        return
    elif intent == "help":
        await cmd_help("", update, state)
    elif intent == "list":
        args = ""
        if result.get("show_all"):
            args = "all"
        elif result.get("due"):
            args = f"due:{result['due']}"
        elif result.get("project"):
            args = result["project"]
        await cmd_list(args, update, state)
    elif intent == "done":
        await cmd_done(str(result.get("num", "")), update, state)
    elif intent == "doing":
        await cmd_doing(str(result.get("num", "")), update, state)
    elif intent == "add":
        await _cmd_add_structured(result, update, state)
    elif intent == "edit":
        await _cmd_edit_structured(result, update, state)
    elif intent == "delete":
        await cmd_delete(str(result.get("num", "")), update, state)
    elif intent == "move":
        nums = result.get("nums", [])
        project = result.get("project", "")
        if nums and project:
            args = ", ".join(nums) + " to " + project
            await cmd_move(args, update, state)
        else:
            await update.message.reply_text("Couldn't figure out which tasks or project. Try: /move 1,3,5 to Project Name")
    elif intent == "people":
        await cmd_people("", update, state)
    elif intent == "note":
        note_text = result.get("text", text)
        await cmd_note(note_text, update, state)
    elif intent == "notes":
        search = result.get("search", "")
        await cmd_notes(search, update, state)
    elif intent == "dump":
        dump_text = result.get("text", text)
        await _handle_dump(dump_text, update)


async def _ensure_task_map(state: dict, update: Update = None, user_id: str = None):
    """Auto-populate task_map if empty.

    Priority: in-memory → persisted in DB → fresh query.
    If update is provided and the map was empty, sends an abbreviated task list.
    """
    if state.get("task_map"):
        return

    # Try loading persisted map first
    if user_id:
        from bot.state import load_task_map
        persisted = await load_task_map(user_id)
        if persisted:
            state["task_map"] = persisted
            return

    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select, case

    pri_order = case(
        (Task.priority == "high", 1),
        (Task.priority == "medium", 2),
        (Task.priority == "low", 3),
        else_=4,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.notin_(["done"]))
            .order_by(Task.project, Task.due_date.asc().nullslast(), pri_order, Task.created_at)
        )
        tasks = result.scalars().all()

    if not tasks:
        return

    state["task_map"] = {str(i + 1): t.id for i, t in enumerate(tasks)}

    # Persist the freshly built map
    if user_id:
        from bot.state import save_task_map
        await save_task_map(user_id, state["task_map"])

    # Show abbreviated list so user knows the numbering
    if update and update.message:
        lines = [f"  {i+1}. {t.title}" for i, t in enumerate(tasks)]
        brief = "\n".join(lines[:8])
        if len(tasks) > 8:
            brief += f"\n  ... and {len(tasks) - 8} more"
        await update.message.reply_text(f"Your tasks:\n{brief}")


async def _push_overdue_tasks(session):
    """Push overdue tasks to today. Called during list operations."""
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    today = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59)
    result = await session.execute(
        select(Task).where(
            Task.due_date < datetime.now(timezone.utc).replace(hour=0, minute=0, second=0),
            Task.status.notin_(["done"]),
        )
    )
    overdue = result.scalars().all()
    if overdue:
        today_end = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59)
        for task in overdue:
            task.due_date = today_end
            push_task(task)
        await session.commit()


async def _get_task_context() -> tuple[list[str], str]:
    """Get project names and a formatted task context string for Claude."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select, distinct

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
            .order_by(Task.project, Task.created_at)
        )
        tasks = result.scalars().all()

        proj_result = await session.execute(
            select(distinct(Task.project)).where(Task.project.isnot(None))
        )
        projects = [row[0] for row in proj_result.all()]

    if not tasks:
        return projects, "No open tasks."

    lines = []
    for t in tasks:
        parts = [t.title, f"status:{t.status}", f"priority:{t.priority}"]
        if t.project:
            parts.append(f"project:{t.project}")
        if t.due_date:
            parts.append(f"due:{t.due_date.strftime('%Y-%m-%d')}")
        lines.append(f"  - {' | '.join(parts)}")

    return projects, "\n".join(lines)


async def _get_people_names() -> list[str]:
    """Get tracked people names from the database."""
    from database.connection import AsyncSessionLocal
    from database.models import Person
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Person.name))
        return [row[0] for row in result.all()]


async def _get_task_map_titles(task_map: dict) -> dict:
    """Resolve task_map display numbers to task titles."""
    if not task_map:
        return {}

    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    task_ids = list(task_map.values())
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id.in_(task_ids)))
        tasks_by_id = {str(t.id): t.title for t in result.scalars().all()}

    return {num: tasks_by_id.get(str(tid), "Unknown") for num, tid in task_map.items()}


async def _handle_dump(text: str, update: Update):
    """Process brain dump extraction."""
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
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Save", callback_data="save_dump"),
                InlineKeyboardButton("Discard", callback_data="discard_dump"),
            ]
        ])
        await update.message.reply_text(formatted, reply_markup=keyboard)
    else:
        await update.message.reply_text("Couldn't extract any items from that. Try being more specific.")


async def cmd_clear(args: str, update: Update, state: dict):
    """Clear conversation history."""
    state["conversation_history"] = []
    await update.message.reply_text("Conversation cleared.")


async def cmd_help(args: str, update: Update, state: dict):
    """Show available commands."""
    text = """You can talk to me naturally or use commands:

"show my tasks" or /list
"show all tasks for Thrown" or /list Thrown
"mark 3 as done" or /done 3
"I'm working on 2" or /doing 2
"add buy groceries due Friday" or /add ...
"change 1 priority to high" or /edit 1 priority: high
"delete 3" or /delete 3
"move 1, 3, 5 to Med Spa Scheduler" or /move 1,3,5 to Project Name
"who do I need to follow up with?" or /people
"dump I need to call Sarah and finish slides" or /dump ...
"note I've been thinking about pricing..." or /note ...
"show my notes" or /notes
"notes about taxes" or /notes taxes
/viewnote 1 — see full transcript + summary

I understand natural language, relative dates ("Friday", "end of the week"), and fuzzy project names.

Task numbers come from the last list you pulled up."""
    await update.message.reply_text(text)


async def cmd_list(args: str, update: Update, state: dict):
    """List tasks. Usage: list [all|project_name]"""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select, case

    pri_order = case(
        (Task.priority == "high", 1),
        (Task.priority == "medium", 2),
        (Task.priority == "low", 3),
        else_=4,
    )

    async with AsyncSessionLocal() as session:
        query = select(Task).order_by(Task.project, Task.due_date.asc().nullslast(), pri_order, Task.created_at)

        show_all = args.strip().lower() == "all"
        due_filter = None
        project_filter = None

        if show_all:
            pass
        elif args.strip().lower().startswith("due:"):
            due_filter = args.strip()[4:].strip()
        elif args.strip():
            project_filter = args.strip()

        if not show_all:
            query = query.where(Task.status.notin_(["done"]))

        if due_filter:
            # Parse due filter: "today", "this week", or "YYYY-MM-DD"
            from datetime import timedelta
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            if due_filter.lower() == "today":
                tomorrow = today_start + timedelta(days=1)
                query = query.where(Task.due_date >= today_start, Task.due_date < tomorrow)
            elif due_filter.lower() in ("this week", "week"):
                # Monday through Sunday
                days_since_monday = today_start.weekday()
                week_start = today_start - timedelta(days=days_since_monday)
                week_end = week_start + timedelta(days=7)
                query = query.where(Task.due_date >= week_start, Task.due_date < week_end)
            else:
                try:
                    target = datetime.strptime(due_filter, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    next_day = target + timedelta(days=1)
                    query = query.where(Task.due_date >= target, Task.due_date < next_day)
                except ValueError:
                    pass  # ignore invalid date, show all

        if project_filter:
            query = query.where(Task.project.ilike(f"%{project_filter}%"))

        # Auto-push overdue tasks to today
        await _push_overdue_tasks(session)

        result = await session.execute(query)
        tasks = result.scalars().all()

    if not tasks:
        label = f" due {due_filter}" if due_filter else (f" for '{project_filter}'" if project_filter else "")
        await update.message.reply_text(f"No tasks found{label}.")
        return

    # Build numbered list grouped by project, sorted by due date within each
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

    # Persist updated map
    user_id = str(update.effective_user.id)
    from bot.state import save_task_map
    await save_task_map(user_id, task_map)

    if show_all:
        header = "All tasks:"
    elif due_filter:
        header = f"Tasks due {due_filter}:"
    else:
        header = "Open tasks:"
    await update.message.reply_text(f"{header}\n\n" + "\n".join(lines))


async def cmd_done(args: str, update: Update, state: dict):
    """Mark task as done. Usage: done N"""
    await _set_status(args, "done", update, state)


async def cmd_doing(args: str, update: Update, state: dict):
    """Mark task as in progress. Usage: doing N"""
    await _set_status(args, "in_progress", update, state)


async def _set_status(args: str, status: str, update: Update, state: dict):
    """Set task status by row number — shows confirmation first."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    num = _clean_num(args)
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

    label = "Mark as done" if status == "done" else "Mark as in progress"
    await _confirm_action(
        {"type": "set_status", "task_id": task_id, "status": status, "num": num},
        f"{label}: #{num} {title}?",
        update, state,
    )


async def _exec_set_status(action: dict, message):
    """Execute confirmed status change."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == action["task_id"]))
        task = result.scalar_one_or_none()
        if not task:
            await message.reply_text("Task not found in database.")
            return
        task.status = action["status"]
        push_task(task)
        await session.commit()
        label = "Done" if action["status"] == "done" else "In progress"
        await message.reply_text(f"{label}: {task.title}")


async def _set_status_by_id(task_id: str, status: str, message):
    """Set task status by UUID (used by callback buttons)."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            await message.reply_text("Task not found in database.")
            return

        task.status = status
        push_task(task)
        await session.commit()
        label = "Done" if status == "done" else "In progress"
        await message.reply_text(f"{label}: {task.title}")


async def _set_status_from_callback(num: str, status: str, message, state: dict):
    """Set task status from inline keyboard callback (legacy number-based)."""
    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await message.reply_text(f"No task #{num}. Use /list first.")
        return
    await _set_status_by_id(task_id, status, message)


async def cmd_add(args: str, update: Update, state: dict):
    """Quick-add a task from prefix syntax. Usage: add Buy groceries p:high proj:Home due:2026-03-15"""

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

    await _create_task(title, priority, project, due_date, update, state)


async def _cmd_add_structured(result: dict, update: Update, state: dict):
    """Add a task from Claude-classified intent."""
    title = result.get("title", "").strip()
    if not title:
        await update.message.reply_text("Couldn't figure out the task title. Try again?")
        return

    priority = result.get("priority", "medium")
    project = result.get("project")
    due_date = None
    if result.get("due"):
        try:
            due_date = datetime.strptime(result["due"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    await _create_task(title, priority, project, due_date, update, state)


async def _create_task(title: str, priority: str, project: str | None, due_date: datetime | None, update: Update, state: dict = None):
    """Shared task creation logic — shows confirmation first."""
    user_id = str(update.effective_user.id)
    if state is None:
        from bot.state import get_state
        state = get_state(user_id)

    parts = [f"Add: {title}"]
    if priority != "medium":
        parts.append(f"  Priority: {priority}")
    if project:
        parts.append(f"  Project: {project}")
    if due_date:
        parts.append(f"  Due: {due_date.strftime('%Y-%m-%d')}")

    await _confirm_action(
        {"type": "add", "title": title, "priority": priority, "project": project, "due": due_date.strftime('%Y-%m-%d') if due_date else None},
        "\n".join(parts) + "?",
        update, state,
    )


async def _exec_add(action: dict, message):
    """Execute confirmed add."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task

    due_date = None
    if action.get("due"):
        due_date = datetime.strptime(action["due"], "%Y-%m-%d").replace(tzinfo=timezone.utc)

    async with AsyncSessionLocal() as session:
        task = Task(
            title=action["title"],
            priority=action.get("priority", "medium"),
            project=action.get("project"),
            due_date=due_date,
            status="not_started",
        )
        session.add(task)
        await session.flush()
        notion_id = push_task(task)
        if notion_id:
            task.notion_id = notion_id
        await session.commit()

    parts = [f"Added: {action['title']}"]
    if action.get("priority", "medium") != "medium":
        parts.append(f"  Priority: {action['priority']}")
    if action.get("project"):
        parts.append(f"  Project: {action['project']}")
    if action.get("due"):
        parts.append(f"  Due: {action['due']}")
    await message.reply_text("\n".join(parts))


async def cmd_edit(args: str, update: Update, state: dict):
    """Edit a task field from prefix syntax. Usage: edit N title: New Title"""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    # Parse: first token is the number, rest is "field: value"
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /edit N field: value\nFields: title, priority, project, due, notes")
        return

    num = _clean_num(parts[0])
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

    await _apply_edit(num, task_id, field, value, update, state)


async def _cmd_edit_structured(result: dict, update: Update, state: dict):
    """Edit a task from Claude-classified intent."""
    num = str(result.get("num", ""))
    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await update.message.reply_text(f"No task #{num}. Use /list first.")
        return

    field = result.get("field", "")
    value = result.get("value", "")

    if not field or not value:
        await update.message.reply_text("Couldn't figure out what to change. Try: /edit N field: value")
        return

    await _apply_edit(num, task_id, field, value, update, state)


async def _apply_edit(num: str, task_id: str, field: str, value: str, update: Update, state: dict = None):
    """Shared edit logic — shows confirmation first."""
    allowed = {"title", "priority", "project", "due", "notes"}
    if field not in allowed:
        await update.message.reply_text(f"Unknown field '{field}'. Allowed: {', '.join(sorted(allowed))}")
        return

    if field == "priority" and value.lower() not in ("high", "medium", "low"):
        await update.message.reply_text("Priority must be high, medium, or low.")
        return

    if field == "due" and value.lower() != "none":
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("Date format: YYYY-MM-DD (or 'none' to clear)")
            return

    user_id = str(update.effective_user.id)
    if state is None:
        from bot.state import get_state
        state = get_state(user_id)

    await _confirm_action(
        {"type": "edit", "task_id": task_id, "num": num, "field": field, "value": value},
        f"Update #{num} {field} → {value}?",
        update, state,
    )


async def _exec_edit(action: dict, message):
    """Execute confirmed edit."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == action["task_id"]))
        task = result.scalar_one_or_none()
        if not task:
            await message.reply_text("Task not found in database.")
            return

        field, value = action["field"], action["value"]
        if field == "title":
            task.title = value
        elif field == "priority":
            task.priority = value.lower()
        elif field == "project":
            task.project = value if value.lower() != "none" else None
        elif field == "due":
            if value.lower() == "none":
                task.due_date = None
            else:
                task.due_date = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        elif field == "notes":
            task.notes = value if value.lower() != "none" else None

        push_task(task)
        await session.commit()
        await message.reply_text(f"Updated #{action['num']} {field} → {value}")


async def cmd_delete(args: str, update: Update, state: dict):
    """Delete a task. Usage: delete N — shows confirmation first."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    num = _clean_num(args)
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

    await _confirm_action(
        {"type": "delete", "task_id": task_id, "num": num},
        f"Delete #{num} '{title}'?",
        update, state,
    )


async def _exec_delete(action: dict, message, state: dict):
    """Execute confirmed delete."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import archive_task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == action["task_id"]))
        task = result.scalar_one_or_none()
        if not task:
            await message.reply_text("Task not found in database.")
            return

        title = task.title
        notion_id = task.notion_id
        await session.delete(task)
        await session.commit()

    if notion_id:
        archive_task(notion_id)

    state.get("task_map", {}).pop(action["num"], None)
    await message.reply_text(f"Deleted: {title}")


async def cmd_move(args: str, update: Update, state: dict):
    """Move tasks to a project. Usage: move 1,3,5 to Project Name — shows confirmation first."""

    # Parse: "1, 3, 5 to Project Name" or "1 3 5 Project Name"
    m = re.match(r'([\d,\s]+)\s+(?:to\s+)?(.+)', args.strip())
    if not m:
        await update.message.reply_text("Usage: /move 1,3,5 to Project Name")
        return

    numbers = re.findall(r'\d+', m.group(1))
    project = m.group(2).strip()

    if not numbers or not project:
        await update.message.reply_text("Usage: /move 1,3,5 to Project Name")
        return

    # Resolve task IDs
    task_map = state.get("task_map", {})
    invalid = [n for n in numbers if n not in task_map]
    if invalid:
        await update.message.reply_text(f"Invalid task number(s): {', '.join(invalid)}. Use /list first.")
        return

    task_ids = {n: task_map[n] for n in numbers}
    project_value = project if project.lower() != "none" else None
    label = f"Move {len(numbers)} task(s) to {project}?" if project_value else f"Remove project from {len(numbers)} task(s)?"

    await _confirm_action(
        {"type": "move", "task_ids": task_ids, "project": project_value, "project_display": project},
        label, update, state,
    )


async def _exec_move(action: dict, message, state: dict):
    """Execute confirmed move."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    moved = []
    async with AsyncSessionLocal() as session:
        for n, tid in action["task_ids"].items():
            result = await session.execute(select(Task).where(Task.id == tid))
            task = result.scalar_one_or_none()
            if task:
                task.project = action["project"]
                push_task(task)
                moved.append(task.title)
        await session.commit()

    project = action["project_display"]
    if action["project"]:
        await message.reply_text(f"Moved {len(moved)} task(s) to {project}:\n" + "\n".join(f"  - {t}" for t in moved))
    else:
        await message.reply_text(f"Removed project from {len(moved)} task(s):\n" + "\n".join(f"  - {t}" for t in moved))


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


async def cmd_note(args: str, update: Update, state: dict):
    """Save a voice/text note with Claude summary. Usage: note [text]"""
    from database.connection import AsyncSessionLocal
    from database.models import Note
    from services.claude import summarize_note
    from services.notion import push_note

    if not args.strip():
        await update.message.reply_text("Usage: /note [your thoughts here]")
        return

    transcript = args.strip()

    # Determine source based on how we got here
    source = "voice" if len(transcript) > 100 else "text"

    try:
        result = summarize_note(transcript)
    except Exception:
        logger.exception("Note summarization failed")
        await update.message.reply_text("Failed to process note. Try again.")
        return

    title = result.get("title", "Untitled Note")
    summary = result.get("summary", "")
    tags = result.get("tags", [])

    async with AsyncSessionLocal() as session:
        note = Note(
            title=title,
            raw_transcript=transcript,
            summary=summary,
            tags=tags,
            source=source,
        )
        session.add(note)
        await session.flush()
        notion_id = push_note(note)
        if notion_id:
            note.notion_id = notion_id
        await session.commit()

    tag_str = f"\nTags: {', '.join(tags)}" if tags else ""
    await update.message.reply_text(f"Noted: {title}\n\n{summary}{tag_str}")


async def cmd_notes(args: str, update: Update, state: dict):
    """List or search notes. Usage: notes [search term]"""
    from database.connection import AsyncSessionLocal
    from database.models import Note
    from sqlalchemy import select

    search = args.strip()

    async with AsyncSessionLocal() as session:
        query = select(Note).order_by(Note.created_at.desc()).limit(10)
        if search:
            query = query.where(
                Note.raw_transcript.ilike(f"%{search}%")
                | Note.title.ilike(f"%{search}%")
                | Note.summary.ilike(f"%{search}%")
            )
        result = await session.execute(query)
        notes = result.scalars().all()

    if not notes:
        label = f" matching '{search}'" if search else ""
        await update.message.reply_text(f"No notes found{label}.")
        return

    # Store note map for viewing
    note_map = {}
    lines = ["Recent notes:" if not search else f"Notes matching '{search}':"]
    for i, n in enumerate(notes, 1):
        date = n.created_at.strftime("%m/%d")
        lines.append(f"  {i}. [{date}] {n.title}")
        note_map[str(i)] = n.id

    state["note_map"] = note_map
    lines.append("\nSend /viewnote N to see the full note.")
    await update.message.reply_text("\n".join(lines))


async def cmd_viewnote(args: str, update: Update, state: dict):
    """View a full note by number. Usage: viewnote N"""
    from database.connection import AsyncSessionLocal
    from database.models import Note
    from sqlalchemy import select

    num = args.strip()
    note_id = state.get("note_map", {}).get(num)
    if not note_id:
        await update.message.reply_text(f"No note #{num}. Use /notes first.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one_or_none()

    if not note:
        await update.message.reply_text("Note not found.")
        return

    date = note.created_at.strftime("%Y-%m-%d %H:%M")
    tag_str = f"\nTags: {', '.join(note.tags)}" if note.tags else ""
    await update.message.reply_text(
        f"{note.title}\n{date} ({note.source}){tag_str}\n\n"
        f"Summary:\n{note.summary}\n\n"
        f"Full transcript:\n{note.raw_transcript}"
    )
