# AI-Powered Personal Productivity System — Research

**Date:** 2026-03-14
**Scope:** Full landscape analysis of approaches, stacks, and components for building a custom AI-powered task/time/people tracking system.
**Constraints:** Both phone + laptop usage, $20-50/mo budget, high technical comfort, priorities are speed-to-capture + AI organization + sellable product potential.

---

## Overview

This research covers the full spectrum of approaches for building a personal productivity system that combines: voice/text capture, AI-powered task extraction and organization, people/relationship tracking, prioritization, scheduling, and accountability. Approaches range from no-code (Notion API) to full custom stacks (Next.js + Expo + PostgreSQL) to advanced local-first architectures.

---

## Key Components Matrix

| Component | Role | Options (cheapest → most capable) |
|---|---|---|
| Voice capture (browser) | Record audio on phone/laptop | Web Speech API (free) → MediaRecorder API + STT service |
| Speech-to-text | Convert voice → text | Web Speech API (free) → Groq Whisper ($0.0011/min) → Deepgram ($0.0043/min) → OpenAI Whisper ($0.006/min) |
| AI extraction | Parse tasks, people, dates from text | GPT-4o-mini ($0.15/$0.60 per 1M tokens) → Claude Haiku ($0.80/$4.00) → Claude Sonnet ($3/$15) |
| Embeddings | Semantic search, "what am I missing?" | text-embedding-3-small ($0.02/1M tokens) → Voyage AI ($0.06/1M) → local sentence-transformers (free) |
| Vector store | Store/query embeddings | SQLite-vss (free local) → Supabase pgvector (free tier) → Pinecone (free tier: 100K vectors) |
| Database | Primary data storage | SQLite/Turso (free) → Supabase Postgres (free tier) → Railway Postgres ($5-10/mo) |
| Auth | User authentication | Supabase Auth (free tier) → NextAuth/Auth.js (free) → Clerk ($25/mo after 10K MAU) |
| Frontend | Web UI | Next.js → SvelteKit → Remix |
| Mobile | Phone experience | PWA (free) → Expo/React Native (free + EAS builds) |
| Hosting | Deploy everything | Vercel free tier → Railway $5-10/mo → Fly.io free tier |

---

## Speech-to-Text: Detailed Comparison

| Service | Cost/min | Cost/hr | Real-time | Streaming | Casual Speech | Mobile Web |
|---|---|---|---|---|---|---|
| Web Speech API | Free | Free | Yes | Yes | Okay | Chrome+Safari only |
| Groq distil-whisper | $0.0002 | $0.012 | No (~3s turnaround) | No | Good (English only) | API-based |
| Groq large-v3 | $0.0011 | $0.066 | No (~3s turnaround) | No | Excellent | API-based |
| Deepgram Nova-2 (batch) | $0.0043 | $0.26 | N/A | N/A | Excellent | API-based |
| Deepgram Nova-2 (stream) | $0.0059 | $0.35 | Yes | WebSocket | Excellent | WebSocket API |
| OpenAI Whisper API | $0.006 | $0.36 | No | No | Good | API-based |
| AssemblyAI | $0.0065 | $0.39 | Yes | WebSocket | Good | WebSocket API |
| Google Cloud STT | $0.016 | $0.96 | Yes | gRPC | Good | gRPC/REST |

**Winner for this use case:** Groq Whisper large-v3. At $0.066/hr it's 5.5x cheaper than OpenAI, same model quality (identical weights), and ~3 second turnaround makes it effectively real-time. OpenAI-compatible API means drop-in replacement. Free tier available.

**Fallback:** Deepgram Nova-2 if real-time streaming is needed (WebSocket support). $0.26/hr batch, $0.35/hr streaming. $200 free credit on signup.

### Browser Voice Capture (Cross-Platform)

**MediaRecorder API:**
- Android Chrome: Reliable. Records `audio/webm;codecs=opus`.
- iOS Safari: Supported from iOS 14.3+. Records `audio/mp4` (AAC) ONLY. No WebM/Opus support.
- `timeslice` parameter broken on iOS < 17. Use manual `.stop()` / `.start()` for chunking on older iOS.
- Both Groq and OpenAI accept both webm and mp4 natively — no transcoding needed.

