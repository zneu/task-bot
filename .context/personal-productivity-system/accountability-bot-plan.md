# Accountability Bot — Implementation Plan

- **Branch:** `feature/accountability-bot`
- **Date:** 2026-03-14
- **Research:** `.context/personal-productivity-system/accountability-bot-implementation-research.md`

---

## Goal

Build a Telegram accountability bot that accepts voice/text brain dumps, extracts tasks/people/ideas via Claude, saves to Railway Postgres, syncs to Notion, and sends scheduled morning/evening check-ins.

---

## Success Criteria

- [ ] Bot responds to text messages via Claude
- [ ] Bot transcribes voice notes via Groq Whisper
- [ ] Brain dumps are extracted into structured tasks/people/ideas
- [ ] User confirms extracted items before saving
- [ ] Tasks saved to Railway Postgres
- [ ] Tasks synced one-way to Notion database
- [ ] Morning check-in fires on schedule, presents top 3 tasks
- [ ] Evening check-in fires on schedule, tracks completion/avoidance
- [ ] Avoided tasks tracked (avoided_count increments)
- [ ] Only responds to authorized user ID
- [ ] Bot runs via polling (no HTTPS required)

---

## Scope Boundaries

### In Scope (Phases 1-6)
- Telegram polling bot with user auth
- Groq Whisper voice transcription
- Claude API for conversations + structured extraction
- Railway Postgres with SQLAlchemy async models
- Notion one-way sync (push tasks)
- APScheduler morning/evening check-ins
- In-memory conversation state
- Brain dump extraction with confirmation flow
- System prompts for morning/evening/brain dump
- Avoidance pattern detection (avoided_count >= 3)

### Out of Scope
- Multi-user support / auth system
- Webhook mode (using polling instead)
- Two-way Notion sync
- Embeddings / vector search / "what am I missing?"
- Stripe billing
- Web frontend
- Deployment to VPS (separate task after code is working locally)

---

## Phase 1: Foundation

**Goal:** Bot runs, responds to messages via Claude, database tables created.

**Files to create:** 10

#### Step 1.1: Create requirements.txt
- **File:** `requirements.txt` (new)
- **Change:** Define all Python dependencies
- **Code:**
```
fastapi
uvicorn[standard]
python-telegram-bot==21.0
anthropic
groq
sqlalchemy[asyncio]
asyncpg
notion-client
apscheduler
pytz
python-dotenv
```
- **Verification:** `pip install -r requirements.txt` succeeds

#### Step 1.2: Create database/connection.py
- **File:** `database/connection.py` (new)
- **Change:** Async SQLAlchemy engine + session factory. Adapted from contacts_ai/backend/app/db/session.py.
- **Code:**
```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
import os

from dotenv import load_dotenv
load_dotenv()

database_url = os.getenv("DATABASE_URL", "")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif database_url.startswith("postgresql://") and "+asyncpg" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

Base = declarative_base()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```
- **Verification:** `python -c "import asyncio; from database.connection import init_db; asyncio.run(init_db())"` creates tables in Railway Postgres

#### Step 1.3: Create database/__init__.py
- **File:** `database/__init__.py` (new)
- **Change:** Empty init file
- **Code:** empty file
- **Verification:** `from database.connection import init_db` works

#### Step 1.4: Create database/models.py
- **File:** `database/models.py` (new)
- **Change:** Define Task, CheckIn, Capture, Person models per spec
- **Code:**
```python
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, Boolean, DateTime, Integer, JSON
from database.connection import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True, default=new_id)
    title = Column(String, nullable=False)
    status = Column(String, default="not_started")
    priority = Column(String, default="medium")
    project = Column(String, nullable=True)
    due_date = Column(DateTime, nullable=True)
    committed_today = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    notion_id = Column(String, nullable=True)
    avoided_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class CheckIn(Base):
    __tablename__ = "checkins"
    id = Column(String, primary_key=True, default=new_id)
    type = Column(String)
    committed_task_ids = Column(JSON, default=list)
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Capture(Base):
    __tablename__ = "captures"
    id = Column(String, primary_key=True, default=new_id)
    raw_text = Column(Text)
    source = Column(String)
    items_created = Column(JSON, default=list)
    processed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class Person(Base):
    __tablename__ = "people"
    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    context = Column(Text, nullable=True)
    follow_up_action = Column(Text, nullable=True)
    follow_up_date = Column(DateTime, nullable=True)
    notion_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
```
- **Verification:** init_db() creates all 4 tables in Railway Postgres

