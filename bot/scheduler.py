import os
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from database.connection import AsyncSessionLocal
from database.models import Task
from services.claude import get_response
from sqlalchemy import select
from pathlib import Path

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID", "")


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


async def afternoon_checkin(telegram_app):
    """Mid-day nudge on open tasks. No response required."""
    logger.info("Afternoon check-in triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No open tasks. Enjoy your afternoon.")
        return

    task_list = "\n".join(
        f"- {t.title} (priority: {t.priority}, status: {t.status})"
        + (f" [project: {t.project}]" if t.project else "")
        for t in tasks
    )

    system = (PROMPTS_DIR / "afternoon.txt").read_text()
    user_msg = f"Here are my open tasks:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=500)

    await telegram_app.bot.send_message(chat_id, response)


async def evening_checkin(telegram_app):
    """End-of-day summary of open tasks. No response required."""
    logger.info("Evening check-in triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "No open tasks. Nice work today.")
        return

    system = (PROMPTS_DIR / "evening.txt").read_text()
    task_list = "\n".join(
        f"- {t.title} (priority: {t.priority}, status: {t.status})"
        + (f" [project: {t.project}]" if t.project else "")
        for t in tasks
    )
    user_msg = f"Here are my open tasks:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=500)

    await telegram_app.bot.send_message(chat_id, response)


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


def start_scheduler(telegram_app):
    tz = pytz.timezone(os.getenv("TIMEZONE", "America/Lima"))
    scheduler = AsyncIOScheduler(timezone=tz)

    morning = os.getenv("MORNING_CHECK_IN_TIME", "08:00").split(":")
    afternoon = os.getenv("AFTERNOON_CHECK_IN_TIME", "14:00").split(":")
    evening = os.getenv("EVENING_CHECK_IN_TIME", "20:00").split(":")

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
        f"evening={evening[0]}:{evening[1]} (mon-fri, {tz})"
    )
    return scheduler