**Implementation pattern:**
```
1. navigator.mediaDevices.getUserMedia({ audio: true })
2. Detect supported MIME: webm;codecs=opus → mp4 → wav
3. Collect chunks via ondataavailable
4. On stop: create Blob, POST to backend → forward to Groq/Deepgram
```

**iOS Safari quirks:** Permission requires user gesture. Recording interrupted by screen lock, phone calls, background. No reliable background recording in PWA.

---

## AI Processing Pipeline

### Task Extraction Architecture

```
Voice Note → STT (Groq) → Raw Text → LLM (structured output) → {
  tasks: [{ title, assignee, due_date, urgency, people, project, type }],
  people: [{ name, context, relationship }],
  notes: [{ content, tags }],
  follow_ups: [{ person, action, deadline, type }]
}
```

### LLM Pricing for Extraction

| Model | Input/1M tokens | Output/1M tokens | Context | Best for |
|---|---|---|---|---|
| GPT-4o-mini | $0.15 | $0.60 | 128K | Bulk extraction (cheapest) |
| Claude 3.5 Haiku | $0.80 | $4.00 | 200K | Nuanced extraction |
| Claude 4 Sonnet | $3.00 | $15.00 | 200K | Complex reasoning, prioritization |
| GPT-4o | $2.50 | $10.00 | 128K | Alternative to Sonnet |

**Cost-effective approach:** GPT-4o-mini for initial task/people extraction (~5-7x cheaper than Haiku). Claude Sonnet for weekly reviews, prioritization, and "what am I missing?" analysis. Estimated cost for 50 voice notes/day: $0.50-2/mo for extraction, $1-5/mo for AI analysis.

**Claude Batch API:** 50% discount on token costs. Process yesterday's notes overnight for categorization/linking. Results within 24 hours.

### Structured Output Pattern

Both OpenAI and Claude support structured extraction:
- **OpenAI:** `response_format: { type: "json_schema" }` — model is constrained to produce valid JSON matching schema
- **Claude:** Tool use with JSON Schema — model returns structured `tool_use` content blocks

Define a tool/schema like:
```json
{
  "name": "extract_from_note",
  "parameters": {
    "tasks": [{ "title": "string", "assignee": "string", "due_date": "date?", "urgency": "high|medium|low", "people": ["string"], "project": "string?", "type": "action|follow_up|waiting_on|idea" }],
    "people_mentioned": [{ "name": "string", "context": "string", "relationship": "string?" }],
    "follow_ups": [{ "person": "string", "action": "string", "deadline": "date?", "type": "deadline|follow_up|waiting_for|recurring" }]
  }
}
```

### Embeddings for "What Am I Forgetting?"

| Service | Dimensions | Cost/1M tokens |
|---|---|---|
| text-embedding-3-small (OpenAI) | 1536 | $0.02 |
| voyage-3-lite (Voyage AI) | 512 | $0.02 |
| voyage-3 (Voyage AI) | 1024 | $0.06 |
| text-embedding-3-large (OpenAI) | 3072 | $0.13 |
| all-MiniLM-L6-v2 (local) | 384 | Free |

**Implementation:**
1. Embed every task/note on creation
2. "What's related to X?" → embed query, nearest-neighbor search
3. "What am I forgetting?" → find open tasks with no activity in N days, rank by original urgency
4. "Context switch to project Y" → retrieve all related items via similarity

**Vector storage options:** Supabase pgvector (free tier, 500MB) or SQLite-vss (embedded, free) are sufficient for a personal system with thousands of items.

### AI Scheduling & Prioritization

**Eisenhower Matrix automation:** Feed task list to GPT-4o-mini with deadline proximity + impact assessment. Returns quadrant classification per task. Cheap enough to run daily.

**Time-blocking:** Feed calendar (free/busy) + prioritized tasks + user preferences → model returns time-blocked schedule. Requires calendar integration (Google Calendar API).