#### Step 1.5: Create services/__init__.py and services/claude.py
- **File:** `services/__init__.py` (new, empty)
- **File:** `services/claude.py` (new)
- **Change:** Claude API client with basic response and brain dump extraction
- **Code:**
```python
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
```
- **Verification:** `python -c "from services.claude import chat; print(chat('hello', []))"` returns a response

#### Step 1.6: Create bot/__init__.py and bot/state.py
- **File:** `bot/__init__.py` (new, empty)
- **File:** `bot/state.py` (new)
- **Change:** In-memory state management per user
- **Code:**
```python
user_states = {}


def get_state(user_id: str) -> dict:
    if user_id not in user_states:
        user_states[user_id] = {
            "mode": "idle",
            "committed_task_ids": [],
            "conversation_history": [],
            "pending_items": None,
        }
    return user_states[user_id]


def add_to_history(user_id: str, role: str, content: str):
    state = get_state(user_id)
    state["conversation_history"].append({"role": role, "content": content})
    # Keep last 10 messages
    if len(state["conversation_history"]) > 10:
        state["conversation_history"] = state["conversation_history"][-10:]
```
- **Verification:** Import and call `get_state("123")` returns default state dict

#### Step 1.7: Create bot/handlers.py (Phase 1 — text only)
- **File:** `bot/handlers.py` (new)
- **Change:** Telegram message handler with auth check, routes text to Claude
- **Code:**
```python
import os
from telegram import Update
from telegram.ext import ContextTypes
from services.claude import chat
from bot.state import get_state, add_to_history

AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID", "")


def is_authorized(update: Update) -> bool:
    return str(update.effective_user.id) == AUTHORIZED_USER_ID


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    user_id = str(update.effective_user.id)
    text = update.message.text

    if text and text.lower() == "ping":
        await update.message.reply_text("pong")
        return

    state = get_state(user_id)

    # Phase 1: just chat with Claude
    response = chat(text, state["conversation_history"])
    add_to_history(user_id, "user", text)
    add_to_history(user_id, "assistant", response)

    await update.message.reply_text(response)
```
- **Verification:** Send "ping" → get "pong". Send any text → get Claude response.

#### Step 1.8: Create main.py
- **File:** `main.py` (new)
- **Change:** FastAPI app with /health endpoint + Telegram polling bot startup
- **Code:**
```python
import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from telegram.ext import Application, MessageHandler, filters
from bot.handlers import handle_message
from database.connection import init_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = FastAPI()
telegram_app = None


@app.get("/health")
async def health():
    return {"status": "ok"}


async def start_telegram():
    global telegram_app
    telegram_app = (
        Application.builder()
        .token(os.getenv("TELEGRAM_BOT_TOKEN"))
        .build()
    )
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    logger.info("Telegram bot started polling")


@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("Database tables created")
    await start_telegram()
    logger.info("Bot ready")


@app.on_event("shutdown")
async def shutdown():
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
```
- **Verification:** `uvicorn main:app --port 8001` starts. `curl localhost:8001/health` returns `{"status":"ok"}`. Message the bot on Telegram → Claude replies.

