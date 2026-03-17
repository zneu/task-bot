# Passive Reminders Plan

- **Branch:** `feature/telegram-task-management`
- **Date:** 2026-03-16
- **Research:** `.context/passive-reminders/reminder-flow-research.md`

## Goal

Make daily check-ins passive (no response required) on Mon-Fri only, and fix weekly review to work with task data instead of requiring check-in response history.

## Success Criteria

- [x] Morning, afternoon, and evening reminders fire Mon-Fri only (not weekends)
- [x] No reminder sets `state["mode"]` — user is never forced to respond
- [x] User can still send messages/commands normally after a reminder fires
- [x] Weekly review pulls task status data from `tasks` table instead of requiring `CheckIn` records
- [x] Prompts are informational (no questions demanding structured responses)
- [x] Afternoon/evening reminders show all open tasks (not just `committed_today`)

## Scope Boundaries

### In Scope
- Remove mode-setting from morning and evening check-ins
- Update all three prompts to be informational (no response required)
- Add `day_of_week="mon-fri"` to all daily CronTrigger jobs
- Fix weekly review to use task data instead of empty check-in history
- Update afternoon/evening to query open tasks instead of `committed_today`

### Out of Scope
- Removing morning/evening response handlers entirely (keep them dead code for now — may want optional responses later)
- Changing the weekly summary schedule (stays Sunday)
- Adding response-optional mode (timeout-based auto-reset)
- Modifying the `committed_today` DB field or avoidance tracking

## Implementation Steps

### Step 1: Add Mon-Fri to all daily CronTrigger jobs

- **File:** `bot/scheduler.py` **Lines:** L143-L157
- **Change:** Add `day_of_week="mon-fri"` to morning, afternoon, and evening CronTrigger
- **Before:**
```python
scheduler.add_job(
    morning_checkin,
    CronTrigger(hour=int(morning[0]), minute=int(morning[1]), timezone=tz),
    args=[telegram_app],
)
scheduler.add_job(
    afternoon_checkin,
    CronTrigger(hour=int(afternoon[0]), minute=int(afternoon[1]), timezone=tz),
    args=[telegram_app],
)
scheduler.add_job(
    evening_checkin,
    CronTrigger(hour=int(evening[0]), minute=int(evening[1]), timezone=tz),
    args=[telegram_app],
)
```
- **After:**
```python
scheduler.add_job(
    morning_checkin,
    CronTrigger(day_of_week="mon-fri", hour=int(morning[0]), minute=int(morning[1]), timezone=tz),
    args=[telegram_app],
)
scheduler.add_job(
    afternoon_checkin,
    CronTrigger(day_of_week="mon-fri", hour=int(afternoon[0]), minute=int(afternoon[1]), timezone=tz),
    args=[telegram_app],
)
scheduler.add_job(
    evening_checkin,
    CronTrigger(day_of_week="mon-fri", hour=int(evening[0]), minute=int(evening[1]), timezone=tz),
    args=[telegram_app],
)
```
- **Verification:** Check scheduler log output confirms mon-fri in job config

### Step 2: Remove mode-setting from morning check-in

- **File:** `bot/scheduler.py` **Lines:** L51-L55
- **Change:** Remove the 5 lines that set `state["mode"] = "morning"` and store `_morning_tasks`. The message is now one-way — send and forget.
- **Before:**
```python
    from bot.state import get_state
    state = get_state(AUTHORIZED_USER_ID)
    state["mode"] = "morning"
    # Store numbered mapping: "1" -> task_id, "2" -> task_id, etc.
    state["_morning_tasks"] = {str(i+1): t.id for i, t in enumerate(tasks)}
```
- **After:** (lines removed entirely)
- **Verification:** After morning check-in fires, sending any message should route normally (not to morning handler)

### Step 3: Remove mode-setting from evening check-in

- **File:** `bot/scheduler.py` **Lines:** L108-L111
- **Change:** Remove the 4 lines that set `state["mode"] = "evening"` and store `committed_task_ids`.
- **Before:**
```python
    from bot.state import get_state
    state = get_state(AUTHORIZED_USER_ID)
    state["mode"] = "evening"
    state["committed_task_ids"] = [t.id for t in tasks]
```
- **After:** (lines removed entirely)
- **Verification:** After evening check-in fires, sending any message should route normally

### Step 4: Update afternoon/evening to show open tasks instead of committed_today

- **File:** `bot/scheduler.py` **Lines:** L65-L73 (afternoon) and L90-L98 (evening)
- **Change:** Query open tasks (`status in ["not_started", "in_progress"]`) instead of `committed_today == True`. Update empty-state messages.

