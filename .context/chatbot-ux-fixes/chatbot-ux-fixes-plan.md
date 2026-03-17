# Chatbot UX Fixes — Plan

- **Branch:** `feature/telegram-task-management`
- **Date:** 2026-03-17
- **Research:** `.context/chatbot-ux-fixes/chatbot-ux-research.md`

## Goal

Fix the 5 bugs that make the bot delete wrong tasks, mangle natural language input, and choke on normal phrasing like "delete task 19".

## Success Criteria

- [x] Morning checkin sets task_map matching its numbering — "delete 12" after morning message hits the right task
- [x] "Delete task 19" strips "task" and finds task #19
- [x] "Add this back under music Review piano study" goes to Claude, not fast-path cmd_add
- [x] Multi-line "Add X\n\nAnd add Y" detected as compound and routed to Claude
- [x] Conversation history cleared after successful command dispatch (no bleed)

## Scope Boundaries

### In Scope
- Bug 1: Morning checkin syncs task_map
- Bug 2: Newline-aware compound detection
- Bug 3: Strip "task"/"number"/"#" from number args
- Bug 4: Fast-path add falls through on natural language
- Bug 5: Clear command history after dispatch

### Out of Scope
- Changing morning checkin sort order (Zachary likes priority sort)
- Afternoon/evening checkin numbering (they don't show numbers)
- Test suite creation

---

## Implementation Steps

### Step 1: Morning checkin sets task_map (Bug 1)

- **File:** `bot/scheduler.py` **Lines:** L18-50
- **Change:** After sorting tasks by priority, build task_map and persist it via `save_task_map`. The scheduler has `AUTHORIZED_USER_ID` already (L15). Import and call `save_task_map` + update in-memory state.
- **Before:**
```python
async def morning_checkin(telegram_app):
    """Pull open tasks, send Claude's recommendation. No response required."""
    logger.info("Morning check-in triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "Good morning! No open tasks. Enjoy the clarity.")
        return

    # Sort by priority (high first), then by avoided_count descending
    priority_order = {"high": 0, "medium": 1, "low": 2}
    tasks.sort(key=lambda t: (priority_order.get(t.priority, 1), -t.avoided_count))

    task_list = "\n".join(
        f"- [{i+1}] {t.title} (priority: {t.priority}, avoided: {t.avoided_count}x"
        + (f", due: {t.due_date.strftime('%Y-%m-%d')}" if t.due_date else "")
        + (f", project: {t.project}" if t.project else "")
        + ")"
        for i, t in enumerate(tasks)
    )

    system = (PROMPTS_DIR / "morning.txt").read_text()
    user_msg = f"Here are my open tasks:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=800)

    await telegram_app.bot.send_message(chat_id, response)
```
- **After:**
```python
async def morning_checkin(telegram_app):
    """Pull open tasks, send Claude's recommendation. No response required."""
    logger.info("Morning check-in triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "Good morning! No open tasks. Enjoy the clarity.")
        return

    # Sort by priority (high first), then by avoided_count descending
    priority_order = {"high": 0, "medium": 1, "low": 2}
    tasks.sort(key=lambda t: (priority_order.get(t.priority, 1), -t.avoided_count))

    # Sync task_map to match morning numbering so "delete 3" hits the right task
    task_map = {str(i + 1): t.id for i, t in enumerate(tasks)}
    from bot.state import get_state, save_task_map
    state = get_state(AUTHORIZED_USER_ID)
    state["task_map"] = task_map
    await save_task_map(AUTHORIZED_USER_ID, task_map)

    task_list = "\n".join(
        f"- [{i+1}] {t.title} (priority: {t.priority}, avoided: {t.avoided_count}x"
        + (f", due: {t.due_date.strftime('%Y-%m-%d')}" if t.due_date else "")
        + (f", project: {t.project}" if t.project else "")
        + ")"
        for i, t in enumerate(tasks)
    )

    system = (PROMPTS_DIR / "morning.txt").read_text()
    user_msg = f"Here are my open tasks:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=800)

    await telegram_app.bot.send_message(chat_id, response)
```
- **Verification:** Morning message shows "12. Finish other tracks" → user says "delete 12" → bot deletes "Finish other tracks" (not some other task)

---

### Step 2: Strip "task"/"number"/"#" from number arguments (Bug 3)

- **File:** `bot/commands.py` — add helper near top, then use in `_set_status`, `cmd_delete`, `cmd_edit`
- **Change:** Add a `_clean_num` helper that strips common filler words. Apply to all commands that take a task number.
- **Code to add** (after imports, around L7):
```python
def _clean_num(raw: str) -> str:
    """Strip filler words from number arguments: 'task 19' → '19', 'number four' → '4'."""
    word_nums = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
        "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
        "nineteen": "19", "twenty": "20",
    }
    cleaned = raw.strip().lower()
    # Remove filler words
    for word in ("task", "number", "#"):
        cleaned = cleaned.replace(word, "")
    cleaned = cleaned.strip()
    # Convert word numbers to digits
    if cleaned in word_nums:
        cleaned = word_nums[cleaned]
    return cleaned
```
- **Apply to `_set_status`** (L339-364):
  - Before: `num = args.strip()`
  - After: `num = _clean_num(args)`
- **Apply to `cmd_delete`** (L581-613):
  - Before: `num = args.strip()`
  - After: `num = _clean_num(args)`
- **Apply to `cmd_edit`** (L487-515):
  - Before: `num = parts[0]`
  - After: `num = _clean_num(parts[0])`
- **Verification:** "Delete task 19" → deletes task #19. "Done number three" → marks #3 done.

---

### Step 3: Fast-path add falls through to Claude on natural language (Bug 4)

- **File:** `bot/commands.py` **Lines:** L44-61 (fast path routing)
- **Change:** For the "add" prefix specifically, if the args contain natural language prepositions ("under", "back under", "for", "to the", "in the"), break out of fast path and let Claude handle it.
- **Before** (inside the fast-path loop, L44-61):
```python
    for prefix, handler_name, requires_args in _COMMAND_TABLE:
        if lower == prefix or lower.startswith(prefix + " "):
            args = text.strip()[len(prefix):].strip()
            # Compound command detection: skip fast path, let Claude parse it
            if " and " in args.lower():
                break
            if prefix == "dump":
                ...
```
- **After:**
```python
    for prefix, handler_name, requires_args in _COMMAND_TABLE:
        if lower == prefix or lower.startswith(prefix + " "):
            args = text.strip()[len(prefix):].strip()
            # Compound command detection: skip fast path, let Claude parse it
            if " and " in args.lower() or "\nand " in args.lower() or "\n" in args:
                break
            # Natural language add: let Claude parse project/context references
            if prefix == "add" and re.search(r'\b(under|back|for the|to the|in the|into)\b', args.lower()):
                break
            if prefix == "dump":
                ...
```
- **Verification:** "Add this back under music Review piano study master document" → goes to Claude → correctly creates task with project: Music

---

### Step 4: Newline-aware compound detection (Bug 2)

Already handled in Step 3 — the `"\nand " in args.lower() or "\n" in args` check. Any multi-line message breaks out of fast path.

- **Verification:** "Add X\n\nAnd add Y" → Claude parses as two add intents

---

### Step 5: Clear conversation history after successful command dispatch (Bug 5)

- **File:** `bot/handlers.py` **Lines:** L56-60
- **Change:** After a successful command dispatch that executed intents, clear the conversation history so old commands don't bleed into the next classification. Keep history for chat (non-command) messages.
- **Before** (L56-60):
```python
    handled = await route_command(text, update, state)
    if handled:
        # Save to conversation history so classify_intent has context for follow-ups
        add_to_history(user_id, "user", text)
        return
```
- **After:**
```python
    handled = await route_command(text, update, state)
    if handled:
        # Save to conversation history for follow-ups, but cap command history
        # to prevent old commands from bleeding into new classifications
        add_to_history(user_id, "user", text)
        # Keep only the last exchange (1 user + 1 assistant) for command context
        # This prevents "delete 4" from replaying intents from 3 messages ago
        if len(state["conversation_history"]) > 2:
            state["conversation_history"] = state["conversation_history"][-2:]
        return
```
- **Verification:** Voice "delete 4 and delete 1" only generates 2 intents, not 4

---

## Testing Plan

### Manual Testing
- [ ] Morning checkin fires → immediately send "delete [N]" → correct task deleted
- [ ] "Delete task 19" → works (strips "task")
- [ ] "Done number three" → works (strips "number", converts word)
- [ ] "Add this back under music Review piano study" → Claude parses correctly, creates task under Music project
- [ ] "Add buy groceries" → still hits fast path (no natural language markers)
- [ ] Multi-line "Add X\n\nAnd add Y" → two tasks created
- [ ] Two voice commands in a row → second doesn't replay first

## Rollback Plan

All changes are in 3 files. `git revert` the commit.

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `_clean_num` strips "task" from a task title | None | N/A | Only applied to the number argument, not titles |
| Natural language regex too broad for add | Low | Low | Only triggers on specific prepositions; "add buy groceries p:high" still fast-paths |
| Trimming history too aggressively | Low | Medium | Keep last 2 messages (1 exchange) so "yes do that" follow-ups still work |
| Morning task_map overwritten by /list | Expected | None | This is correct behavior — /list should update to its own numbering |

## Open Questions

None — all fixes are surgical and well-scoped.
