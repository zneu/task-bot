# Bot Improvements — Smarter Claude Context + Inline Keyboards

- **Branch:** `feature/telegram-task-management`
- **Date:** 2026-03-16
- **Research:** `.context/personal-productivity-system/bot-improvements-research.md`

## Goal

Give Claude full task/people awareness so natural language commands actually work well ("mark the groceries task done", "what's due this week"), and add inline keyboard buttons to task lists and confirmations for one-tap interaction.

## Success Criteria

- [ ] "mark the groceries task done" works (Claude sees task titles, matches by name)
- [ ] "what's due this week?" in chat returns relevant task info
- [ ] "what do I need to do for Sarah?" works (Claude sees people names)
- [ ] Intent classifier receives task titles, statuses, priorities, due dates, and people names
- [ ] Chat fallback receives task/people context so conversational questions about tasks get real answers
- [ ] `/list` output includes inline `[Done]` and `[Doing]` buttons per task
- [ ] Brain dump confirmation uses `[Save]` / `[Discard]` inline buttons instead of text yes/no
- [ ] Callback query handler registered in `main.py`
- [ ] Existing text-based commands still work unchanged

## Scope Boundaries

### In Scope
- Enrich `classify_intent()` system prompt with task titles/statuses/due dates and people names
- Enrich `chat()` with task and people context
- Add helper to build task context string from DB
- Add inline keyboards to `cmd_list` output (Done/Doing buttons per task)
- Add inline keyboard to brain dump confirmation (Save/Discard buttons)
- Add `CallbackQueryHandler` in `main.py` + callback dispatcher in `bot/handlers.py`
- Update `_classify_and_dispatch` to pass richer context

### Out of Scope
- Inline keyboards on scheduler check-in messages (future improvement)
- Message editing (updating list in-place after button press) — will send new message for now
- Inline mode (`@bot` from other chats)
- Bot reactions (emoji on messages)
- Message pinning
- Removing dead morning/evening response handlers (kept per passive-reminders plan)

---

## Implementation Steps

### Phase 1: Smarter Claude Context

#### Step 1.1: Add task/people context builder
- **File:** `bot/commands.py` **Lines:** After L113 (after `_get_project_names`)
- **Change:** Add `_get_task_context()` and `_get_people_names()` helpers that return structured strings for Claude prompts.
- **After:**
```python
async def _get_task_context() -> tuple[list[str], str]:
    """Get project names and a formatted task context string for Claude."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

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
```
- **Verification:** Functions return data when tasks/people exist in DB

#### Step 1.2: Update `_classify_and_dispatch` to pass richer context
- **File:** `bot/commands.py` **Lines:** L55-L66
- **Change:** Replace `_get_project_names()` call with `_get_task_context()` and `_get_people_names()`. Pass all to `classify_intent`.
- **Before:**
```python
async def _classify_and_dispatch(text: str, update: Update, state: dict) -> bool:
    """Use Claude to classify intent and dispatch to handler."""
    from services.claude import classify_intent

    # Get project names for fuzzy matching
    projects = await _get_project_names()

    try:
        result = classify_intent(text, projects, state.get("task_map", {}))
    except Exception:
        logger.exception("Intent classification failed")
        return False  # Fall through to chat
```
- **After:**
```python
async def _classify_and_dispatch(text: str, update: Update, state: dict) -> bool:
    """Use Claude to classify intent and dispatch to handler."""
    from services.claude import classify_intent

    # Get full task and people context for Claude
    projects, task_context = await _get_task_context()
    people = await _get_people_names()

    try:
        result = classify_intent(text, projects, state.get("task_map", {}), task_context, people)
    except Exception:
        logger.exception("Intent classification failed")
        return False  # Fall through to chat
```
- **Verification:** `classify_intent` receives 5 args instead of 3

#### Step 1.3: Update `classify_intent` signature and prompt
- **File:** `services/claude.py` **Lines:** L49-L107
- **Change:** Add `task_context` and `people` params. Include task titles/details and people names in the system prompt so Claude can match by name, understand due dates, and answer questions about people.
- **Before:**
```python
def classify_intent(text: str, projects: list[str], task_map: dict) -> dict:
```
- **After:**
```python
def classify_intent(text: str, projects: list[str], task_map: dict, task_context: str = "", people: list[str] = None) -> dict:
```