**Daily planning prompt pattern:**
```
Open tasks: {tasks_json}. Today is {date}, {day_of_week}.
Available work hours: {hours}.
Pick top 3 priorities. Explain why. Break first task into 15-min first step.
```

**Weekly review prompt pattern:**
```
Completed this week: {done_tasks}. Still open: {open_tasks}.
Slipped from last week: {carried_over}.
Identify: wins, slips, next week priorities, tasks to drop/delegate.
```

---

## Stack Options: Simplest to Most Complex

### Option 1: Notion API Backend (Simplest)

**How it works:** Use Notion as the database. Build a custom web frontend for voice capture + AI processing. Notion stores everything.

| Aspect | Detail |
|---|---|
| Database | Notion (free plan, unlimited pages) |
| API | Notion API: CRUD pages/databases/blocks, search, query with filters |
| Rate limits | 3 req/sec per integration |
| Frontend | Next.js on Vercel (free) |
| Voice | MediaRecorder → Groq Whisper → AI extract → Notion API POST |
| Auth | Notion OAuth or single-user (no auth needed) |
| Cost | $0-5/mo (Groq API only) |
| Offline | No |
| Mobile | PWA |

**Pros:** Zero database maintenance. Notion UI available as fallback. Free.
**Cons:** 3 req/sec rate limit. No webhooks (must poll). No real-time sync. Locked into Notion's data model. Not sellable — depends on Notion.

### Option 2: Next.js + Supabase (Sweet Spot for MVP)

**How it works:** Supabase provides Postgres, auth, realtime, and file storage. Next.js on Vercel for the frontend. AI processing via API routes.

| Aspect | Detail |
|---|---|
| Database | Supabase PostgreSQL (free: 500MB, 2 projects) |
| Auth | Supabase Auth (free: 50K MAU) |
| Realtime | Supabase Realtime (free: 200 concurrent connections) |
| Frontend | Next.js on Vercel (free tier) |
| Voice | MediaRecorder → Groq → AI extract → Supabase insert |
| Embeddings | pgvector extension (included in Supabase) |
| Cost | $0 free tier, $25/mo Supabase Pro if needed |
| Offline | Limited (service worker caching) |
| Mobile | PWA with push notifications (iOS 16.4+) |

**Pros:** Full-featured free tier. Auth + DB + realtime in one service. pgvector for embeddings built in. Row-level security. Good DX.
**Cons:** Free tier pauses after 1 week inactivity. 500MB storage limit on free. Not truly offline-capable. Supabase dependency.

**Sellable:** Yes. Supabase scales to production. Multi-tenant via RLS.

### Option 3: SvelteKit + PocketBase (Lightweight Self-Hosted)

**How it works:** PocketBase is a single Go binary with SQLite, auth, REST API, and realtime. Deploy on Railway or Fly.io.

| Aspect | Detail |
|---|---|
| Database | PocketBase (SQLite, single file) |
| Auth | PocketBase built-in (email/password + OAuth2) |
| Realtime | PocketBase SSE subscriptions |
| Frontend | SvelteKit (lighter than Next.js, excellent DX) |
| Voice | Same pipeline: MediaRecorder → Groq → AI → PocketBase |
| Hosting | Railway ($5-10/mo) or Fly.io (free tier) |
| Cost | $5-10/mo hosting + $1-5/mo AI APIs |
| Mobile | PWA |

**Pros:** Single binary deployment. Full control. Very lightweight. Fast.
**Cons:** SQLite = single writer (fine for personal, not for multi-user SaaS). No pgvector (would need separate embedding solution). Smaller community.

### Option 4: T3 Stack on Railway (Full TypeScript)

**How it works:** Next.js + tRPC + Prisma + PostgreSQL. End-to-end TypeScript type safety. Deploy on Railway.

| Aspect | Detail |
|---|---|
| Database | PostgreSQL on Railway |
| Auth | Auth.js (NextAuth) |
| API | tRPC (type-safe, no REST routes to define) |
| ORM | Prisma (type-safe queries, migrations) |
| Frontend | Next.js + Tailwind |
| Voice | Same pipeline |
| Hosting | Railway ($5-10/mo for app + DB) |
| Mobile | PWA, or separate React Native client calling same tRPC API |

