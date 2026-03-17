# Task Map Persistence & Callback Improvements — Plan

- **Branch:** `feature/telegram-task-management`
- **Date:** 2026-03-16
- **Research:** `.context/task-map-persistence/task-map-state-research.md`

## Goal

Eliminate stale task_map bugs by using UUIDs in callback buttons, auto-showing tasks on first populate, fixing callback handler state, and persisting task_map to the database.

## Success Criteria

- [x] Inline keyboard buttons use task UUIDs directly — no task_map lookup needed for callbacks
- [x] Callback handler works after bot restart (buttons from old sessions still function)
- [x] First command in a new session auto-shows an abbreviated task list
- [x] `task_map` is persisted to PostgreSQL and restored on session init
- [ ] Existing `/list`, `/done`, `/doing`, `/edit`, `/delete`, `/move` commands still work correctly

## Scope Boundaries

### In Scope
- **Fix A:** Callback handler calls `_ensure_task_map` before status update
- **Fix B:** Use task UUIDs in callback_data instead of display numbers
- **Fix C:** Auto-show abbreviated task list when `_ensure_task_map` populates
- **Fix D:** Persist `task_map` to PostgreSQL via a `UserState` table

### Out of Scope
- Persisting conversation history to DB (Tier 2 item E — separate effort)
- Stable task identifiers / monotonic counters (Tier 3 item F)
- Name-based task references via Claude (Tier 3 item G)
- Alembic migrations (project uses `create_all` pattern)
- Test suite creation (no existing tests in repo)

## Implementation Steps

### Phase 1: Callback & Auto-Show Fixes (Tier 1: A, B, C)

#### Step 1.1: Use task UUIDs in callback_data (Fix B)

This is the most impactful fix — it makes buttons self-contained and eliminates the need for task_map in callbacks entirely. This supersedes Fix A (callback handler calling `_ensure_task_map`), since callbacks won't need the task_map at all.

- **File:** `bot/commands.py` **Lines:** L299-L326 (inside `cmd_list`)
- **Change:** The list command currently doesn't create inline buttons. We need to find where buttons ARE created. Looking at the codebase, callback_data like `"done:3"` is parsed in `handlers.py` L193-199, but buttons are not created in `cmd_list`. The research mentions buttons exist — let me check if they're generated elsewhere.

Actually, reviewing the code more carefully: `cmd_list` (L273-326) does NOT create inline keyboard buttons. The only inline buttons are in `_handle_dump` (L231-236) for save/discard. The callback handler (handlers.py L193-199) parses `done:N` and `doing:N` patterns, but no code currently generates those buttons.

This means Fix B is about **future-proofing** — if/when task action buttons are added, they should use UUIDs. For now, we should still implement Fix A to handle the case where buttons get added later, and we should change the callback handler to support UUID-based lookups.

**Revised approach:** Modify the callback handler to accept both formats — `done:<uuid>` (direct) and `done:<num>` (legacy, with task_map fallback). This makes the system ready for UUID buttons without breaking anything.

- **File:** `bot/handlers.py` **Lines:** L193-L199
- **Change:** Add `_ensure_task_map` call, then resolve the identifier: if it looks like a UUID (contains hyphens), use directly; otherwise look up in task_map.
- **Before:**
```python
    if data.startswith("done:") or data.startswith("doing:"):
        action, num = data.split(":", 1)
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        status = "done" if action == "done" else "in_progress"
        from bot.commands import _set_status_from_callback
        await _set_status_from_callback(num, status, query.message, state)
```
- **After:**
```python
    if data.startswith("done:") or data.startswith("doing:"):
        action, identifier = data.split(":", 1)
        user_id = str(query.from_user.id)
        state = get_state(user_id)
        status = "done" if action == "done" else "in_progress"

        # Resolve: UUID (direct) or display number (via task_map)
        if "-" in identifier:
            task_id = identifier
        else:
            from bot.commands import _ensure_task_map
            await _ensure_task_map(state)
            task_id = state.get("task_map", {}).get(identifier)

        if task_id:
            from bot.commands import _set_status_by_id
            await _set_status_by_id(task_id, status, query.message)
        else:
            await query.message.reply_text(f"Couldn't find that task. Use /list to refresh.")
```
- **Verification:** Bot restart → old numbered buttons fall back to task_map → new UUID buttons work directly

#### Step 1.2: Extract `_set_status_by_id` from `_set_status_from_callback`

