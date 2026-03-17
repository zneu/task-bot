user_states = {}


def get_state(user_id: str) -> dict:
    if user_id not in user_states:
        user_states[user_id] = {
            "mode": "idle",
            "conversation_history": [],
            "pending_items": None,
            "task_map": {},
        }
    return user_states[user_id]


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