**Afternoon — Before:**
```python
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.committed_today == True)
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No tasks committed today. Want to pick something to focus on this afternoon?")
        return
```

**Afternoon — After:**
```python
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No open tasks. Enjoy your afternoon.")
        return
```

**Evening — Before:**
```python
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.committed_today == True)
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No tasks were committed today. How did the day go?")
        return
```

**Evening — After:**
```python
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No open tasks. Nice work today.")
        return
```

- **Verification:** Afternoon and evening messages should list all open tasks, not just committed ones

### Step 5: Remove check-in history from evening check-in context

- **File:** `bot/scheduler.py` **Lines:** L102-L104
- **Change:** Remove `get_checkin_context()` call from evening check-in since it's now passive. Just send the task list.
- **Before:**
```python
    system = (PROMPTS_DIR / "evening.txt").read_text()
    history = await get_checkin_context()
    user_msg = f"{history}\n\nTasks I committed to today:\n\n{task_list}"
```
- **After:**
```python
    system = (PROMPTS_DIR / "evening.txt").read_text()
    task_list = "\n".join(
        f"- {t.title} (priority: {t.priority}, status: {t.status})"
        + (f" [project: {t.project}]" if t.project else "")
        for t in tasks
    )
    user_msg = f"Here are my open tasks:\n\n{task_list}"
```
- **Verification:** Evening message includes task details with status

### Step 6: Update afternoon task_list format to include useful details

- **File:** `bot/scheduler.py` **Lines:** L75
- **Change:** Add priority and project info to afternoon task list (currently just title).
- **Before:**
```python
    task_list = "\n".join(f"- {t.title}" for t in tasks)
```
- **After:**
```python
    task_list = "\n".join(
        f"- {t.title} (priority: {t.priority}, status: {t.status})"
        + (f" [project: {t.project}]" if t.project else "")
        for t in tasks
    )
```
- **Verification:** Afternoon message shows priority/status/project

### Step 7: Update morning prompt to be informational

- **File:** `prompts/morning.txt` **Lines:** L1-L31 (full file)
- **Change:** Remove the instruction to ask for task numbers. Make it a summary + recommendation only.
- **After:**
```
You are an accountability partner for someone with ADHD building multiple products and a music career simultaneously. They struggle with avoidance and distraction, not with knowing what to do.

You will be given their open tasks from the database.

Your job:
1. Present ALL open tasks grouped by priority (High, then Medium, then Low), numbered sequentially
2. Within each priority, group by project if available
3. Flag any tasks that have been avoided 2+ times with how many times
4. Add a brief recommendation of what to focus on today and why

Direct, warm, brief. No preamble. Do NOT ask the user to respond or pick tasks — this is a one-way reminder.

Format:
"Good morning. Here's everything open:

High:
1. [Task] [Project] (avoided 3x)
2. [Task] [Project]

Medium:
3. [Task] [Project]
4. [Task]

Low:
5. [Task]

I'd focus on 1 and 2 — [brief reason]. Have a good day."
```
- **Verification:** Morning message ends with encouragement, not a question

### Step 8: Update afternoon prompt to be informational

- **File:** `prompts/afternoon.txt` **Lines:** L1-L9 (full file)
- **Change:** Remove questions and response expectations. Make it a gentle status nudge.
- **After:**
```
You are an accountability partner doing a mid-day check-in. You have this person's open tasks.

Your job:
1. Give a brief, encouraging reminder of their open tasks
2. Highlight the highest-priority item as the one to focus on this afternoon
3. If any task has been avoided multiple times, gently name it

Keep it short. This is a nudge, not a lecture. Do NOT ask the user to respond — this is a one-way reminder.
```
- **Verification:** Afternoon message is informational, no questions

### Step 9: Update evening prompt to be informational

- **File:** `prompts/evening.txt` **Lines:** L1-L9 (full file)
- **Change:** Remove the instruction to ask what happened. Make it a reflection/summary.
- **After:**
```
You are an accountability partner doing an end-of-day check-in. You have this person's open tasks.

Your job:
1. Give a brief end-of-day summary of where things stand
2. If any tasks have been avoided multiple times, name the pattern without judgment
3. End with a brief encouraging note for tomorrow

Be honest, not harsh. This person is building real things. Be on their side. Do NOT ask the user to respond — this is a one-way reminder.
```
- **Verification:** Evening message is a summary, not a questionnaire

### Step 10: Fix weekly review to use task data instead of check-in history