#### Step 1.9: Create prompts directory with brain_dump.txt
- **File:** `prompts/brain_dump.txt` (new)
- **Change:** Brain dump extraction prompt from spec
- **Code:**
```
Extract structured information from this brain dump. Return ONLY valid JSON. No markdown, no code fences, no other text.

Categories:
- tasks: things to do { name, project (if mentioned), priority (High/Medium/Low, default Medium) }
- people: people mentioned { name, context, follow_up_action }
- ideas: ideas or future possibilities { description, project }
- commitments: things promised or scheduled { description, due_date (ISO format or null) }

Err toward extracting more rather than less.

{
  "tasks": [],
  "people": [],
  "ideas": [],
  "commitments": []
}
```
- **Verification:** File exists and is readable by services/claude.py

#### Step 1.10: Create prompts/morning.txt and prompts/evening.txt
- **File:** `prompts/morning.txt` (new)
- **Code:**
```
You are an accountability partner for someone with ADHD building multiple products and a music career simultaneously. They struggle with avoidance and distraction, not with knowing what to do.

You will be given their open tasks from the database.

Your job:
1. Pick the 1-3 highest leverage tasks for today (consider: priority, how many days avoided, due dates, project momentum)
2. Present them clearly, ask for confirmation
3. If they want to swap, help — but push back gently on obvious avoidance
4. Direct, warm, brief. No preamble.

Format:
"Good morning. Your focus today:
1. [Task]
2. [Task]
3. [Task]

Confirm these or tell me what to swap?"
```

- **File:** `prompts/evening.txt` (new)
- **Code:**
```
You are an accountability partner doing an end-of-day check-in. You have the tasks this person committed to this morning.

Your job:
1. Ask what happened with each committed task — done, in progress, or avoided?
2. If avoided, ask ONE honest question about the block. Don't lecture.
3. If you see a pattern (same task avoided multiple times), name it directly, without judgment.
4. End with: "What are you carrying into tomorrow?"

Be honest, not harsh. This person is building real things. Be on their side.
```
- **Verification:** Files exist in prompts/

---

## Phase 2: Voice Notes

**Goal:** Bot transcribes Telegram voice messages via Groq Whisper and treats them as text input.

**Files to create:** 1 new, 1 modified

#### Step 2.1: Create services/groq.py
- **File:** `services/groq.py` (new)
- **Change:** Groq Whisper transcription function
- **Code:**
```python
import os
from groq import Groq

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = groq_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        response_format="text",
    )
    return transcription.strip()
```
- **Verification:** Pass a .ogg audio file bytes → get text back

#### Step 2.2: Update bot/handlers.py — add voice handler
- **File:** `bot/handlers.py`
- **Change:** Add voice message detection, download, transcription. Add voice handler to imports and export.
- **After:**
```python
import os
from telegram import Update
from telegram.ext import ContextTypes
from services.claude import chat
from services.groq import transcribe_voice
from bot.state import get_state, add_to_history

AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID", "")


def is_authorized(update: Update) -> bool:
    return str(update.effective_user.id) == AUTHORIZED_USER_ID


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    user_id = str(update.effective_user.id)
    text = update.message.text

    if text and text.lower() == "ping":
        await update.message.reply_text("pong")
        return

    state = get_state(user_id)

    response = chat(text, state["conversation_history"])
    add_to_history(user_id, "user", text)
    add_to_history(user_id, "assistant", response)

    await update.message.reply_text(response)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    audio_bytes = await file.download_as_bytearray()

    await update.message.reply_text("🎙️ Transcribing...")

    text = transcribe_voice(bytes(audio_bytes))

    await update.message.reply_text(f"🎙️ I heard:\n_{text}_", parse_mode="Markdown")

    # Process as regular text input
    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    response = chat(text, state["conversation_history"])
    add_to_history(user_id, "user", text)
    add_to_history(user_id, "assistant", response)

    await update.message.reply_text(response)
```
- **Verification:** Send voice note to bot → see transcription → get Claude response