- **File:** `bot/commands.py` **Lines:** L366-L389
- **Change:** Create a new `_set_status_by_id(task_id, status, message)` that takes a UUID directly. Refactor `_set_status_from_callback` to call it (or remove it since the handler now resolves the ID itself).
- **Before:**
```python
async def _set_status_from_callback(num: str, status: str, message, state: dict):
    """Set task status from inline keyboard callback (uses message.reply_text)."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await message.reply_text(f"No task #{num}. Use /list first.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            await message.reply_text("Task not found in database.")
            return

        task.status = status
        push_task(task)
        await session.commit()
        label = "Done" if status == "done" else "In progress"
        await message.reply_text(f"{label}: {task.title}")
```
- **After:**
```python
async def _set_status_by_id(task_id: str, status: str, message):
    """Set task status by UUID (used by callback buttons)."""
    from database.connection import AsyncSessionLocal
    from database.models import Task
    from services.notion import push_task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            await message.reply_text("Task not found in database.")
            return

        task.status = status
        push_task(task)
        await session.commit()
        label = "Done" if status == "done" else "In progress"
        await message.reply_text(f"{label}: {task.title}")


async def _set_status_from_callback(num: str, status: str, message, state: dict):
    """Set task status from inline keyboard callback (legacy number-based)."""
    task_id = state.get("task_map", {}).get(num)
    if not task_id:
        await message.reply_text(f"No task #{num}. Use /list first.")
        return
    await _set_status_by_id(task_id, status, message)
```
- **Verification:** `/done 3` still works via `_set_status` (unchanged). Callbacks route through `_set_status_by_id`.

#### Step 1.3: Auto-show abbreviated task list on first populate (Fix C)

- **File:** `bot/commands.py` **Lines:** L149-L165
- **Change:** When `_ensure_task_map` actually populates (not skips), return a brief summary string. The caller can optionally send it to the user. Modify to also fetch task titles.
- **Before:**
```python
async def _ensure_task_map(state: dict):
    """Auto-populate task_map if empty, so commands work without /list first."""
    if state.get("task_map"):
        return

    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.notin_(["done"]))
            .order_by(Task.project, Task.created_at)
        )
        tasks = result.scalars().all()

    state["task_map"] = {str(i + 1): t.id for i, t in enumerate(tasks)}
```
- **After:**
```python
async def _ensure_task_map(state: dict, update: Update = None):
    """Auto-populate task_map if empty, so commands work without /list first.

    If update is provided and the map was empty, sends an abbreviated task list
    so the user knows the current numbering.
    """
    if state.get("task_map"):
        return

    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.notin_(["done"]))
            .order_by(Task.project, Task.created_at)
        )
        tasks = result.scalars().all()

    if not tasks:
        return

    state["task_map"] = {str(i + 1): t.id for i, t in enumerate(tasks)}

    # Show abbreviated list so user knows the numbering
    if update and update.message:
        lines = [f"  {i+1}. {t.title}" for i, t in enumerate(tasks)]
        brief = "\n".join(lines[:8])
        if len(tasks) > 8:
            brief += f"\n  ... and {len(tasks) - 8} more"
        await update.message.reply_text(f"Your tasks:\n{brief}")
```
- **Verification:** Fresh session → first command (e.g., `/done 3`) → user sees abbreviated task list before the command result

#### Step 1.4: Pass `update` to `_ensure_task_map` calls

- **File:** `bot/commands.py` **Lines:** L39, L95
- **Change:** Pass the `update` parameter so auto-show works.
- **Before (L39):**
```python
    await _ensure_task_map(state)
```
- **After (L39):**
```python
    await _ensure_task_map(state, update)
```
- **Before (L95):**
```python
    await _ensure_task_map(state)
```
- **After (L95):**
```python
    await _ensure_task_map(state, update)
```
- **Verification:** Both fast-path and slow-path commands trigger auto-show on first use.

---

### Phase 2: Persist task_map to Database (Tier 2: D)

#### Step 2.1: Add `UserState` model

- **File:** `database/models.py` **Lines:** After L71 (end of file)
- **Change:** Add a `UserState` table to store per-user JSON state (task_map). Using a single JSON column keeps it simple — no need to normalize the map.
- **Code to add:**
```python
class UserState(Base):
    __tablename__ = "user_states"
    user_id = Column(String, primary_key=True)
    task_map = Column(JSON, default=dict)
    updated_at = Column(TIMESTAMP(timezone=True), default=utcnow, onupdate=utcnow)
```
- **Verification:** Table created on next `init_db()` call (uses `create_all`).