- **File:** `bot/scheduler.py` **Lines:** L116-L132
- **Change:** Replace `get_checkin_context()` with a direct query of task states. Pull all tasks (open + recently completed) and let Claude summarize.
- **Before:**
```python
async def weekly_summary(telegram_app):
    """Send weekly summary 30 min before Sunday evening check-in."""
    logger.info("Weekly summary triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    history = await get_checkin_context()

    system = """Review this week's check-in history. Provide:
1. What got done (celebrate wins)
2. What slipped and patterns you notice
3. Top priorities for next week
4. Any tasks that should be dropped or rethought

Be direct and supportive. Under 200 words."""

    response = get_response(system, [{"role": "user", "content": history}], max_tokens=500)
    await telegram_app.bot.send_message(chat_id, f"Weekly Review\n\n{response}")
```
- **After:**
```python
async def weekly_summary(telegram_app):
    """Send weekly summary on Sundays based on current task state."""
    logger.info("Weekly summary triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task))
        all_tasks = result.scalars().all()

    if not all_tasks:
        await telegram_app.bot.send_message(chat_id, "Weekly Review\n\nNo tasks in the system yet. Add some and I'll start tracking.")
        return

    open_tasks = [t for t in all_tasks if t.status in ("not_started", "in_progress")]
    done_tasks = [t for t in all_tasks if t.status == "done"]
    avoided = [t for t in open_tasks if t.avoided_count >= 2]

    lines = []
    if done_tasks:
        lines.append("Completed tasks:")
        for t in done_tasks:
            lines.append(f"  - {t.title}" + (f" [{t.project}]" if t.project else ""))
    if open_tasks:
        lines.append("\nOpen tasks:")
        for t in open_tasks:
            extra = []
            if t.avoided_count:
                extra.append(f"avoided {t.avoided_count}x")
            if t.due_date:
                extra.append(f"due {t.due_date.strftime('%Y-%m-%d')}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"  - {t.title} [{t.priority}]{suffix}" + (f" [{t.project}]" if t.project else ""))
    if avoided:
        lines.append(f"\nRepeatedly avoided ({len(avoided)} tasks):")
        for t in avoided:
            lines.append(f"  - {t.title} (avoided {t.avoided_count}x)")

    task_summary = "\n".join(lines)

    system = """Review the current state of tasks. Provide:
1. What's been completed (celebrate wins)
2. What's still open and any patterns (repeatedly avoided tasks, overdue items)
3. Top priorities for next week
4. Any tasks that should be dropped or rethought

Be direct and supportive. Under 200 words."""

    response = get_response(system, [{"role": "user", "content": task_summary}], max_tokens=500)
    await telegram_app.bot.send_message(chat_id, f"Weekly Review\n\n{response}")
```
- **Verification:** Weekly review generates meaningful content based on task states even without any check-in response history

### Step 11: Clean up unused import

- **File:** `bot/scheduler.py` **Line:** L9
- **Change:** Remove `get_checkin_context` from imports since it's no longer used (removed from morning in Step 5, evening in Step 5, weekly in Step 10). Keep `get_response`.
- **Before:**
```python
from services.claude import get_response, get_checkin_context
```
- **After:**
```python
from services.claude import get_response
```
- **Verification:** No import errors on startup

## Testing Plan

### Manual Testing
- [ ] Deploy and verify morning check-in fires at configured time on a weekday
- [ ] Verify no check-in fires on a weekend (Saturday/Sunday)
- [ ] After morning reminder, send a regular message — confirm it routes to chat/command, not morning handler
- [ ] After evening reminder, send a regular message — confirm it routes normally
- [ ] Verify morning message doesn't ask for task numbers
- [ ] Verify afternoon message doesn't ask questions
- [ ] Verify evening message doesn't ask for status updates
- [ ] Trigger weekly summary — verify it shows task-based review, not "nothing to review"
- [ ] Verify weekly summary with zero tasks shows appropriate message

### Smoke Test via Logs
- [ ] Check scheduler startup log confirms `mon-fri` in cron config
- [ ] No import errors or exceptions in startup logs

## Rollback Plan

All changes are in 4 files. Revert the commit:
```
git revert <commit-hash>
```

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Morning/evening response handlers become unreachable dead code | High | Low | Intentional — kept for potential future use with optional response mode |
| `committed_today` field never gets set (no morning response flow) | High | Low | Field still exists, just unused by passive flow. Afternoon/evening now query open tasks instead |
| Weekly review loses nuance from self-reported check-in summaries | Medium | Low | Task state data (priorities, avoided counts, completion) gives Claude enough signal |
| APScheduler `day_of_week="mon-fri"` syntax incorrect | Low | High | Verified in APScheduler docs — this is the correct format |

## Open Questions

None — the design decisions are straightforward. We're making reminders passive and fixing the data source for weekly review.