#### Step 2.3: Update main.py — register voice handler
- **File:** `main.py`
- **Change:** Import handle_voice and add MessageHandler for voice messages
- **Add after existing handler registration:**
```python
from bot.handlers import handle_message, handle_voice

# In start_telegram():
telegram_app.add_handler(
    MessageHandler(filters.VOICE, handle_voice)
)
```
- **Verification:** Voice notes handled by bot

---

## Phase 3: Brain Dump

**Goal:** Text or voice input → Claude extracts tasks/people/ideas → user confirms → saves to Postgres → syncs to Notion.

**Files to create:** 1 new, 3 modified

#### Step 3.1: Create services/notion.py
- **File:** `services/notion.py` (new)
- **Change:** One-way push sync to Notion
- **Code:**
```python
import os
import logging
from notion_client import Client

logger = logging.getLogger(__name__)

notion = Client(auth=os.getenv("NOTION_API_KEY"))
DB_ID = os.getenv("NOTION_TASKS_DATABASE_ID")


def push_task(task) -> str | None:
    """Push or update a task in Notion. Returns Notion page ID."""
    try:
        status_map = {
            "not_started": "Not Started",
            "in_progress": "In Progress",
            "done": "Done",
            "avoided": "Avoided",
        }
        properties = {
            "Name": {"title": [{"text": {"content": task.title}}]},
            "Status": {"select": {"name": status_map.get(task.status, "Not Started")}},
            "Priority": {"select": {"name": task.priority.title()}},
            "Committed Today": {"checkbox": task.committed_today},
        }
        if task.project:
            properties["Project"] = {"select": {"name": task.project}}
        if task.notes:
            properties["Notes"] = {
                "rich_text": [{"text": {"content": task.notes[:2000]}}]
            }
        if task.due_date:
            properties["Due Date"] = {
                "date": {"start": task.due_date.isoformat()[:10]}
            }

        if task.notion_id:
            notion.pages.update(page_id=task.notion_id, properties=properties)
            return task.notion_id
        else:
            result = notion.pages.create(
                parent={"database_id": DB_ID}, properties=properties
            )
            return result["id"]
    except Exception:
        logger.exception(f"Failed to sync task '{task.title}' to Notion")
        return None
```
- **Verification:** Create a Task object, call push_task → appears in Notion database

#### Step 3.2: Update bot/state.py — add pending items and mode helpers
- **File:** `bot/state.py`
- **Change:** Add helper functions for mode transitions and pending items
- **After:**
```python
user_states = {}


def get_state(user_id: str) -> dict:
    if user_id not in user_states:
        user_states[user_id] = {
            "mode": "idle",
            "committed_task_ids": [],
            "conversation_history": [],
            "pending_items": None,
        }
    return user_states[user_id]


def set_mode(user_id: str, mode: str):
    get_state(user_id)["mode"] = mode


def set_pending(user_id: str, items: dict):
    state = get_state(user_id)
    state["pending_items"] = items
    state["mode"] = "brain_dump_confirm"


def clear_pending(user_id: str):
    state = get_state(user_id)
    state["pending_items"] = None
    state["mode"] = "idle"


def add_to_history(user_id: str, role: str, content: str):
    state = get_state(user_id)
    state["conversation_history"].append({"role": role, "content": content})
    if len(state["conversation_history"]) > 10:
        state["conversation_history"] = state["conversation_history"][-10:]
```
- **Verification:** Mode transitions work correctly