**Pros:** Full type safety client-to-database. Great DX. Large community. Scales to SaaS.
**Cons:** More boilerplate than Supabase approach. Cold starts if on serverless. Need to manage DB yourself.

**Sellable:** Yes. T3 stack is production-proven for SaaS.

### Option 5: Next.js + Expo (True Native Mobile)

**How it works:** Shared backend (tRPC or REST), Next.js for web, Expo/React Native for native iOS + Android apps.

| Aspect | Detail |
|---|---|
| Database | Supabase or Railway Postgres |
| Web frontend | Next.js |
| Mobile | Expo (React Native) — native apps |
| Shared | tRPC API, shared TypeScript types |
| Voice (mobile) | Expo AV for recording + native speech APIs |
| Voice (web) | MediaRecorder + Groq |
| Push notifications | expo-notifications (native, reliable) |
| Offline | SQLite on device (expo-sqlite) + sync |
| Cost | $10-20/mo hosting + $1-5/mo AI + EAS builds (30 free/mo) |

**Pros:** True native experience. Background tasks. Siri Shortcuts integration. Reliable push. Offline with local SQLite.
**Cons:** Maintaining two frontends (web + mobile). More complex deployment. App Store review process.

**Sellable:** Most sellable option. Native apps command higher perceived value.

### Option 6: Local-First with Sync (Advanced)

**How it works:** Data lives on device first (SQLite). Syncs to server via CRDT or sync engine. Zero-latency reads, full offline.

| Aspect | Detail |
|---|---|
| Client DB | SQLite (via wa-sqlite in browser, expo-sqlite on mobile) |
| Sync | PowerSync (Postgres ↔ SQLite) or Triplit |
| Server DB | PostgreSQL (Supabase or self-hosted) |
| Frontend | Next.js or SvelteKit (web) + Expo (mobile) |
| Cost | PowerSync free tier + Supabase free tier |

**Pros:** Instant UI. Full offline. Sync "just works." Best UX.
**Cons:** Highest complexity. Conflict resolution edge cases. Schema migrations across distributed clients. Debugging sync issues is hard.

### Option 7: Elixir/Phoenix LiveView (Real-Time Native)

| Aspect | Detail |
|---|---|
| Backend | Elixir/Phoenix |
| Frontend | LiveView (server-rendered, real-time via WebSocket) |
| Database | PostgreSQL |
| Hosting | Fly.io (first-class Elixir support) |
| Cost | $5-10/mo |

**Pros:** Real-time built into the platform. 10K+ concurrent connections per server. Fault-tolerant. Elegant.
**Cons:** Niche language (harder to hire/sell). No native mobile story. Learning curve.

---

## Data Model: Recommended Schema

Based on patterns from Monica CRM, Notion, Vikunja, and the "everything is a block" approach:

### Core Entities

```
User
  id, email, name, preferences (JSON)

Item (unified primitive — replaces separate task/note/contact tables)
  id, user_id, type (task | note | person | follow_up | idea | meeting)
  title, content (rich text)
  status (inbox | active | waiting | done | archived)
  priority (1-4, Eisenhower quadrant)
  due_date, reminder_date
  project_id (nullable)
  parent_id (nullable, for subtasks/threading)
  metadata (JSONB — flexible fields per type)
  embedding (vector, for semantic search)
  source (voice | text | ai_generated | import)
  created_at, updated_at

Project
  id, user_id, name, color, status, description

Tag
  id, user_id, name, color

ItemTag (junction)
  item_id, tag_id

PersonLink (connects items to people, and people to each other)
  id, from_item_id, to_item_id, relationship (mentioned_in | assigned_to | related_to | knows)

Capture (raw voice/text inputs before AI processing)
  id, user_id, raw_text, audio_url (nullable)
  processed (boolean)
  items_created (array of item IDs)
  created_at
```

