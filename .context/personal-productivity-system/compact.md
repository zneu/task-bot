# Compaction Log

---

## Session: 2026-03-16 — Remove committed_today cycle, fix voice message routing

### Session Info
- **Date**: 2026-03-16
- **Branch**: `feature/telegram-task-management`
- **Task**: Fix voice messages not working after check-ins; remove committed_today commit-report cycle

### Files Modified This Session
- `bot/handlers.py` — Removed `handle_morning_response`, `handle_evening_response`, and morning/evening mode routing from `process_input`. Removed unused `set_pending` import.
- `bot/scheduler.py` — Made all check-ins one-way (no mode setting), query open tasks instead of committed_today tasks, weekday-only (mon-fri).
- `bot/state.py` — Removed `committed_task_ids` from default state, removed unused `set_mode` function.
- `database/models.py` — Removed `committed_today` column from Task model. Added `Note` model (from prior uncommitted work).
- `services/claude.py` — Removed `get_checkin_context`. Added `summarize_note` and note/notes intents in `classify_intent` (from prior uncommitted work).
- `services/notion.py` — Removed `committed_today` from Notion push_task properties.
- `prompts/morning.txt`, `prompts/afternoon.txt`, `prompts/evening.txt` — Rewritten for one-way reminders, no user response expected.

### Root Cause of Bug
Morning check-in set `state["mode"] = "morning"`, which was never cleared unless the user responded with valid task numbers. All subsequent messages (including voice) were routed to `handle_morning_response` which expected task numbers, not natural language.

### Key Decisions
- All check-ins are now one-way — informational only, no response required
- `committed_today` column removed from model (column left in DB per DB safety rules — no destructive migrations)
- Check-ins limited to mon-fri via CronTrigger `day_of_week` param
- Notes feature (`/note`, `/notes`, `/viewnote`) deployed alongside this fix

### Deployment
- No git remote — deployed via rsync to Hetzner VPS (95.217.217.145)
- Files synced: handlers, scheduler, state, models, commands, claude, notion, prompts
- Service restarted, verified active with clean startup logs
- `notes` table created automatically via `init_db()` / `create_all`

### Commits
- `2d6e320` — Add voice journal/notes feature (committed before session)
- `5892fe2` — Add research + the committed_today removal changes

### Next Actions
- None — fix is deployed and live
