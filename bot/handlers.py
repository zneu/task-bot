import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from services.claude import chat
from services.groq import transcribe_voice
from bot.state import get_state, clear_pending, add_to_history

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
    lines.append("")  # trailing newline before buttons
    return "\n".join(lines)


async def process_input(text: str, update: Update, source: str = "text"):
    """Process text input — extract or chat depending on content."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    # If awaiting confirmation
    if state["mode"] == "brain_dump_confirm":
        await handle_confirmation(text, update)
        return

    # Try command routing first
    from bot.commands import route_command
    handled = await route_command(text, update, state, source=source)
    if handled:
        # Don't add commands to conversation history — they get re-executed
        # by the classifier if they appear in history context. Only chat
        # messages (below) should persist for follow-up context.
        state["conversation_history"] = []
        return

    # Default: chat (with task context)
    from bot.commands import _get_task_context, _get_people_names
    _, task_context = await _get_task_context()
    people = await _get_people_names()
    context_msg = f"[Current open tasks:\n{task_context}\nTracked people: {', '.join(people) if people else 'none'}]"
    enriched_history = [{"role": "user", "content": context_msg}, {"role": "assistant", "content": "Got it, I have your current task and people context."}] + state["conversation_history"]
    response = chat(text, enriched_history)
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


async def save_items(items: dict, reply_target):
    """Save extracted items to Postgres and sync to Notion.

    reply_target: Update (has .message.reply_text) or CallbackQuery (has .message.reply_text)
    """
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
    # Get the reply method — works for both Update and CallbackQuery
    message = getattr(reply_target, 'message', reply_target)
    await message.reply_text(f"Saved {summary}.")



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


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    if not query or str(query.from_user.id) != AUTHORIZED_USER_ID:
        return

    await query.answer()

    data = query.data

    if data.startswith("done:") or data.startswith("doing:"):
        action, identifier = data.split(":", 1)
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        status = "done" if action == "done" else "in_progress"

        # Resolve: UUID (direct) or display number (via task_map)
        if "-" in identifier:
            task_id = identifier
        else:
            from bot.commands import _ensure_task_map
            await _ensure_task_map(state, user_id=user_id)
            task_id = state.get("task_map", {}).get(identifier)

        if task_id:
            from bot.commands import _set_status_by_id
            await _set_status_by_id(task_id, status, query.message)
        else:
            await query.message.reply_text("Couldn't find that task. Use /list to refresh.")

    elif data == "save_dump":
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        if state["pending_items"]:
            await save_items(state["pending_items"], query)
            clear_pending(user_id)
        else:
            await query.message.reply_text("Nothing to save.")

    elif data == "discard_dump":
        user_id = str(query.from_user.id)
        clear_pending(user_id)
        await query.message.reply_text("Discarded.")

    elif data.startswith("confirm:"):
        action_id = data.split(":", 1)[1]
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        from bot.commands import execute_pending_action
        await execute_pending_action(action_id, state, query.message)

    elif data.startswith("cancel:"):
        action_id = data.split(":", 1)[1]
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        state.get("pending_actions", {}).pop(action_id, None)
        await query.message.reply_text("Cancelled.")


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