#### Step 3.3: Update bot/handlers.py — full brain dump flow
- **File:** `bot/handlers.py`
- **Change:** Route all non-command text/voice through brain dump extraction. Show extracted items. Handle confirmation ("yes"/"y") to save. Handle rejection ("no"/"n") to discard.
- **After:** (full file replacement)
```python
import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from services.claude import chat, extract_from_dump
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
        lines.append("📋 *Tasks:*")
        for t in items["tasks"]:
            pri = t.get("priority", "Medium")
            proj = f" [{t.get('project')}]" if t.get("project") else ""
            lines.append(f"  • {t['name']} ({pri}){proj}")
    if items.get("people"):
        lines.append("\n👤 *People:*")
        for p in items["people"]:
            action = f" → {p.get('follow_up_action')}" if p.get("follow_up_action") else ""
            lines.append(f"  • {p['name']}: {p.get('context', '')}{action}")
    if items.get("ideas"):
        lines.append("\n💡 *Ideas:*")
        for i in items["ideas"]:
            lines.append(f"  • {i['description']}")
    if items.get("commitments"):
        lines.append("\n🤝 *Commitments:*")
        for c in items["commitments"]:
            due = f" (by {c.get('due_date')})" if c.get("due_date") else ""
            lines.append(f"  • {c['description']}{due}")
    lines.append("\n*Save these? (yes/no)*")
    return "\n".join(lines)


async def process_input(text: str, update: Update, source: str = "text"):
    """Process text input — extract or chat depending on content."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    # If awaiting confirmation
    if state["mode"] == "brain_dump_confirm":
        await handle_confirmation(text, update)
        return

    # Try to extract structured items
    try:
        items = extract_from_dump(text)
        has_items = any(items.get(k) for k in ["tasks", "people", "ideas", "commitments"])
    except Exception:
        logger.exception("Extraction failed, falling back to chat")
        has_items = False
        items = None

    if has_items:
        set_pending(user_id, items)
        formatted = format_extracted(items)
        await update.message.reply_text(formatted, parse_mode="Markdown")
    else:
        # Nothing to extract — just chat
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
        await update.message.reply_text("Reply *yes* to save or *no* to discard.", parse_mode="Markdown")


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
    await update.message.reply_text(f"✅ Saved {summary}.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text
    if text and text.lower() == "ping":
        await update.message.reply_text("pong")
        return

    await process_input(text, update, source="text")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    audio_bytes = await file.download_as_bytearray()

    await update.message.reply_text("🎙️ Transcribing...")

    try:
        text = transcribe_voice(bytes(audio_bytes))
    except Exception:
        logger.exception("Transcription failed")
        await update.message.reply_text("❌ Transcription failed. Try again.")
        return

    await update.message.reply_text(f"🎙️ I heard:\n_{text}_", parse_mode="Markdown")

    await process_input(text, update, source="voice")
```
- **Verification:** Send "I need to call John about the project proposal and finish the landing page by Friday" → see extracted tasks/people → reply "yes" → saved to Postgres + Notion

---

## Phase 4: Morning Check-In

**Goal:** Scheduler fires in the morning, Claude picks top 3 tasks, user confirms, tasks marked as committed.

**Files to create:** 1 new, 1 modified

