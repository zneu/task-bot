import anthropic
import json
import os
from pathlib import Path

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def get_response(system_prompt: str, messages: list, max_tokens: int = 500) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


def chat(user_message: str, conversation_history: list) -> str:
    """General chat — Claude responds conversationally."""
    system = (
        "You are an accountability partner. Be direct, warm, and brief. "
        "Help the user stay focused and organized."
    )
    messages = conversation_history + [{"role": "user", "content": user_message}]
    return get_response(system, messages)


def extract_from_dump(text: str) -> dict:
    """Extract tasks, people, ideas, commitments from a brain dump."""
    system = (PROMPTS_DIR / "brain_dump.txt").read_text()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text
    # Strip code fences if present
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
    return json.loads(raw.strip())


async def get_checkin_context() -> str:
    """Pull last 7 days of check-ins for Claude context."""
    from database.connection import AsyncSessionLocal
    from database.models import CheckIn
    from sqlalchemy import select
    from datetime import datetime, timedelta, timezone

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CheckIn)
            .where(CheckIn.created_at >= week_ago)
            .order_by(CheckIn.created_at)
        )
        checkins = result.scalars().all()

    if not checkins:
        return "No check-in history yet."

    lines = ["Check-in history (last 7 days):"]
    for c in checkins:
        date = c.created_at.strftime("%a %m/%d")
        lines.append(f"  [{date}] {c.type}: {c.summary or 'No summary'}")
    return "\n".join(lines)