Update the system prompt (L70-L94) — add task details and people after the existing project/task_map lines:

- **Before (L72-L73):**
```python
    system = f"""You are a task management command parser. Today is {today}.

The user's projects are: {json.dumps(projects) if projects else "none yet"}
{task_list_str}
```
- **After:**
```python
    people_str = json.dumps(people) if people else "none"

    system = f"""You are a task management command parser. Today is {today}.

The user's projects are: {json.dumps(projects) if projects else "none yet"}
{task_list_str}

Current open tasks:
{task_context}

Tracked people: {people_str}
```

Also add to the rules section (before the closing `"""` at L94):
```
- When the user references a task by name/description (not number), match to the closest task title above
- When the user asks about a person, match to tracked people names fuzzily
```
- **Verification:** "mark the groceries task done" returns `{"intent": "done", "num": "N"}` where N matches the groceries task's position

#### Step 1.4: Add task-aware chat context
- **File:** `bot/handlers.py` **Lines:** L70-L74
- **Change:** Before calling `chat()`, fetch task and people context from DB and prepend to conversation history so Claude can answer questions about tasks.
- **Before:**
```python
    # Default: chat
    response = chat(text, state["conversation_history"])
    add_to_history(user_id, "user", text)
    add_to_history(user_id, "assistant", response)
    await update.message.reply_text(response)
```
- **After:**
```python
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
```
- **Verification:** "what's due this week?" in chat returns actual task info. "what do I need to do for Sarah?" returns relevant people context.

#### Step 1.5: Remove now-redundant `_get_project_names`
- **File:** `bot/commands.py` **Lines:** L103-L113
- **Change:** Delete `_get_project_names()` — its functionality is now part of `_get_task_context()` which returns projects as the first tuple element. Update the `from sqlalchemy import select, distinct` import in `_get_task_context` since it needs `distinct`.
- **Verification:** No references to `_get_project_names` remain. Code still works.

---

### Phase 2: Inline Keyboards

#### Step 2.1: Add callback query handler
- **File:** `bot/handlers.py` **Lines:** After `handle_slash_command` (L290-L302)
- **Change:** Add `handle_callback` that dispatches inline keyboard button presses.
- **After:**
```python
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    if not query or str(query.from_user.id) != AUTHORIZED_USER_ID:
        return

    await query.answer()  # Acknowledge the button press

    data = query.data  # e.g. "done:3" or "doing:3" or "save_dump" or "discard_dump"

    if data.startswith("done:") or data.startswith("doing:"):
        action, num = data.split(":", 1)
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        from bot.commands import cmd_done, cmd_doing
        if action == "done":
            await cmd_done(num, update, state)
        else:
            await cmd_doing(num, update, state)

    elif data == "save_dump":
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        if state["pending_items"]:
            await save_items(state["pending_items"], update)
            clear_pending(user_id)
        else:
            await query.message.reply_text("Nothing to save.")

    elif data == "discard_dump":
        user_id = str(query.from_user.id)
        clear_pending(user_id)
        await query.message.reply_text("Discarded.")
```
- **Verification:** Button presses route to the correct handler

#### Step 2.2: Fix callback handler to work with CallbackQuery updates
- **File:** `bot/handlers.py`
- **Change:** The `cmd_done`/`cmd_doing` functions use `update.message.reply_text()`, but callback queries have `update.callback_query.message` instead of `update.message`. We need to handle this. Update `handle_callback` to pass a reply function or use `query.message.reply_text`.

Actually, looking more carefully: `cmd_done` and `cmd_doing` call `_set_status` which uses `update.message.reply_text()`. For callback queries, `update.message` is None but `update.callback_query.message` exists. The simplest fix: in `handle_callback`, call `_set_status` directly with `query.message` as the reply target.

Revise `handle_callback`:
```python
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    if not query or str(query.from_user.id) != AUTHORIZED_USER_ID:
        return

    await query.answer()

    data = query.data

    if data.startswith("done:") or data.startswith("doing:"):
        action, num = data.split(":", 1)
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        status = "done" if action == "done" else "in_progress"
        from bot.commands import _set_status
        # _set_status uses update.message — patch to use callback message
        await _set_status_from_callback(num, status, query.message, state)

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
```