#### Step 4.1: Create bot/scheduler.py
- **File:** `bot/scheduler.py` (new)
- **Change:** APScheduler with morning and evening cron triggers
- **Code:**
```python
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
    """Pull open tasks, ask Claude for top 3, send to user."""
    logger.info("Morning check-in triggered")
    chat_id = int(AUTHORIZED_USER_ID)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_(["not_started", "in_progress"]))
        )
        tasks = result.scalars().all()

    if not tasks:
        await telegram_app.bot.send_message(chat_id, "Good morning! No open tasks. Enjoy the clarity ☀️")
        return

    # Format tasks for Claude
    task_list = "\n".join(
        f"- [{t.id[:8]}] {t.title} (priority: {t.priority}, avoided: {t.avoided_count}x"
        + (f", due: {t.due_date.strftime('%Y-%m-%d')}" if t.due_date else "")
        + ")"
        for t in tasks
    )

    system = (PROMPTS_DIR / "morning.txt").read_text()
    user_msg = f"Here are my open tasks:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=500)

    from bot.state import get_state, set_mode
    state = get_state(AUTHORIZED_USER_ID)
    state["mode"] = "morning"
    # Store all task IDs so confirmation handler can find them
    state["_morning_tasks"] = {t.id[:8]: t.id for t in tasks}

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
    user_msg = f"Tasks I committed to today:\n\n{task_list}"

    response = get_response(system, [{"role": "user", "content": user_msg}], max_tokens=500)

    from bot.state import get_state
    state = get_state(AUTHORIZED_USER_ID)
    state["mode"] = "evening"
    state["committed_task_ids"] = [t.id for t in tasks]

    await telegram_app.bot.send_message(chat_id, response)


def start_scheduler(telegram_app):
    tz = pytz.timezone(os.getenv("TIMEZONE", "America/Lima"))
    scheduler = AsyncIOScheduler(timezone=tz)

    morning = os.getenv("MORNING_CHECK_IN_TIME", "08:00").split(":")
    evening = os.getenv("EVENING_CHECK_IN_TIME", "20:00").split(":")

    scheduler.add_job(
        morning_checkin,
        CronTrigger(hour=int(morning[0]), minute=int(morning[1]), timezone=tz),
        args=[telegram_app],
    )
    scheduler.add_job(
        evening_checkin,
        CronTrigger(hour=int(evening[0]), minute=int(evening[1]), timezone=tz),
        args=[telegram_app],
    )

    scheduler.start()
    logger.info(f"Scheduler started: morning={morning[0]}:{morning[1]}, evening={evening[0]}:{evening[1]} ({tz})")
    return scheduler
```
- **Verification:** Temporarily set check-in time to 1 minute from now, observe message sent

#### Step 4.2: Update main.py — start scheduler
- **File:** `main.py`
- **Change:** Import and start scheduler on startup
- **Add to startup():**
```python
from bot.scheduler import start_scheduler

# In startup(), after start_telegram():
start_scheduler(telegram_app)
```
- **Verification:** App starts, logs show scheduler started with correct times

#### Step 4.3: Update bot/handlers.py — morning confirmation flow
- **File:** `bot/handlers.py`
- **Change:** When mode is "morning", parse user's confirmation and mark tasks as committed_today. When mode is "evening", parse completion status and update tasks.
- **Add to process_input(), before the brain dump extraction try block:**
```python
    # Morning check-in confirmation
    if state["mode"] == "morning":
        await handle_morning_response(text, update)
        return

    # Evening check-in response
    if state["mode"] == "evening":
        await handle_evening_response(text, update)
        return
```
- **New functions to add:**
```python
async def handle_morning_response(text: str, update: Update):
    """Handle response to morning check-in — mark tasks as committed."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    # Let Claude interpret the response in context
    from services.claude import get_response
    from pathlib import Path

    system = """The user is responding to their morning check-in. Parse their response.
If they confirm (e.g. "yes", "looks good", "let's go"), respond with: CONFIRMED
If they want to adjust, help them and ask again.
Be brief."""

    response = get_response(system, [
        {"role": "user", "content": text}
    ])

    if "CONFIRMED" in response:
        # Mark all open tasks as committed (Claude already picked the top 3)
        from database.connection import AsyncSessionLocal
        from database.models import Task
        from sqlalchemy import select, update as sql_update

        async with AsyncSessionLocal() as session:
            await session.execute(
                sql_update(Task)
                .where(Task.status.in_(["not_started", "in_progress"]))
                .values(committed_today=True)
            )
            await session.commit()

        state["mode"] = "idle"
        await update.message.reply_text("✅ Locked in. Go get it.")
    else:
        await update.message.reply_text(response)


async def handle_evening_response(text: str, update: Update):
    """Handle response to evening check-in — update task statuses."""
    user_id = str(update.effective_user.id)
    state = get_state(user_id)

    from services.claude import get_response
    from database.connection import AsyncSessionLocal
    from database.models import Task, CheckIn
    from services.notion import push_task
    from sqlalchemy import select

    system = """The user is reporting on their day. For each task they committed to, determine the status.
Return ONLY valid JSON: {"updates": [{"title_fragment": "...", "status": "done|in_progress|avoided"}]}
If you can't parse clearly, ask a follow-up question instead of returning JSON."""

    response = get_response(system, [
        {"role": "user", "content": f"Committed tasks: {state['committed_task_ids']}\n\nMy update: {text}"}
    ], max_tokens=500)

    import json
    try:
        data = json.loads(response.strip())
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

            # Match update to task
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

        # Save check-in record
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

    msg = "✅ Updated. "
    if chronic:
        names = ", ".join(t.title for t in chronic)
        msg += f"\n\n⚠️ Chronically avoided: {names}. What's really blocking these?"

    msg += "\n\nWhat are you carrying into tomorrow?"
    await update.message.reply_text(msg)
```
- **Verification:** Morning fires → confirm → tasks marked committed. Evening fires → report status → tasks updated in Postgres + Notion. Avoided tasks increment counter.