**Why unified Item table:** One query to search everything. One embedding space. AI doesn't need to know what "type" something is to find connections. Filters by type for specific views (task board, people list, notes).

---

## Existing Tools: Architectural Inspiration

### Capture Patterns

| Tool | Capture Philosophy |
|---|---|
| Mem.ai | Flat stream, AI organizes later. No folders. |
| Audiopen.ai | Voice → AI → structured text. Messy input, clean output. |
| Logseq | Daily journal as inbox. Everything starts on today's page. |
| Granola.ai | User's rough notes + audio → AI combines into structured output. |
| Reclaim.ai | Tasks are inbox, calendar is output. |

**Best fit for this project:** Mem.ai + Audiopen hybrid. Voice/text goes into a flat capture stream. AI processes it into structured items (tasks, people, follow-ups). User reviews and adjusts. Organization is emergent, not manual.

### CRM Inspiration

| Tool | Stack | Key Idea |
|---|---|---|
| Monica CRM | PHP/Laravel + MySQL | Personal relationship management. Contacts are central. Activities, reminders, conversations attached to people. |
| Twenty CRM | NestJS + React + PostgreSQL + GraphQL | Custom objects/fields. Metadata engine for extensibility. Timeline per record. |
| Folk CRM | Commercial | Auto-enrichment from email/LinkedIn. Minimal manual entry. |

**Key insight:** Monica's data model (contacts with activities, reminders, relationships between contacts) maps directly to the "people tracking" requirement. Twenty's custom object engine enables extensibility without code changes.

### Open-Source Starting Points

| Tool | Why Relevant | Could Fork? |
|---|---|---|
| Vikunja | Go + Vue, full REST API, CalDAV, lightweight | Yes — add AI layer on top |
| AppFlowy | Rust + Flutter, CRDT sync, Notion-like | Complex — heavy stack |
| Huly | Svelte + MongoDB, plugin architecture | Interesting architecture, complex codebase |
| PocketBase | Go, single binary, auth + API + realtime | Best as a backend component, not a fork |

---

## Cost Projections ($20-50/mo Budget)

### Minimal Viable System ($5-15/mo)

| Component | Service | Cost |
|---|---|---|
| Hosting (app) | Vercel free tier | $0 |
| Database | Supabase free tier (500MB) | $0 |
| STT | Groq Whisper (50 notes/day × 2 min avg) | ~$3/mo |
| AI extraction | GPT-4o-mini | ~$1-2/mo |
| AI analysis | Claude Sonnet (weekly reviews) | ~$2-5/mo |
| Embeddings | text-embedding-3-small | ~$0.10/mo |
| **Total** | | **~$6-10/mo** |

### Production System ($20-35/mo)

| Component | Service | Cost |
|---|---|---|
| Hosting | Railway (app + API) | $5-10 |
| Database | Supabase Pro or Railway Postgres | $7-25 |
| STT | Groq Whisper | ~$3 |
| AI | GPT-4o-mini + Claude Sonnet | ~$5-10 |
| Embeddings | text-embedding-3-small | ~$0.10 |
| Push notifications | Expo (free tier) | $0 |
| **Total** | | **~$20-35/mo** |

---

## Recommended Approach Stack (Not a Plan — Just Synthesis)

Based on the constraints (both devices, $20-50/mo, sellable, speed to capture + AI organization):

**Option 2 (Next.js + Supabase) is the highest-ROI starting point** because:
- Free tier gets an MVP running at $0 hosting cost
- Auth, database, realtime, pgvector all included
- PWA covers both phone and laptop
- Scales to multi-tenant SaaS via RLS
- Largest community/ecosystem for help
- Can add Expo native app later without changing the backend

**If native mobile becomes critical, evolve to Option 5** (add Expo alongside Next.js, sharing the Supabase backend and tRPC types).

**The cortext vision** (contacts/relationships) fits naturally as a "type" within the unified Item data model. People are items with `type: 'person'`. PersonLink connects them to tasks, notes, and each other.

---

## Existing Infrastructure: What Zachary Already Has

