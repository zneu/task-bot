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