---

## Phase 5: Evening Check-In Refinements

Already covered in Phase 4 implementation above. Phase 5 from the spec (evening check-in) is built alongside Phase 4 since the scheduler, state machine, and handlers are interdependent.

---

## Phase 6: Memory & Patterns

**Goal:** Claude has 7-day check-in history context. Weekly summary.

**Files modified:** 2

#### Step 6.1: Update services/claude.py — add history context
- **File:** `services/claude.py`
- **Change:** Add function to build context from recent check-ins
- **Add:**
```python
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
```

#### Step 6.2: Update bot/scheduler.py — inject history into check-in prompts
- **File:** `bot/scheduler.py`
- **Change:** In both morning_checkin and evening_checkin, prepend check-in history to Claude's context.
- **Add before Claude call in morning_checkin:**
```python
from services.claude import get_checkin_context
history = await get_checkin_context()
user_msg = f"{history}\n\nHere are my open tasks:\n\n{task_list}"
```
- **Same pattern in evening_checkin.**
- **Verification:** After a few days of use, Claude references previous days' patterns in check-in messages.

#### Step 6.3: Add weekly summary job to scheduler
- **File:** `bot/scheduler.py`
- **Change:** Add Sunday evening weekly summary
- **Add job in start_scheduler():**
```python
scheduler.add_job(
    weekly_summary,
    CronTrigger(day_of_week="sun", hour=int(evening[0]), minute=int(evening[1]) - 30, timezone=tz),
    args=[telegram_app],
)
```
- **Add function:**
```python
async def weekly_summary(telegram_app):
    """Send weekly summary 30 min before Sunday evening check-in."""
    chat_id = int(AUTHORIZED_USER_ID)
    from services.claude import get_response, get_checkin_context

    history = await get_checkin_context()

    system = """Review this week's check-in history. Provide:
1. What got done (celebrate wins)
2. What slipped and patterns you notice
3. Top priorities for next week
4. Any tasks that should be dropped or rethought

Be direct and supportive. Under 200 words."""

    response = get_response(system, [{"role": "user", "content": history}], max_tokens=500)
    await telegram_app.bot.send_message(chat_id, f"📊 *Weekly Review*\n\n{response}", parse_mode="Markdown")
```
- **Verification:** Trigger manually → get weekly summary based on check-in data

---

## Testing Plan

### Manual Testing (Primary — single-user bot)
- [ ] `curl localhost:8001/health` returns OK
- [ ] Send "ping" → "pong"
- [ ] Send any text → Claude responds
- [ ] Send voice note → transcription echoed → Claude responds
- [ ] Send brain dump text → extracted items shown → "yes" → saved to Postgres
- [ ] Check Notion database → task appears
- [ ] Temporarily set morning time to now+1min → check-in fires
- [ ] Reply to confirm → tasks marked committed_today in DB
- [ ] Temporarily set evening time → check-in fires
- [ ] Report "done/avoided" → task statuses updated
- [ ] Avoided task 3+ times → warning message appears
- [ ] Restart bot → state resets to idle, DB data persists

