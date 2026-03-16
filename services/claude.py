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


def summarize_note(text: str) -> dict:
    """Summarize a transcript into a structured note.

    Returns dict: {"title": "...", "summary": "- bullet 1\n- bullet 2", "tags": ["tag1", "tag2"]}
    """
    system = """You are a note summarizer. Given a raw transcript (often from a voice memo), produce a structured summary.

Return ONLY valid JSON with these fields:
- "title": A short descriptive title (5-10 words) capturing the main topic
- "summary": Key points as bullet points (each line starts with "- "). Preserve the speaker's intent and meaning. Be thorough but concise.
- "tags": 1-5 topic tags from this list: General, Music, Throne, Business, Personal. Pick the most relevant.

Do NOT add action items or task suggestions. Just capture what was said."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
    return json.loads(raw.strip())


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


def classify_intent(text: str, projects: list[str], task_map: dict, task_context: str = "", people: list[str] = None) -> dict:
    """Classify user message into a structured intent using Claude.

    Returns dict like:
        {"intent": "list", "project": "Thrown", "show_all": false}
        {"intent": "done", "num": "3"}
        {"intent": "add", "title": "Buy groceries", "priority": "high", "project": "Home", "due": "2026-03-20"}
        {"intent": "chat"}
        {"intent": "dump", "text": "I need to call Sarah..."}
    """
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%A, %Y-%m-%d")

    # Build task list context for num references
    task_list_str = ""
    if task_map:
        task_list_str = "\nCurrently displayed task numbers:\n" + "\n".join(
            f"  #{k}" for k in sorted(task_map.keys(), key=int)
        )

    people_str = json.dumps(people) if people else "none"

    system = f"""You are a task management command parser. Today is {today}.

The user's projects are: {json.dumps(projects) if projects else "none yet"}
{task_list_str}

Current open tasks:
{task_context}

Tracked people: {people_str}

Classify the user's message into ONE of these intents and return ONLY valid JSON:

- {{"intent": "list"}} — user wants to see tasks. Optional: "project" (matched to known projects), "show_all" (true if they want done tasks too)
- {{"intent": "done", "num": "N"}} — mark task #N as done
- {{"intent": "doing", "num": "N"}} — mark task #N as in progress
- {{"intent": "add", "title": "...", "priority": "high|medium|low", "project": "...", "due": "YYYY-MM-DD"}} — add a new task. Only include fields the user specified. Resolve relative dates (e.g. "Friday" → actual date).
- {{"intent": "edit", "num": "N", "field": "title|priority|project|due|notes", "value": "..."}} — edit a task field. Resolve relative dates.
- {{"intent": "delete", "num": "N"}} — delete a task
- {{"intent": "move", "nums": ["1","3","5"], "project": "..."}} — move tasks to a project. Match project name fuzzily to known projects.
- {{"intent": "people"}} — list tracked people
- {{"intent": "dump", "text": "..."}} — user is brain-dumping multiple tasks/ideas/people at once (long, stream-of-consciousness input with multiple items)
- {{"intent": "note", "text": "..."}} — user wants to save a journal note/thought (not a task). Triggered by "note", "add a note", "journal", or when the user is clearly just recording thoughts/reflections
- {{"intent": "notes"}} — user wants to list or search their notes. Optional: "search": "keyword"
- {{"intent": "help"}} — user wants to know what they can do
- {{"intent": "chat"}} — just conversation, not a task command

Rules:
- Match project names fuzzily (e.g. "throne" → "Thrown" if that project exists)
- Resolve ALL relative dates to absolute YYYY-MM-DD using today's date
- "end of the week" = Friday of this week
- If ambiguous between dump and add, prefer add for single items, dump for multiple
- When the user references a task by name/description (not number), match to the closest task title above
- When the user asks about a person, match to tracked people names fuzzily
- If the message contains MULTIPLE commands (e.g. "delete 11 and move 15,16,17 to Taxes"), return a JSON array of intents: [{{"intent": "delete", "num": "11"}}, {{"intent": "move", "nums": ["15","16","17"], "project": "Taxes"}}]
- For single commands, return a single JSON object (not an array)
- If the message is clearly just chat/greeting/thanks, return chat
- Return ONLY the JSON, no explanation"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
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