And add `_set_status_from_callback` to `bot/commands.py`:
```python
async def _set_status_from_callback(num: str, status: str, message, state: dict):
    """Set task status from inline keyboard callback (uses message.reply_text)."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await message.reply_text(f"No task #{num}. Use /list first.")
        return

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
```
- **Verification:** Pressing `[Done]` button sends correct status update

#### Step 2.3: Add inline keyboards to `cmd_list`
- **File:** `bot/commands.py` **Lines:** L160-L213 (`cmd_list`)
- **Change:** After building the text list, add an `InlineKeyboardMarkup` with Done/Doing buttons per task. Telegram limits callback_data to 64 bytes — `"done:N"` is fine.
- **Add import at top of file (L4):**
```python
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
```
- **Before (L212-L213):**
```python
    header = "All tasks:" if show_all else "Open tasks:"
    await update.message.reply_text(f"{header}\n\n" + "\n".join(lines))
```
- **After:**
```python
    header = "All tasks:" if show_all else "Open tasks:"
    text = f"{header}\n\n" + "\n".join(lines)

    # Build inline keyboard: Done/Doing buttons for open tasks
    buttons = []
    for num_str, task_id in task_map.items():
        # Find the task to check if it's already done
        task_obj = next((t for t in tasks if t.id == task_id), None)
        if task_obj and task_obj.status != "done":
            buttons.append([
                InlineKeyboardButton(f"Done {num_str}", callback_data=f"done:{num_str}"),
                InlineKeyboardButton(f"Doing {num_str}", callback_data=f"doing:{num_str}"),
            ])

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(text, reply_markup=reply_markup)
```
- **Verification:** `/list` shows task list with Done/Doing buttons below each open task

#### Step 2.4: Add inline keyboard to brain dump confirmation
- **File:** `bot/commands.py` **Lines:** L132-L135 (`_handle_dump`, the `set_pending` + `reply_text` block)
- **Change:** Add Save/Discard inline buttons to the confirmation message.
- **Before:**
```python
    if has_items:
        set_pending(user_id, items)
        formatted = format_extracted(items)
        await update.message.reply_text(formatted)
```
- **After:**
```python
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
```
- **Verification:** Brain dump shows Save/Discard buttons. Pressing Save persists items.

#### Step 2.5: Update `save_items` to accept callback query
- **File:** `bot/handlers.py` **Lines:** L94-L150 (`save_items`)
- **Change:** `save_items` currently takes `update: Update` and calls `update.message.reply_text()`. When called from a callback, `update` is a `CallbackQuery` not an `Update`. Change the function to accept a generic reply target.
- **Before (L94, L150):**
```python
async def save_items(items: dict, update: Update):
    ...
    await update.message.reply_text(f"Saved {summary}.")
```
- **After:**
```python
async def save_items(items: dict, reply_target):
    """Save extracted items to Postgres and sync to Notion.

    reply_target: Update (has .message.reply_text) or CallbackQuery (has .message.reply_text)
    """
    ...
    # Get the reply method — works for both Update and CallbackQuery
    message = getattr(reply_target, 'message', reply_target)
    if hasattr(message, 'reply_text'):
        await message.reply_text(f"Saved {summary}.")
```
- **Verification:** Save works from both text "yes" confirmation and inline button

#### Step 2.6: Register CallbackQueryHandler in main.py
- **File:** `main.py` **Lines:** L9-L10
- **Change:** Import `CallbackQueryHandler`, import `handle_callback`, register handler.
- **Before:**
```python
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from bot.handlers import handle_message, handle_voice, handle_slash_command
```
- **After:**
```python
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from bot.handlers import handle_message, handle_voice, handle_slash_command, handle_callback
```
- **Before (L40-L45):**
```python
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    telegram_app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )
```
- **After:**
```python
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    telegram_app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))
```
- **Verification:** Inline buttons are handled by the bot (no "query not answered" errors)

#### Step 2.7: Update `format_extracted` to remove "Save these? (yes/no)" text
- **File:** `bot/handlers.py` **Lines:** L40
- **Change:** Remove the "Save these? (yes/no)" line from `format_extracted` since we now use inline buttons.
- **Before:**
```python
    lines.append("\nSave these? (yes/no)")
```
- **After:**
```python
    lines.append("")  # trailing newline before buttons
```
- **Verification:** Brain dump confirmation shows items with buttons, no "yes/no" text

