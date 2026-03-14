# Accountability Bot — Implementation Research

**Date:** 2026-03-14
**Scope:** What exists, what needs to be built, what can be reused, and what decisions remain for implementing the accountability bot spec.

---

## Overview

This research documents the current state of Zachary's existing infrastructure and codebases to determine exactly what needs to be built vs. reused for the accountability bot. The spec calls for: Python/FastAPI, Telegram bot, Claude API, Groq Whisper, Railway Postgres, Notion sync, APScheduler, deployed on existing Hetzner VPS.

---

## Key Files: Existing Infrastructure

### remote-claude (Telegram Bot on Hetzner)

| File | Role | Lines | Reuse Potential |
|---|---|---|---|
| `/Users/zacharyneumann/code/remote-claude/bot.py` | Full Telegram bot — polling, sessions, Claude via subprocess | 589 | **Reference only** — different architecture (subprocess vs API) |
| `/Users/zacharyneumann/code/remote-claude/webhook.py` | GitHub webhook → auto-deploy for 5 repos | 67 | **Extend** — add accountability-bot to DEPLOY_SCRIPTS dict |
| `/Users/zacharyneumann/code/remote-claude/deploy.sh` | git pull → pip install → systemctl restart → rollback on fail | 22 | **Copy and adapt** for accountability-bot |
| `/Users/zacharyneumann/code/remote-claude/.env.example` | Bot token, user ID, work dir, timeout, allowed tools | 6 | **Reference** — user ID is `1304690403` |
| `/Users/zacharyneumann/code/remote-claude/claude-bot.service` | systemd unit for remote-claude | ~10 | **Copy and adapt** — change port to 8001, change paths |

**VPS details:**
- Host: `root@95.217.217.145` (Hetzner)
- remote-claude runs as `claude-bot` systemd service (polling mode, no port binding)
- webhook.py listens on port 9000 for GitHub webhook auto-deploys
- Already deploys 5 projects: remote-claude, calida, astrology-ai, coaching-ai, contacts-ai
- Uses `.venv` virtual environment pattern
- Rollback on failed deploy (git checkout previous commit)

**Key difference from spec:** remote-claude uses `claude -p` subprocess (pipes to Claude Code CLI). The accountability bot spec calls for direct Anthropic API calls via the `anthropic` Python SDK. These are fundamentally different — remote-claude gives Claude access to tools (Bash, Read, Write, etc.), while the accountability bot just needs conversational AI + structured extraction. Direct API is correct for this use case.

### contacts_ai (Connector CRM — The Cortext Vision)

| File | Role | Lines | Reuse Potential |
|---|---|---|---|
| `/Users/zacharyneumann/code/contacts_ai/backend/app/db/session.py` | SQLAlchemy async engine + session factory + init_db() | 48 | **Direct copy** — identical pattern needed |
| `/Users/zacharyneumann/code/contacts_ai/backend/app/models/contact.py` | Contact SQLAlchemy model with UUID PK, relationships, embedding text | 75 | **Reference** — Person model in accountability bot is simpler |
| `/Users/zacharyneumann/code/contacts_ai/backend/app/models/voice_memo.py` | VoiceMemo model (audio_url, transcript, parsed_data JSONB) | 26 | **Reference** — Capture model in spec is similar |
| `/Users/zacharyneumann/code/contacts_ai/backend/app/services/ai_service.py` | Claude API integration, voice memo parsing, contact extraction | 227 | **Partial reuse** — `parse_voice_memo()` pattern directly applicable |
| `/Users/zacharyneumann/code/contacts_ai/backend/app/core/config.py` | Pydantic Settings for config | ~30 | **Reference** — spec uses plain dotenv instead |
| `/Users/zacharyneumann/code/contacts_ai/backend/requirements.txt` | FastAPI + SQLAlchemy + asyncpg + anthropic + openai + pinecone | 20 | **Reference** — overlapping deps |

**contacts_ai stack:**
- FastAPI backend on Railway (port 5003)
- Next.js 14 frontend on Railway
- PostgreSQL on Railway
- Pinecone for vector search
- Anthropic Claude API (claude-sonnet-4-20250514)
- Google OAuth
- Alembic for migrations
- **Live at:** contacts.audioalchemycollective.com

**Critical reusable patterns:**
1. `db/session.py` — async SQLAlchemy engine with `postgresql+asyncpg://` URL conversion. This is exactly what the accountability bot needs.
2. `ai_service.py` `parse_voice_memo()` — extracts structured data from transcript via Claude. Same pattern as `extract_from_dump()` in the spec.
3. `voice_memo.py` model — transcript + parsed_data (JSONB) pattern maps to the Capture model.
4. Deploy pattern — same VPS, same systemd approach.