### remote-claude (Telegram Bot → Claude Code)

Zachary already has a working Telegram bot on a Hetzner VPS that pipes messages to Claude Code via `claude -p` subprocess. This is a significant piece of existing infrastructure.

**What it does:**
- Telegram bot receives messages (text, photos, documents)
- Pipes them to `claude -p` with `--output-format stream-json`
- Supports multiple named sessions with independent CWD and conversation history
- Shows tool progress in real-time (edits a single message with tool names)
- Handles images and files (saves to CWD, passes to Claude, cleans up)
- Auto-deploy via GitHub webhook → `deploy.sh` on VPS
- Single authorized user (user ID check)

**Tech:** Python, `python-telegram-bot` v21, polling mode, `telegramify-markdown` for rendering, sessions stored in `sessions.json`.

**VPS also hosts deploy webhooks for:** calida, astrology-ai, coaching-ai, contacts-ai (5 projects total).

**Key file:** `/Users/zacharyneumann/code/remote-claude/bot.py` (589 lines) — the full bot implementation.

### Accountability Bot Spec (Provided by Zachary)

Zachary has a detailed spec for a **Telegram Accountability Bot** that acts as an AI accountability partner. This is essentially a more focused, purpose-built version of the productivity system concept.

**Stack defined in spec:**
- Python 3.11+ / FastAPI
- python-telegram-bot v20+ (async)
- Anthropic Claude API (claude-sonnet-4-6) — direct API calls, not `claude -p`
- Notion API as task database
- APScheduler for morning/evening check-ins
- Hetzner VPS (existing) with systemd
- In-memory state (dict) for MVP, Redis/Postgres later

**5 phases defined:**
1. **Foundation:** Telegram webhook, auth, Claude API + Notion API connected
2. **Morning Check-In:** APScheduler fires → pull incomplete tasks from Notion → Claude picks top 3 → sends to user → user confirms → logs to Notion
3. **Evening Check-In:** Pull committed tasks → ask what happened → Claude parses completion → logs status → calls out avoidance patterns (3+ days)
4. **Brain Dump:** Any unscheduled message → Claude extracts tasks, people, ideas, commitments → user confirms → creates in Notion
5. **Memory & Patterns:** Store 7 days of conversation history → Claude surfaces patterns

**Notion database schema defined:**
- Name (title), Status (Not started / In progress / Done / Avoided), Priority (High / Medium / Low), Project (select), Due Date, Committed Today (checkbox), Notes (text)

**System prompts designed for:**
- Morning check-in: "accountability partner for someone with ADHD building multiple products and a music career"
- Evening check-in: honest but supportive, detects avoidance patterns
- Brain dump extraction: returns structured JSON (tasks, people, ideas, commitments)

**Sellable version notes:** Add auth (Clerk/Supabase), Notion OAuth, move state to Postgres, add Stripe, onboarding flow. Target: "$15-25/month SaaS."

### How This Changes the Analysis

The accountability bot spec is a **concrete, buildable MVP** that sits between the research options. Key observations:

1. **Notion as database is already chosen** for the accountability bot — this aligns with Option 1 (Notion API Backend) from the stack analysis. Quick to build, but limits sellability.

2. **Telegram as the interface** eliminates the need for a web frontend initially. Voice notes can be sent directly via Telegram voice messages. This dramatically simplifies the MVP.

3. **The remote-claude bot already solves general-purpose Telegram → Claude interaction.** The accountability bot is a specialized version with scheduled check-ins, Notion integration, and structured prompts.

4. **Two possible paths forward:**

   **Path A: Build accountability bot as specified (Telegram + Notion + Claude API)**
   - Fastest to MVP (days, not weeks)
   - Telegram handles both phone and laptop
   - Notion handles task storage with existing UI as fallback
   - Voice notes via Telegram voice messages → can add Groq STT later
   - Limited by Notion API (3 req/sec, no webhooks, no embeddings)
   - Sellable as a Telegram bot SaaS ($15-25/mo)

   **Path B: Build the full web app (Next.js + Supabase) with Telegram as one channel**
   - Slower to MVP (weeks)
   - More powerful: embeddings, custom data model, real-time
   - Telegram bot becomes one input channel alongside web/PWA
   - More sellable long-term (web app > Telegram bot for most users)
   - Can start with Path A and migrate to Path B later

   **Path C: Extend remote-claude with accountability features**
   - Add scheduled messages and Notion integration to the existing bot
   - Leverage existing session management and deployment infrastructure
   - Lowest effort — build on what works
   - But: conflates general-purpose Claude bot with accountability-specific logic

