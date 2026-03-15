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


def archive_task(notion_id: str) -> bool:
    """Archive a task in Notion. Returns True on success."""
    try:
        notion.pages.update(page_id=notion_id, archived=True)
        return True
    except Exception:
        logger.exception(f"Failed to archive Notion page '{notion_id}'")
        return False
