import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from services.claude import chat
from services.groq import transcribe_voice
from bot.state import get_state, set_pending, clear_pending, add_to_history

logger = logging.getLogger(__name__)
AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID", "")


def is_authorized(update: Update) -> bool:
    return str(update.effective_user.id) == AUTHORIZED_USER_ID


def format_extracted(items: dict) -> str:
    """Format extracted items for display."""
    lines = []
    if items.get("tasks"):
        lines.append("Tasks:")
        for t in items["tasks"]:
            pri = t.get("priority", "Medium")
            proj = f" [{t.get('project')}]" if t.get("project") else ""
            lines.append(f"  - {t['name']} ({pri}){proj}")
    if items.get("people"):
        lines.append("\nPeople:")
        for p in items["people"]:
            action = f" -> {p.get('follow_up_action')}" if p.get("follow_up_action") else ""
            lines.append(f"  - {p['name']}: {p.get('context', '')}{action}")
    if items.get("ideas"):
        lines.append("\nIdeas:")
        for i in items["ideas"]:
            lines.append(f"  - {i['description']}")
    if items.get("commitments"):
        lines.append("\nCommitments:")
        for c in items["commitments"]:
            due = f" (by {c.get('due_date')})" if c.get("due_date") else ""
            lines.append(f"  - {c['description']}{due}")
    lines.append("\nSave these? (yes/no)")
    return "\n".join(lines)


async def process_input(text: str, update: Update, source: str = "text"):
    """Process text input — extract or chat depending on content."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    # If awaiting confirmation
    if state["mode"] == "brain_dump_confirm":
        await handle_confirmation(text, update)
        return

    # Morning check-in confirmation
    if state["mode"] == "morning":
        await handle_morning_response(text, update)
        return

    # Evening check-in response
    if state["mode"] == "evening":
        await handle_evening_response(text, update)
        return

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


async def handle_confirmation(text: str, update: Update):
    """Handle yes/no confirmation for pending items."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)
    lower = text.strip().lower()

    if lower in ("yes", "y", "confirm", "save"):
        items = state["pending_items"]
        await save_items(items, update)
        clear_pending(user_id)
    elif lower in ("no", "n", "cancel", "discard"):
        clear_pending(user_id)
        await update.message.reply_text("Discarded.")
    else:
        await update.message.reply_text("Reply yes to save or no to discard.")


async def save_items(items: dict, update: Update):
    """Save extracted items to Postgres and sync to Notion."""
    from database.connection import AsyncSessionLocal
    from database.models import Task, Person, Capture
    from services.notion import push_task
    import json

    saved_tasks = 0
    saved_people = 0

    async with AsyncSessionLocal() as session:
        # Save tasks
        for t in items.get("tasks", []):
            task = Task(
                title=t["name"],
                priority=t.get("priority", "Medium").lower(),
                project=t.get("project"),
                status="not_started",
            )
            session.add(task)
            await session.flush()

            # Sync to Notion
            notion_id = push_task(task)
            if notion_id:
                task.notion_id = notion_id

            saved_tasks += 1

        # Save people
        for p in items.get("people", []):
            person = Person(
                name=p["name"],
                context=p.get("context"),
                follow_up_action=p.get("follow_up_action"),
            )
            session.add(person)
            saved_people += 1

        # Save the capture record
        capture = Capture(
            raw_text=json.dumps(items),
            source="brain_dump",
            processed=True,
            items_created=[],
        )
        session.add(capture)

        await session.commit()

    parts = []
    if saved_tasks:
        parts.append(f"{saved_tasks} task(s)")
    if saved_people:
        parts.append(f"{saved_people} person(s)")
    summary = " and ".join(parts)
    await update.message.reply_text(f"Saved {summary}.")


async def handle_morning_response(text: str, update: Update):
    """Handle response to morning check-in — parse numbers, mark selected tasks as committed."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)
    import re

    # Extract numbers from the message (e.g. "1, 3, 5" or "1 3 5" or "1,3,5")
    numbers = re.findall(r'\d+', text)
    task_map = state.get("_morning_tasks", {})

    if not numbers:
        await update.message.reply_text("Send the numbers of the tasks you're committing to (e.g. 1, 3, 5)")
        return

    # Validate all numbers exist
    invalid = [n for n in numbers if n not in task_map]
    if invalid:
        await update.message.reply_text(f"Invalid number(s): {', '.join(invalid)}. Try again.")
        return

    selected_ids = [task_map[n] for n in numbers]

    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    committed_titles = []
    async with AsyncSessionLocal() as session:
        for task_id in selected_ids:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if task:
                task.committed_today = True
                committed_titles.append(task.title)
                push_task(task)
        await session.commit()

    state["mode"] = "idle"
    state["committed_task_ids"] = selected_ids

    task_lines = "\n".join(f"  - {t}" for t in committed_titles)
    await update.message.reply_text(f"Locked in.\n\n{task_lines}\n\nGo get it.")


async def handle_evening_response(text: str, update: Update):
    """Handle response to evening check-in — update task statuses."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    from services.claude import get_response
    from database.connection import AsyncSessionLocal
    from database.models import Task, CheckIn
    from services.notion import push_task
    from sqlalchemy import select
    import json

    system = """The user is reporting on their day. For each task they committed to, determine the status.
Return ONLY valid JSON: {"updates": [{"title_fragment": "...", "status": "done|in_progress|avoided"}]}
If you can't parse clearly, ask a follow-up question instead of returning JSON."""

    response = get_response(system, [
        {"role": "user", "content": f"Committed tasks: {state['committed_task_ids']}\n\nMy update: {text}"}
    ], max_tokens=500)

    try:
        raw = response.strip()
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
        data = json.loads(raw.strip())
        updates = data.get("updates", [])
    except (json.JSONDecodeError, KeyError):
        # Claude wants to ask a follow-up
        await update.message.reply_text(response)
        return

    async with AsyncSessionLocal() as session:
        for task_id in state["committed_task_ids"]:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                continue

            matched_status = None
            for u in updates:
                if u["title_fragment"].lower() in task.title.lower():
                    matched_status = u["status"]
                    break

            if matched_status:
                task.status = matched_status
                if matched_status == "avoided":
                    task.avoided_count += 1
                task.committed_today = False
                push_task(task)

        checkin = CheckIn(
            type="evening",
            committed_task_ids=state["committed_task_ids"],
            summary=text,
        )
        session.add(checkin)
        await session.commit()

    # Avoidance callout
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.avoided_count >= 3)
        )
        chronic = result.scalars().all()

    state["mode"] = "idle"
    state["committed_task_ids"] = []

    msg = "Updated."
    if chronic:
        names = ", ".join(t.title for t in chronic)
        msg += f"\n\nChronically avoided: {names}. What's really blocking these?"

    msg += "\n\nWhat are you carrying into tomorrow?"
    await update.message.reply_text(msg)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text
    if text and text.lower() == "ping":
        await update.message.reply_text("pong")
        return

    await process_input(text, update, source="text")


async def handle_slash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    # Reconstruct "command args" text from the slash command
    text = update.message.text  # e.g. "/list all"
    command_text = text.lstrip("/")

    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    from bot.commands import route_command
    await route_command(command_text, update, state)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    audio_bytes = await file.download_as_bytearray()

    await update.message.reply_text("Transcribing...")

    try:
        text = transcribe_voice(bytes(audio_bytes))
    except Exception:
        logger.exception("Transcription failed")
        await update.message.reply_text("Transcription failed. Try again.")
        return

    await update.message.reply_text(f"I heard:\n{text}")

    await process_input(text, update, source="voice")
