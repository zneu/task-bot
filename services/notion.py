import os
import logging
from notion_client import Client

logger = logging.getLogger(__name__)

notion = Client(auth=os.getenv("NOTION_API_KEY"))
DB_ID = os.getenv("NOTION_TASKS_DATABASE_ID")
NOTES_DB_ID = os.getenv("NOTION_NOTES_DATABASE_ID")


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


def push_note(note) -> str | None:
    """Push a note to Notion. Returns Notion page ID."""
    try:
        source_emoji = {"voice": "Voice \U0001f7e2", "text": "Text \U0001f535"}.get(note.source, "Text \U0001f535")
        properties = {
            "Title": {"title": [{"text": {"content": note.title}}]},
            "Transcript": {"rich_text": [{"text": {"content": note.raw_transcript[:2000]}}]},
            "Summary": {"rich_text": [{"text": {"content": note.summary[:2000]}}]},
            "Source": {"select": {"name": source_emoji}},
        }
        if note.tags:
            properties["Tags"] = {"multi_select": [{"name": t} for t in note.tags]}

        result = notion.pages.create(
            parent={"database_id": NOTES_DB_ID}, properties=properties
        )
        return result["id"]
    except Exception:
        logger.exception(f"Failed to sync note '{note.title}' to Notion")
        return None


def archive_task(notion_id: str) -> bool:
    """Archive a task in Notion. Returns True on success."""
    try:
        notion.pages.update(page_id=notion_id, archived=True)
        return True
    except Exception:
        logger.exception(f"Failed to archive Notion page '{notion_id}'")
        return False