5. **The "contacts-ai" project on the VPS** may be the prior "cortext" concept mentioned earlier. This could inform the people/CRM dimension.

---

## Complexity Estimate

- **Files to modify:** 0 (greenfield project)
- **Estimated phases:** 3+
- **Rationale:** This is a full application build with multiple subsystems (voice capture, AI pipeline, data model, frontend, auth, mobile). Each subsystem is independently complex enough to warrant its own phase.
- **Phase boundaries (if multi):**
  - Phase 1: Data model + auth + basic CRUD (Supabase + Next.js)
  - Phase 2: Voice capture + STT + AI extraction pipeline
  - Phase 3: AI organization (embeddings, prioritization, scheduling, "what am I missing?")
  - Phase 4: People/CRM features (relationships, follow-ups, contact tracking)
  - Phase 5: Mobile (PWA polish or Expo native app)
  - Phase 6: Multi-tenant / sellable (billing, onboarding, landing page)

---

## Appendix A: PWA vs Native for This Use Case

| Capability | PWA | Native (Expo) |
|---|---|---|
| Push notifications | Yes (iOS 16.4+, must be installed) | Yes (reliable) |
| Offline data | IndexedDB + service worker | SQLite + filesystem |
| Voice recording | MediaRecorder API (works, iOS quirks) | expo-av (reliable) |
| Background sync | Limited (Background Sync API, poor iOS) | Yes (background fetch) |
| Siri/Shortcuts | No | Yes |
| Widgets | No | Yes |
| App Store presence | No (Play Store via TWA only) | Yes |
| Dev complexity | Low | Medium-High |
| Time to MVP | Fast | Slower |

**Recommendation:** Start PWA, add native later if needed.

## Appendix B: All Pricing (as of mid-2025, verify before building)

### Hosting
| Service | Free Tier | Paid |
|---|---|---|
| Vercel | Unlimited sites, 100GB BW, 100 GB-hrs functions | $20/user/mo |
| Railway | $5 credit trial | $5/mo + usage (~$5-10 total) |
| Fly.io | 3 VMs, 160GB BW, 3GB volumes | Pay-as-you-go from ~$2/mo |
| Render | Free instance (sleeps after 15 min) | $7/mo always-on |
| Coolify | Free (self-hosted on ~$5/mo VPS) | N/A |

### Databases
| Service | Free Tier | Paid |
|---|---|---|
| Supabase | 500MB, 2 projects | $25/mo pro |
| Turso | 9GB, 500 DBs | $29/mo |
| Neon | 0.5GB, scales to zero | $19/mo |
| Railway Postgres | Usage-based | ~$0.25/GB/mo |
| MongoDB Atlas | 512MB | $57/mo dedicated |
| PocketBase | Free (self-hosted) | Hosting cost only |

### AI APIs
| Service | Unit | Cost |
|---|---|---|
| Groq Whisper large-v3 | per minute | $0.0011 |
| Groq distil-whisper | per minute | $0.0002 |
| OpenAI Whisper API | per minute | $0.006 |
| Deepgram Nova-2 batch | per minute | $0.0043 |
| GPT-4o-mini | per 1M tokens (in/out) | $0.15 / $0.60 |
| Claude 3.5 Haiku | per 1M tokens (in/out) | $0.80 / $4.00 |
| Claude 4 Sonnet | per 1M tokens (in/out) | $3.00 / $15.00 |
| text-embedding-3-small | per 1M tokens | $0.02 |
| Voyage-3-lite | per 1M tokens | $0.02 |
