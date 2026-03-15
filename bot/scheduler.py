import os
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from database.connection import AsyncSessionLocal
from database.models import Task
from services.claude import get_response, get_checkin_context
from sqlalchemy import select
from pathlib import Path

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID", "")


async def morning_checkin(telegram_app):
    """Pull open tasks, ask Claude for top 3, send to user."""
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
    history = await get_checkin_context()
    user_msg = f"{history}\n\nHere are my open tasks:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=800)

    from bot.state import get_state
    state = get_state(AUTHORIZED_USER_ID)
    state["mode"] = "morning"
    # Store numbered mapping: "1" -> task_id, "2" -> task_id, etc.
    state["_morning_tasks"] = {str(i+1): t.id for i, t in enumerate(tasks)}

    await telegram_app.bot.send_message(chat_id, response)


async def afternoon_checkin(telegram_app):
    """Mid-day nudge on committed tasks."""
    logger.info("Afternoon check-in triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.committed_today == True)
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No tasks committed today. Want to pick something to focus on this afternoon?")
        return

    task_list = "\n".join(f"- {t.title}" for t in tasks)

    system = (PROMPTS_DIR / "afternoon.txt").read_text()
    user_msg = f"Tasks I committed to today:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=500)

    await telegram_app.bot.send_message(chat_id, response)


async def evening_checkin(telegram_app):
    """Check committed tasks, ask what happened."""
    logger.info("Evening check-in triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.committed_today == True)
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No tasks were committed today. How did the day go?")
        return

    task_list = "\n".join(f"- {t.title}" for t in tasks)

    system = (PROMPTS_DIR / "evening.txt").read_text()
    history = await get_checkin_context()
    user_msg = f"{history}\n\nTasks I committed to today:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=500)

    from bot.state import get_state
    state = get_state(AUTHORIZED_USER_ID)
    state["mode"] = "evening"
    state["committed_task_ids"] = [t.id for t in tasks]

    await telegram_app.bot.send_message(chat_id, response)


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


def start_scheduler(telegram_app):
    tz = pytz.timezone(os.getenv("TIMEZONE", "America/Lima"))
    scheduler = AsyncIOScheduler(timezone=tz)

    morning = os.getenv("MORNING_CHECK_IN_TIME", "08:00").split(":")
    afternoon = os.getenv("AFTERNOON_CHECK_IN_TIME", "14:00").split(":")
    evening = os.getenv("EVENING_CHECK_IN_TIME", "20:00").split(":")

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

    # Weekly summary on Sundays, 30 min before evening check-in
    weekly_minute = max(int(evening[1]) - 30, 0)
    weekly_hour = int(evening[0]) if int(evening[1]) >= 30 else int(evening[0]) - 1
    scheduler.add_job(
        weekly_summary,
        CronTrigger(day_of_week="sun", hour=weekly_hour, minute=weekly_minute, timezone=tz),
        args=[telegram_app],
    )

    scheduler.start()
    logger.info(
        f"Scheduler started: morning={morning[0]}:{morning[1]}, "
        f"afternoon={afternoon[0]}:{afternoon[1]}, "
        f"evening={evening[0]}:{evening[1]} ({tz})"
    )
    return scheduler