#### Step 2.2: Add persistence helpers to `bot/state.py`

- **File:** `bot/state.py` **Lines:** After L31 (end of file)
- **Change:** Add async functions to save/load task_map from the database. These are called when task_map changes (after `/list`, after `_ensure_task_map` populates) and when state is first initialized.
- **Code to add:**
```python
async def save_task_map(user_id: str, task_map: dict):
    """Persist task_map to database."""
    from database.connection import AsyncSessionLocal
    from database.models import UserState
    from sqlalchemy.dialects.postgresql import insert

    async with AsyncSessionLocal() as session:
        stmt = insert(UserState).values(
            user_id=user_id, task_map=task_map
        ).on_conflict_do_update(
            index_elements=["user_id"],
            set_={"task_map": task_map}
        )
        await session.execute(stmt)
        await session.commit()


async def load_task_map(user_id: str) -> dict:
    """Load task_map from database. Returns empty dict if not found."""
    from database.connection import AsyncSessionLocal
    from database.models import UserState
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserState.task_map).where(UserState.user_id == user_id)
        )
        row = result.scalar_one_or_none()
        return row if row else {}
```
- **Verification:** Can round-trip a task_map: save then load returns same data.

#### Step 2.3: Load persisted task_map on state initialization

- **File:** `bot/commands.py` **Lines:** L149-L165 (`_ensure_task_map`)
- **Change:** Before querying for all tasks, try to load a persisted task_map. If one exists and is non-empty, use it (skip the DB query for tasks). This preserves the user's last-seen numbering.
- **Before** (the version from Step 1.3):
```python
async def _ensure_task_map(state: dict, update: Update = None):
    """..."""
    if state.get("task_map"):
        return
    ...
```
- **After:**
```python
async def _ensure_task_map(state: dict, update: Update = None, user_id: str = None):
    """Auto-populate task_map if empty.

    Priority: in-memory → persisted in DB → fresh query.
    If update is provided and the map was empty, sends an abbreviated task list.
    """
    if state.get("task_map"):
        return

    # Try loading persisted map first
    if user_id:
        from bot.state import load_task_map
        persisted = await load_task_map(user_id)
        if persisted:
            state["task_map"] = persisted
            return

    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status.notin_(["done"]))
            .order_by(Task.project, Task.created_at)
        )
        tasks = result.scalars().all()

    if not tasks:
        return

    state["task_map"] = {str(i + 1): t.id for i, t in enumerate(tasks)}

    # Persist the freshly built map
    if user_id:
        from bot.state import save_task_map
        await save_task_map(user_id, state["task_map"])

    # Show abbreviated list so user knows the numbering
    if update and update.message:
        lines = [f"  {i+1}. {t.title}" for i, t in enumerate(tasks)]
        brief = "\n".join(lines[:8])
        if len(tasks) > 8:
            brief += f"\n  ... and {len(tasks) - 8} more"
        await update.message.reply_text(f"Your tasks:\n{brief}")
```
- **Verification:** Restart bot → first command loads persisted map (no abbreviated list shown since it was restored, not freshly built).

#### Step 2.4: Pass `user_id` to `_ensure_task_map` calls

- **File:** `bot/commands.py` **Lines:** L39, L95
- **Change:** Extract user_id from update and pass to `_ensure_task_map`.
- **Before (L29-39, inside `route_command`):**
```python
async def route_command(text: str, update: Update, state: dict) -> bool:
    ...
    # Auto-populate task_map if empty
    await _ensure_task_map(state, update)
```
- **After:**
```python
async def route_command(text: str, update: Update, state: dict) -> bool:
    ...
    # Auto-populate task_map if empty
    user_id = str(update.effective_user.id)
    await _ensure_task_map(state, update, user_id)
```
- **Before (L94-95, inside `_classify_and_dispatch`):**
```python
    await _ensure_task_map(state)
```
- **After:**
```python
    user_id = str(update.effective_user.id)
    await _ensure_task_map(state, update, user_id)
```
- **Verification:** Both code paths pass user_id for persistence.

#### Step 2.5: Save task_map after `/list` updates it