### Database Verification
- [ ] `SELECT * FROM tasks;` shows created tasks
- [ ] `SELECT * FROM checkins;` shows check-in records
- [ ] `SELECT * FROM captures;` shows raw captures
- [ ] `SELECT * FROM people;` shows extracted people

---

## Rollback Plan

- Git revert any commit
- Database: `DROP TABLE tasks, checkins, captures, people CASCADE;` and re-run init_db() (destructive — only for fresh start during development)
- Notion: manually delete pages (no automated cleanup needed)

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Groq rate limit on free tier | Medium | Low | Catch exception, reply "try again in a moment" |
| Claude returns invalid JSON from brain dump | Medium | Medium | Code fence stripping + try/except fallback to chat |
| APScheduler timezone drift | Low | Medium | Explicit pytz timezone, test with Lima |
| Telegram file download timeout | Low | Low | Wrap in try/except, ask user to resend |
| Notion sync fails | Low | Low | Log error, don't block task creation — Postgres is source of truth |
| Railway Postgres connection drops | Low | Medium | pool_pre_ping=True reconnects automatically |

---

## Open Questions

None — all decisions resolved.

---

## Phase Prompts

### Phase 1 Prompt (Foundation)
```
Read .context/personal-productivity-system/accountability-bot-plan.md and then /implement accountability bot — Execute Phase 1 (Foundation).
Scope: Project scaffolding, database models, FastAPI app, Telegram polling, Claude basic chat.
Steps: 1.1–1.10. Files: requirements.txt, database/connection.py, database/models.py, database/__init__.py, services/__init__.py, services/claude.py, bot/__init__.py, bot/state.py, bot/handlers.py, main.py, prompts/brain_dump.txt, prompts/morning.txt, prompts/evening.txt.
Prerequisite: .env file configured with all keys.
Verification: uvicorn main:app --port 8001 starts. Telegram bot responds to text. curl /health returns ok.
```

### Phase 2 Prompt (Voice Notes)
```
Read .context/personal-productivity-system/accountability-bot-plan.md and then /implement accountability bot — Execute Phase 2 (Voice Notes).
Scope: Groq Whisper integration, voice message handling.
Steps: 2.1–2.3. Files: services/groq.py, bot/handlers.py (update), main.py (update).
Prerequisite: Phase 1 complete and committed.
Verification: Send voice note to bot → transcription echoed → Claude responds.
```

### Phase 3 Prompt (Brain Dump)
```
Read .context/personal-productivity-system/accountability-bot-plan.md and then /implement accountability bot — Execute Phase 3 (Brain Dump).
Scope: Brain dump extraction, confirmation flow, Postgres save, Notion sync.
Steps: 3.1–3.3. Files: services/notion.py, bot/state.py (update), bot/handlers.py (full rewrite).
Prerequisite: Phase 2 complete and committed.
Verification: Send brain dump → see extracted items → confirm → check Postgres and Notion.
```

### Phase 4+5 Prompt (Morning + Evening Check-Ins)
```
Read .context/personal-productivity-system/accountability-bot-plan.md and then /implement accountability bot — Execute Phase 4+5 (Check-Ins).
Scope: APScheduler, morning/evening check-in flows, task commitment, status updates, avoidance tracking.
Steps: 4.1–4.3. Files: bot/scheduler.py, main.py (update), bot/handlers.py (update).
Prerequisite: Phase 3 complete and committed.
Verification: Set check-in time to now+1min → fires → confirm → tasks committed. Evening → report → statuses updated.
```

### Phase 6 Prompt (Memory & Patterns)
```
Read .context/personal-productivity-system/accountability-bot-plan.md and then /implement accountability bot — Execute Phase 6 (Memory & Patterns).
Scope: 7-day check-in history context, weekly summary.
Steps: 6.1–6.3. Files: services/claude.py (update), bot/scheduler.py (update).
Prerequisite: Phase 4+5 complete and committed.
Verification: Check-in messages reference previous days. Weekly summary fires Sunday.
```