---

## File Summary

| File | Action | Phase | Key Changes |
|------|--------|-------|------------|
| `bot/commands.py` | Modify | 1 + 2 | Add `_get_task_context`, `_get_people_names`, `_set_status_from_callback`; update `_classify_and_dispatch`; remove `_get_project_names`; add inline keyboards to `cmd_list` and `_handle_dump`; import `InlineKeyboardButton`/`InlineKeyboardMarkup` |
| `services/claude.py` | Modify | 1 | Update `classify_intent` signature + system prompt with task details and people |
| `bot/handlers.py` | Modify | 1 + 2 | Add task context to chat fallback; add `handle_callback`; update `save_items` for dual caller support; update `format_extracted` |
| `main.py` | Modify | 2 | Register `CallbackQueryHandler`, import `handle_callback` |

## Testing Plan

### Manual Testing (via Telegram)

**Phase 1 — Smarter context:**
- [ ] "mark the groceries task done" → matches groceries task by title, marks done
- [ ] "what's due this week?" → chat response lists tasks with approaching due dates
- [ ] "what do I need to do for Sarah?" → chat mentions Sarah's follow-up actions
- [ ] "show me my Thrown tasks" → still works (fuzzy project matching)
- [ ] "add buy milk due Friday" → still works (relative date resolution)
- [ ] Regular commands (`done 3`, `/list`, etc.) → still work unchanged
- [ ] "hey what's up" → still routes to chat, not a command

**Phase 2 — Inline keyboards:**
- [ ] `/list` → shows tasks with [Done N] [Doing N] buttons per open task
- [ ] Press [Done 1] → marks task 1 as done, bot confirms
- [ ] Press [Doing 2] → marks task 2 as in_progress, bot confirms
- [ ] `/dump I need to call Sarah and buy groceries` → shows extracted items with [Save] [Discard] buttons
- [ ] Press [Save] → saves items, bot confirms count
- [ ] Press [Discard] → clears pending, bot says "Discarded."
- [ ] Text "yes" after brain dump still works (backward compat via mode check)
- [ ] No "query not answered" errors in Telegram

### Smoke Tests
- [ ] Bot starts without import errors
- [ ] `/help` still works
- [ ] Voice messages still work (transcribe → route)

## Rollback Plan

```
git revert HEAD       # if single commit
git revert HEAD~1..   # if two commits (one per phase)
```

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Task context makes classify_intent prompt too long (>4000 tokens) | Low | Medium | Only include open tasks, not done. For 100 open tasks this is ~2000 tokens — well within limits. |
| Enriched chat history exceeds token limit | Low | Low | Context is prepended as 2 messages — ~500 tokens. conversation_history is already capped at 10 messages. |
| Inline keyboard buttons stop working after bot restart (task_map lost) | Medium | Low | User gets "No task #N. Use /list first." error — same as today. List refreshes the map. |
| Callback data collision if user presses old list buttons after new list | Low | Low | task_map updates on each list; old button numbers may map to wrong tasks. Acceptable for single user. |
| `save_items` dual-caller interface is fragile | Low | Medium | Simple `getattr` check. Both `Update` and `CallbackQuery` have `.message.reply_text`. |

## Open Questions

None.

## Phase Prompts

### Phase 1 Prompt
```
/implement bot improvements — Execute Phase 1 of .context/personal-productivity-system/smarter-context-inline-keyboards-plan.md.
Scope: Smarter Claude context. Steps 1.1–1.5.
Files: bot/commands.py, services/claude.py, bot/handlers.py.
Prerequisite: Passive reminders changes already deployed.
Verification: "mark the groceries task done" works by title match. "what's due this week?" returns real data in chat.
```

### Phase 2 Prompt
```
/implement bot improvements — Execute Phase 2 of .context/personal-productivity-system/smarter-context-inline-keyboards-plan.md.
Scope: Inline keyboards. Steps 2.1–2.7.
Files: bot/commands.py, bot/handlers.py, main.py.
Prerequisite: Phase 1 complete and committed.
Verification: /list shows Done/Doing buttons. Brain dump shows Save/Discard buttons. Buttons work.
```