---

## Architecture: What Exists vs. What's New

### Already Exists (Reuse/Reference)

| Component | Source | Status |
|---|---|---|
| Hetzner VPS | remote-claude | Running, has deploy pipeline |
| GitHub webhook auto-deploy | remote-claude/webhook.py | Running on port 9000, add new repo entry |
| Deploy script pattern | remote-claude/deploy.sh | Copy and adapt |
| systemd service pattern | remote-claude/claude-bot.service | Copy and adapt |
| SQLAlchemy async + asyncpg setup | contacts_ai/db/session.py | Direct copy with minor changes |
| Claude API integration pattern | contacts_ai/ai_service.py | Adapt for accountability prompts |
| Voice transcript → structured data extraction | contacts_ai/ai_service.py:153-192 | Same pattern, different schema |
| Telegram bot auth (user ID check) | remote-claude/bot.py:112-113 | Same pattern |
| Telegram user ID | remote-claude/.env.example | `1304690403` |
| Railway Postgres | contacts_ai (already paying) | Add new database or reuse project |

### Needs to Be Built (New)

| Component | Spec Section | Complexity |
|---|---|---|
| FastAPI app with webhook endpoint | main.py | Low — standard FastAPI + python-telegram-bot webhook |
| Database models (Task, CheckIn, Capture, Person) | database/models.py | Low — straightforward SQLAlchemy models |
| Telegram webhook handler (text + voice) | bot/handlers.py | Medium — voice download + Groq transcription + state machine |
| Groq Whisper integration | services/groq.py | Low — 10-line function, well-documented API |
| Claude API for accountability conversations | services/claude.py | Low — already have the pattern from contacts_ai |
| Brain dump extraction with structured output | services/claude.py + prompts/ | Medium — needs good prompts + JSON parsing |
| Notion one-way sync (push tasks) | services/notion.py | Low — Notion SDK, straightforward CRUD |
| APScheduler morning/evening check-ins | bot/scheduler.py | Medium — needs to send Telegram messages on schedule + handle responses |
| Conversation state machine | bot/state.py | Medium — modes (idle/morning/evening/brain_dump) + pending confirmations |
| System prompts | prompts/*.txt | Low — already written in the spec |
| Deployment (systemd, nginx, webhook) | infra | Low — copy from remote-claude, adapt |

---

## Decision Points: Resolved by Spec

| Decision | Spec Answer | Notes |
|---|---|---|
| Separate repo or extend remote-claude? | **Separate repo** (Option B) | Different behavior — scheduled messages vs. on-demand |
| Polling or webhook mode? | **Webhook** (FastAPI) | Different from remote-claude (polling). Needs HTTPS endpoint. |
| Direct Claude API or `claude -p`? | **Direct API** | No tool use needed — just conversation + structured extraction |
| Which Claude model? | `claude-sonnet-4-6` | Good balance of quality and cost for conversations |
| Database: Notion or Postgres? | **Postgres is source of truth**, Notion is display | One-way sync Postgres → Notion |
| Voice transcription service? | **Groq Whisper large-v3** | $0.0011/min, ~3s turnaround, free tier available |
| In-memory or DB state? | **In-memory dict** for MVP | Upgrade to Postgres-backed sessions for multi-user |
| Port? | **8001** | Avoids conflict with remote-claude (polling, no port) and webhook.py (9000) |

---

## Decision Points: Still Open

### 1. HTTPS for Telegram Webhook

Telegram webhooks require HTTPS. remote-claude uses polling (no HTTPS needed). The accountability bot spec says webhook mode.

**Options:**
- **A: Use nginx reverse proxy on Hetzner** — If there's already nginx on the VPS for other services, add a server block for the bot domain/subdomain. Route HTTPS to localhost:8001.
- **B: Use Telegram's self-signed certificate** — Telegram supports self-signed certs for webhooks. No nginx needed. Pass cert in `set_webhook()` call.
- **C: Switch to polling mode** — Like remote-claude. Simpler. No HTTPS needed. APScheduler still works. Trade-off: slightly higher latency on messages.
- **D: Use Cloudflare tunnel** — If other services already use Cloudflare.

**What to check on VPS:** Is nginx already installed? Are there existing HTTPS certs? What domain(s) point to this VPS?

**Note:** remote-claude uses polling (`app.run_polling()`), not webhooks. The webhook.py file is for *GitHub* deploy webhooks (port 9000, HTTP, not HTTPS), not Telegram. So there may not be any HTTPS setup on this VPS.

**Recommendation:** Start with **polling mode** (Option C) to match remote-claude's pattern and avoid HTTPS complexity. Webhook mode is marginally better for production but polling is fine for a single-user bot. Can switch later.

### 2. Railway Postgres: New Database or New Service?

contacts_ai already uses Railway Postgres. The accountability bot needs its own.

**Options:**
- **A: Add second Postgres service to same Railway project** — Separate database, same billing. Clean separation.
- **B: Add tables to contacts_ai's database** — Shares the same DB. Simpler but couples the projects.
- **C: New Railway project with its own Postgres** — Complete isolation.

**Recommendation:** Option A (new Postgres service, same project). Keeps billing simple, databases isolated.

### 3. Notion Database: Create New or Use Existing?

**Need to check:** Does Zachary already have a Notion tasks database? The spec defines a specific schema (Name, Status, Priority, Project, Due Date, Committed Today, Notes).

### 4. Telegram Bot: New Bot or Reuse?

**Need:** A separate bot from remote-claude (different @username, different token from BotFather).

### 5. Conversation History Persistence

The spec says in-memory state for MVP, but check-in history (Phase 6) requires 7 days of context. In-memory state is lost on restart.

**Resolution:** CheckIn table in Postgres stores all check-in summaries. Conversation history for Claude context can be rebuilt from the DB. In-memory state only needs current session (what mode are we in, what's pending confirmation). This is fine — restarts just reset to "idle" mode.

---

## Dependencies: What Needs to Happen Before Building

### Before Phase 1 (Foundation)

| Dependency | Action | Who |
|---|---|---|
| Telegram bot token | Create new bot via @BotFather | Zachary (manual, 30 seconds) |
| Railway Postgres | Add Postgres service in Railway dashboard, copy DATABASE_URL | Zachary (manual, 2 minutes) |
| Groq API key | Sign up at console.groq.com, create key | Zachary (manual, 2 minutes) |
| Anthropic API key | Already have (used in contacts_ai) | Already done |
| Notion integration | Create at notion.so/my-integrations, share with tasks DB | Zachary (manual, 3 minutes) |
| Notion tasks database | Create with required schema (or verify existing) | Zachary (manual, 5 minutes) |
| VPS access | SSH to root@95.217.217.145 | Already have |
| Domain/HTTPS (if webhook mode) | nginx + cert setup on VPS | Skip if using polling mode |

### Before Deployment

| Dependency | Action |
|---|---|
| New GitHub repo | Create accountability-bot repo |
| Add to webhook.py | Add `"accountability-bot": "/opt/accountability-bot/deploy.sh"` to DEPLOY_SCRIPTS |
| Create systemd service | Copy and adapt from claude-bot.service |
| Create deploy.sh | Copy and adapt from remote-claude/deploy.sh |

---

## Code Reuse Map

### Direct Copy (adapt paths/names)

```
contacts_ai/backend/app/db/session.py  →  accountability-bot/database/connection.py
  - Change import path for settings
  - Change to use os.getenv("DATABASE_URL") directly instead of Pydantic Settings
  - Keep: engine creation, AsyncSessionLocal, get_db(), init_db()

remote-claude/deploy.sh  →  accountability-bot/deploy.sh
  - Change: cd path to /opt/accountability-bot
  - Change: service name to accountability-bot
  - Keep: git pull, pip install, restart, rollback logic
```

### Pattern Reuse (same approach, different content)

```
contacts_ai/ai_service.py:parse_voice_memo()  →  accountability-bot/services/claude.py:extract_from_dump()
  - Same pattern: transcript → Claude → JSON parsing with code fence stripping
  - Different: schema (tasks/people/ideas vs. contact fields)

contacts_ai/models/voice_memo.py  →  accountability-bot/database/models.py:Capture
  - Same pattern: raw text + parsed JSON + metadata
  - Different: no contact_id FK, add source field

remote-claude/bot.py:_is_allowed()  →  accountability-bot/bot/handlers.py
  - Same pattern: check user ID against env var
```

### New Code (no existing equivalent)

```
bot/scheduler.py          — APScheduler with cron triggers (no existing scheduler code)
bot/state.py              — Conversation state machine (remote-claude has sessions, but different purpose)
services/groq.py          — Groq Whisper transcription (contacts_ai uses OpenAI Whisper, not Groq)
services/notion.py        — Notion push sync (no existing Notion integration in any project)
prompts/*.txt             — All new (provided in spec)
main.py                   — FastAPI + Telegram setup (contacts_ai has FastAPI, but webhook integration is new)
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Groq free tier rate limits | Medium | Low | Fall back to OpenAI Whisper API ($0.006/min) |
| Telegram voice file download issues | Low | Medium | Telegram API is reliable; handle timeouts |
| Claude JSON extraction not valid JSON | Medium | Medium | Strip code fences (contacts_ai already handles this), add retry |
| APScheduler timezone issues | Medium | Medium | Use explicit `pytz.timezone()`, test with Lima timezone |
| VPS port conflicts | Low | Low | 8001 is clear (remote-claude polls, webhook.py on 9000) |
| Notion API rate limits (3 req/sec) | Low | Low | One-way sync with modest volume; batch if needed |
| In-memory state lost on restart | Certain | Low | State is minimal (current mode). Tasks/check-ins survive in Postgres. |

---

## Implementation Order (From Spec, Validated Against Infrastructure)

The spec's build order is sound. Here's the validated sequence with infrastructure steps added:

### Pre-build (Manual Setup — Zachary)
1. Create Telegram bot via @BotFather → get token
2. Add Postgres in Railway dashboard → get DATABASE_URL
3. Get Groq API key from console.groq.com
4. Create Notion integration + share with tasks database
5. Create Notion tasks database with required schema (if not existing)

### Phase 1 — Foundation (Estimated: ~15 files)
Build order from spec, step 1-5:
- `database/connection.py` — copy from contacts_ai, simplify
- `database/models.py` — Task, CheckIn, Capture, Person models
- `main.py` — FastAPI + Telegram polling setup + /health
- `bot/handlers.py` — message handler with user auth
- `services/claude.py` — basic Claude response
- `.env.example`, `requirements.txt`
- Test: message bot → Claude replies

### Phase 2 — Voice Notes (~3 files modified)
- `services/groq.py` — Groq Whisper transcription
- Update `bot/handlers.py` — voice message detection + download + transcription
- Test: send voice note → bot echoes transcription

### Phase 3 — Brain Dump (~5 files modified)
- `prompts/brain_dump.txt` — extraction prompt
- Update `services/claude.py` — `extract_from_dump()` with JSON parsing
- `services/notion.py` — one-way push to Notion
- `bot/state.py` — state machine for confirmation flow
- Update `bot/handlers.py` — brain dump flow (extract → confirm → save → sync)
- Test: text/voice dump → extracted items → confirm → appears in Postgres + Notion

### Phase 4 — Morning Check-In (~3 files modified)
- `prompts/morning.txt` — morning prompt
- `bot/scheduler.py` — APScheduler with morning trigger
- Update `bot/handlers.py` — morning check-in conversation flow
- Update `bot/state.py` — morning mode
- Test: scheduler fires → bot sends top 3 → user confirms → committed_today set

### Phase 5 — Evening Check-In (~3 files modified)
- `prompts/evening.txt` — evening prompt
- Update `bot/scheduler.py` — add evening trigger
- Update `bot/handlers.py` — evening check-in conversation flow
- Update `bot/state.py` — evening mode
- Test: scheduler fires → bot asks about committed tasks → user reports → status updated

### Phase 6 — Memory & Patterns (~2 files modified)
- Update `services/claude.py` — include 7-day check-in history in context
- Update `bot/scheduler.py` — add weekly summary trigger
- Test: Claude references previous days' patterns

### Deployment
- Create GitHub repo
- `deploy.sh` — copy from remote-claude, adapt
- systemd service file
- Add to webhook.py DEPLOY_SCRIPTS
- SSH to VPS, clone, configure .env, start service

---

## Complexity Estimate

- **Files to create:** ~15 (new project, greenfield)
- **Estimated phases:** 6 (as defined in spec)
- **Rationale:** Each phase is independently testable and builds on the previous. The spec's ordering is correct — foundation → input (voice) → processing (brain dump) → scheduled output (check-ins) → intelligence (patterns).
- **Phase boundaries:** As defined in spec above. Each phase can be committed and deployed independently.
- **Estimated time:** Phase 1-3 could be built in a single focused session. Phases 4-5 in a second session. Phase 6 after a week of personal use.

---

## Appendix: VPS Service Map

```
root@95.217.217.145 (Hetzner VPS)
├── Port 9000: webhook.py (GitHub deploy webhooks for all projects)
├── claude-bot.service: remote-claude Telegram bot (polling, no port)
├── Port 8001: accountability-bot (planned, polling or webhook)
└── Other services: calida, astrology-ai, coaching-ai, contacts-ai (deploy scripts exist)
```