- **File:** `bot/commands.py` **Lines:** L323 (inside `cmd_list`, after `state["task_map"] = task_map`)
- **Change:** Persist the updated map after `/list` builds it.
- **Before:**
```python
    state["task_map"] = task_map

    header = "All tasks:" if show_all else "Open tasks:"
```
- **After:**
```python
    state["task_map"] = task_map

    # Persist updated map
    user_id = str(update.effective_user.id)
    from bot.state import save_task_map
    await save_task_map(user_id, task_map)

    header = "All tasks:" if show_all else "Open tasks:"
```
- **Verification:** Run `/list` → restart bot → `/done 3` works with correct numbering from persisted map.

#### Step 2.6: Update callback handler for persistence path

- **File:** `bot/handlers.py` **Lines:** L193-199 (the code from Step 1.1)
- **Change:** Pass `user_id` to `_ensure_task_map` in the number-based fallback.
- **Before** (from Step 1.1):
```python
        if "-" in identifier:
            task_id = identifier
        else:
            from bot.commands import _ensure_task_map
            await _ensure_task_map(state)
            task_id = state.get("task_map", {}).get(identifier)
```
- **After:**
```python
        if "-" in identifier:
            task_id = identifier
        else:
            from bot.commands import _ensure_task_map
            await _ensure_task_map(state, user_id=user_id)
            task_id = state.get("task_map", {}).get(identifier)
```
- **Verification:** Old numbered buttons still work after restart (load from DB).

---

## Testing Plan

### Manual Testing (no test framework in repo)

#### Phase 1
- [ ] Fresh session: send `/done 3` — see abbreviated task list + done confirmation
- [ ] Fresh session: send natural language "mark 2 as done" — see abbreviated list + done confirmation
- [ ] Run `/list` — see full task list, numbers match abbreviated list
- [ ] Run `/list Thrown` — filtered list with different numbering
- [ ] After `/list`, `/done 1` works correctly
- [ ] After bot restart, inline buttons from previous session still appear in Telegram (UI) — pressing them should now work via `_ensure_task_map` fallback

#### Phase 2
- [ ] Run `/list` → restart bot → `/done 3` uses persisted map (correct task)
- [ ] Run `/list` → restart bot → no abbreviated list shown (restored from DB, not freshly built)
- [ ] Run `/list Thrown` → restart bot → map reflects the filtered list
- [ ] Fresh user (no DB state) → falls through to task query → builds and persists map
- [ ] Check `user_states` table has rows after first use

### Rollback Plan

- Phase 1: Revert changes to `commands.py` and `handlers.py` — no schema changes
- Phase 2: Revert code changes; `user_states` table can be dropped or left (no harm). `DROP TABLE user_states;` if cleanup wanted.

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Persisted task_map references deleted tasks | Medium | Low | `_set_status` / `_set_status_by_id` already handles "task not found in database" gracefully |
| Stale persisted map after adding/deleting tasks | Medium | Low | `/list` overwrites the persisted map; `_ensure_task_map` only loads if in-memory is empty |
| `create_all` doesn't add columns to existing tables | Low | Medium | `UserState` is a new table, not an alter — `create_all` handles this fine |
| Auto-show message clutters the chat | Low | Low | Limited to 8 tasks, only shown on first populate (not on every command) |

## Open Questions

1. Should the auto-show message be suppressed when the user's first command IS `/list`? (Currently it would show abbreviated list then immediately show full list.) — **Recommendation:** Yes, skip auto-show if the command is `list`. Will implement by checking the command being routed.

---

## Phase Prompts

### Phase 1 Prompt
/implement task-map-persistence — Execute Phase 1 of `.context/task-map-persistence/task-map-persistence-plan.md`.
Scope: Tier 1 fixes — UUID-based callback resolution, _set_status_by_id extraction, auto-show abbreviated task list, pass update to _ensure_task_map. Steps: 1.1–1.4. Files: `bot/commands.py`, `bot/handlers.py`.
Prerequisite: None.
Verification: Fresh session → first command shows abbreviated task list → `/done N` works → callback buttons resolve via task_map fallback.

### Phase 2 Prompt
/implement task-map-persistence — Execute Phase 2 of `.context/task-map-persistence/task-map-persistence-plan.md`.
Scope: Persist task_map to PostgreSQL via UserState table. Steps: 2.1–2.6. Files: `database/models.py`, `bot/state.py`, `bot/commands.py`, `bot/handlers.py`.
Prerequisite: Phase 1 complete and committed.
Verification: `/list` → restart bot → `/done 3` uses correct task from persisted map → no abbreviated list on restore.
