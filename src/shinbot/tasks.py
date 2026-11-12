import hashlib
import json
import time

from .chat_options import ChatOptions
from .db import packed, uid

TERMINAL = {"completed", "failed", "cancelled"}
LEGACY_BUDGET_ERROR = "This task reached its token budget. Check the card’s budget and Settings → Workspace. Raise or clear the cap before resuming."
UNKNOWN_BUDGET_ERROR = (
    "A configured token budget stopped this task. Review your token limits before continuing."
)


class Tasks:
    def __init__(self, db, plugins, bots=None):
        self.db, self.plugins = db, plugins
        self.bots = bots

    def search(self, query="", bot_id=None, offset=0, limit=30):
        query = query.strip()
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.db.all(
            "SELECT t.id,t.title,t.status,t.bot_id,t.updated,b.name AS bot_name,"
            "(SELECT m.id FROM messages m WHERE m.task_id=t.id AND "
            "(json_extract(m.payload,'$.content') LIKE ? ESCAPE '\\' OR m.answer LIKE ? ESCAPE '\\') "
            "ORDER BY m.created DESC,m.id DESC LIMIT 1) AS message_id "
            "FROM tasks t LEFT JOIN bots b ON b.id=t.bot_id "
            "WHERE (? IS NULL OR t.bot_id=?) AND "
            "(t.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m WHERE m.task_id=t.id AND "
            "(json_extract(m.payload,'$.content') LIKE ? ESCAPE '\\' OR m.answer LIKE ? ESCAPE '\\'))) "
            "ORDER BY (t.title LIKE ? ESCAPE '\\') DESC,t.updated DESC,t.id DESC LIMIT ? OFFSET ?",
            (pattern, pattern, bot_id, bot_id, pattern, pattern, pattern, pattern, limit + 1, offset),
        )
        for row in rows[:limit]:
            message = self.db.one("SELECT payload,answer FROM messages WHERE id=?", (row["message_id"],))
            text = ""
            if message:
                prompt = json.loads(message["payload"])["content"]
                row["match"] = "user" if query.casefold() in prompt.casefold() else "answer"
                text = prompt if row["match"] == "user" else message["answer"] or prompt
            start = max(0, text.casefold().find(query.casefold()) - 65) if query else 0
            row["snippet"] = (
                ("…" if start else "") + text[start : start + 240] + ("…" if len(text) > start + 240 else "")
            )
        return {"items": rows[:limit], "has_more": len(rows) > limit}

    def create(
        self,
        prompt,
        title=None,
        source="web",
        grants=None,
        connection=None,
        provider=None,
        model=None,
        bot_id="default",
        options=None,
    ):
        if not prompt.strip() or len(prompt) > 20000:
            raise ValueError("Enter a message of 1–20,000 characters.")
        preferences = self.db.setting("preferences")
        bot = self.bots.get(bot_id) if self.bots else {"profile": {}, "browser_id": "shared"}
        profile = bot["profile"]
        options = ChatOptions.model_validate(options or {}).model_dump(exclude_none=True)
        provider = options.get("provider") or provider
        model = options.get("model") or model
        provider = provider or profile.get("provider") or preferences["provider"]
        model = model or profile.get("model") or preferences["model"]
        if not provider or not model:
            raise ValueError("Choose a model in Settings before starting a conversation.")
        key, now = uid(), time.time()
        grants = (
            grants
            if grants is not None
            else {
                "computer": preferences["computer_auto"],
                "browser": preferences["browser_auto"],
                "extensions": preferences["extensions_auto"],
                "memory": preferences.get("learn_preferences", False),
            }
        )
        allowed = {"computer", "browser", "extensions", "memory"}
        if not set(grants) <= allowed or any(not isinstance(v, bool) for v in grants.values()):
            raise ValueError("Unknown task permission.")
        values = (
            key,
            (title or prompt)[:160],
            "queued",
            provider,
            model,
            now,
            now,
            packed(grants),
            packed(["shinbot-core-v1", *self.plugins.versions(bot_id)]),
            source,
            preferences["task_tokens"],
        )

        def insert(db):
            db.execute(
                "INSERT INTO tasks(id,title,status,provider,model,created,updated,grants,extensions,source,max_tokens) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            db.execute(
                "UPDATE tasks SET bot_id=?,bot_profile=?,browser_id=? WHERE id=?",
                (bot_id, packed(profile), bot["browser_id"], key),
            )
            db.execute(
                "UPDATE tasks SET approval_mode=?,thinking_level=? WHERE id=?",
                (
                    options.get("approval_mode", preferences.get("approval_mode", "auto")),
                    options.get("thinking_level", preferences.get("thinking_level", "off")),
                    key,
                ),
            )
            self._message(db, key, prompt, uid())

        if connection is not None:
            insert(connection)
        else:
            with self.db.transaction() as db:
                insert(db)
        return {"id": key, "title": values[1], "status": "queued"}

    def create_request(self, prompt, request_id, bot_id="default", options=None):
        request = {"content": prompt, "bot_id": bot_id}
        if options:
            request["options"] = options
        digest = hashlib.sha256(packed(request).encode()).hexdigest()
        with self.db.transaction() as db:
            prior = db.execute("SELECT * FROM task_requests WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["hash"] != digest:
                    raise ValueError("Request ID was already used with different content.")
                return dict(
                    db.execute("SELECT id,title,status FROM tasks WHERE id=?", (prior["task_id"],)).fetchone()
                )
            task = self.create(prompt, connection=db, bot_id=bot_id, options=options)
            db.execute("INSERT INTO task_requests VALUES (?,?,?)", (request_id, digest, task["id"]))
            return task

    def _message(self, db, task_id, prompt, request_id, mode="queue"):
        payload = packed({"content": prompt, **({"mode": mode} if mode != "queue" else {})})
        digest = hashlib.sha256(payload.encode()).hexdigest()
        prior = db.execute(
            "SELECT * FROM messages WHERE task_id=? AND request_id=?", (task_id, request_id)
        ).fetchone()
        if prior:
            if prior["hash"] != digest:
                raise ValueError("This request ID was already used for another message.")
            return dict(prior)
        key = uid()
        db.execute(
            "INSERT INTO messages(id,task_id,request_id,payload,hash,status,created,mode) VALUES (?,?,?,?,?,'queued',?,?)",
            (key, task_id, request_id, payload, digest, time.time(), mode),
        )
        return {"id": key, "status": "queued", "request_id": request_id}

    def message(self, task_id, prompt, request_id, mode="queue"):
        if not prompt.strip() or len(prompt) > 20000 or not request_id or len(request_id) > 128:
            raise ValueError("Invalid message or request ID.")
        if mode not in {"queue", "steer"}:
            raise ValueError("Choose Queue or Steer for this message.")
        with self.db.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError("Task not found.")
            if task["status"] in {"paused", "uncertain", "cancelled"}:
                raise ValueError("Resolve or resume this task before sending another message.")
            if task["workspace_id"]:
                workspace = db.execute(
                    "SELECT state FROM project_workspaces WHERE id=?", (task["workspace_id"],)
                ).fetchone()
                if not workspace or workspace["state"] != "ready":
                    raise ValueError(
                        "This worktree is archived or unavailable. Start another task from Projects."
                    )
            result = self._message(db, task_id, prompt, request_id, mode)
            if result["status"] == "queued":
                db.execute(
                    "UPDATE tasks SET status='queued',updated=? WHERE id=? AND status IN ('completed','failed')",
                    (time.time(), task_id),
                )
            return result

    def steer(self, task_id, message_id):
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT m.*,t.status AS task_status FROM messages m JOIN tasks t ON t.id=m.task_id WHERE m.id=? AND m.task_id=?",
                (message_id, task_id),
            ).fetchone()
            if not row:
                raise ValueError("Message not found.")
            if row["task_status"] in {"paused", "uncertain", "cancelled"}:
                raise ValueError("Resolve or resume this task before steering it.")
            if row["mode"] != "steer":
                if row["status"] != "queued":
                    raise ValueError(
                        "This message has already started. Send another message to steer the task."
                    )
                db.execute("UPDATE messages SET mode='steer' WHERE id=?", (message_id,))
        return {"id": message_id, "mode": "steer"}

    def detail(self, task_id):
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("Task not found.")
        task["grants"] = json.loads(task["grants"])
        task["extensions"] = json.loads(task["extensions"])
        task["messages"] = self.db.all(
            "SELECT * FROM messages WHERE task_id=? ORDER BY created,rowid", (task_id,)
        )
        # Older installations recorded reply completion in events, before messages had finished.
        completions = self.db.all(
            "SELECT * FROM events WHERE task_id=? AND kind IN ('completed','failed') ORDER BY created,id",
            (task_id,),
        )
        for message in task["messages"]:
            message["content"] = json.loads(message.pop("payload"))["content"]
            if not message["finished"] and (message["answer"] or message["error"]):
                title = (message["answer"] or "")[:200] or message["error"] or message["status"]
                event = next(
                    (
                        e
                        for e in completions
                        if e["kind"] == message["status"]
                        and e["title"] == title
                        and e["created"] >= message["created"]
                    ),
                    None,
                )
                if event:
                    message["finished"] = event["created"]
                    completions.remove(event)
        task["actions"] = self.db.all("SELECT * FROM actions WHERE task_id=? ORDER BY created", (task_id,))
        from .attachments import attachment_view

        task["attachments"] = [
            attachment_view(row)
            for row in self.db.all(
                "SELECT f.* FROM attachments f JOIN actions a ON a.id=f.action_id "
                "WHERE f.task_id=? AND a.status='completed' ORDER BY f.created,f.id",
                (task_id,),
            )
        ]
        from .results import without_images

        for action in task["actions"]:
            review = self.db.one(
                "SELECT reason,once_only FROM action_reviews WHERE action_id=?", (action["id"],)
            )
            if review:
                action["approval"] = review
            if action["result"]:
                action["result"] = packed(without_images(json.loads(action["result"])))
        task["board_cards"] = []
        if self.db.one("SELECT 1 FROM plugins WHERE id='kanban' AND status='active'"):
            task["board_cards"] = self.db.all(
                "SELECT c.* FROM work_cards c WHERE c.task_id=? OR EXISTS "
                "(SELECT 1 FROM work_runs r WHERE r.card_id=c.id AND r.task_id=?) ORDER BY c.updated DESC",
                (task_id, task_id),
            )
        task["events"] = self.db.all(
            "SELECT id,kind,title,created FROM events WHERE task_id=? ORDER BY created DESC,id DESC LIMIT 100",
            (task_id,),
        )
        # Older errors did not record the exhausted limit. Do not infer it from today's settings.
        for row in [task, *task["messages"]]:
            if row.get("error") == LEGACY_BUDGET_ERROR:
                row["error"] = UNKNOWN_BUDGET_ERROR
        for event in task["events"]:
            if event["kind"] in {"error", "failed"} and event["title"] == LEGACY_BUDGET_ERROR:
                event["title"] = UNKNOWN_BUDGET_ERROR
        return task

    def options(self, task_id, values):
        values = ChatOptions.model_validate(values).model_dump(exclude_none=True)
        with self.db.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError("Task not found.")
            if (
                set(values) - {"approval_mode"}
                and db.execute(
                    "SELECT 1 FROM messages WHERE task_id=? AND status IN ('queued','dispatching','running')",
                    (task_id,),
                ).fetchone()
            ):
                raise ValueError(
                    "Wait for this reply to finish before changing the model or thinking effort."
                )
            if values:
                db.execute(
                    "UPDATE tasks SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                    (*values.values(), task_id),
                )
        return self.detail(task_id)
