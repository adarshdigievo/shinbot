import json
import time
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReadNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=256)


class ReadNotifications(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReadNotification] = Field(max_length=100)


class Notifications:
    def __init__(self, core):
        self.core, self.db = core, core.db

    def snapshot(self):
        reads = {row["id"]: row["revision"] for row in self.db.all("SELECT * FROM notification_reads")}
        items = []
        for row in self.db.all(
            "SELECT a.id,a.status,a.created,a.task_id,t.title,b.name AS bot "
            "FROM actions a JOIN tasks t ON t.id=a.task_id LEFT JOIN bots b ON b.id=t.bot_id "
            "WHERE a.status='uncertain' OR (a.status='pending' AND a.expires>?)",
            (time.time(),),
        ):
            items.append(
                {
                    "id": "action:" + row["id"],
                    "revision": row["status"],
                    "title": row["title"],
                    "kind": "approval",
                    "detail": ("Approval needed" if row["status"] == "pending" else "Action needs review")
                    + " · "
                    + (row["bot"] or "Bot"),
                    "created": row["created"],
                    "task_id": row["task_id"],
                    "view": "chat",
                }
            )
        for row in self.db.all(
            "SELECT e.*,t.title AS task_title,t.source,b.name AS bot,s.notify "
            "FROM events e LEFT JOIN tasks t ON t.id=e.task_id LEFT JOIN bots b ON b.id=t.bot_id "
            "LEFT JOIN schedules s ON t.source='job:'||s.id ORDER BY e.id DESC LIMIT 200"
        ):
            if row["kind"] == "completed" and row["notify"] == 0:
                continue
            label = {
                "completed": "Reply ready",
                "failed": "Task failed",
                "learning": "Skill ready for review",
                "warning": "Needs attention",
                "error": "Needs attention",
                "plugin": "Plugin updated",
            }.get(row["kind"], "Workspace update")
            items.append(
                {
                    "id": "event:" + str(row["id"]),
                    "revision": "1",
                    "kind": row["kind"],
                    "title": row["task_title"] or row["title"],
                    "detail": label + (" · " + row["bot"] if row["bot"] else ""),
                    "created": row["created"],
                    "task_id": row["task_id"] if row["task_title"] else None,
                    "view": "chat" if row["task_title"] else "plugins" if row["kind"] == "plugin" else "jobs",
                }
            )
        if self.db.one("SELECT 1 FROM plugins WHERE id='projects' AND status='active'"):
            scopes = self.core.inbox.scopes()
            for row in self.db.all(
                "SELECT i.*,s.seen,s.snoozed FROM github_items i LEFT JOIN github_item_state s ON s.id=i.id "
                "WHERE coalesce(s.snoozed,0)<=?",
                (time.time(),),
            ):
                if not self.core.inbox.allowed(row["repo"], scopes):
                    continue
                item = json.loads(row["payload"])
                if row["seen"] == item["updated_at"]:
                    continue
                try:
                    created = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
                except ValueError:
                    created = row["synced"]
                items.append(
                    {
                        "id": "github:" + item["id"],
                        "revision": item["updated_at"] or str(row["synced"]),
                        "title": item["title"],
                        "kind": "github",
                        "item_id": item["id"],
                        "detail": item["repo"] + " #" + str(item["number"]),
                        "created": created,
                        "task_id": None,
                        "view": "projects",
                    }
                )
        for item in items:
            item["unread"] = reads.get(item["id"]) != item["revision"]
        items.sort(
            key=lambda item: (item["unread"], item["kind"] == "approval", item["created"]), reverse=True
        )
        return {
            "items": items[:100],
            "unread": sum(item["unread"] for item in items),
            "has_more": len(items) > 100,
        }

    def read(self, payload: ReadNotifications):
        current = {item["id"]: item for item in self.snapshot()["items"]}
        with self.db.transaction() as db:
            for requested in payload.items:
                item = current.get(requested.id)
                if not item or item["revision"] != requested.revision:
                    continue
                db.execute(
                    "INSERT INTO notification_reads VALUES (?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
                    (requested.id, requested.revision),
                )
                if item["kind"] == "github":
                    db.execute(
                        "INSERT INTO github_item_state(id,seen) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET seen=excluded.seen",
                        (item["item_id"], requested.revision),
                    )
        return self.snapshot()
