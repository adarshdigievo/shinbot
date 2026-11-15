"""Telegram channel state: owner binding, commands, approvals and the delivery outbox."""

import html
import json
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .db import packed, uid

PLUGIN = "telegram"
TOKEN, OWNER = "SHINBOT_PLUGIN_TELEGRAM_BOT_TOKEN", "SHINBOT_PLUGIN_TELEGRAM_OWNER_ID"
COMMANDS = [
    ("new", "Start a new conversation"),
    ("stop", "Stop the work in progress"),
    ("steer", "Redirect the work in progress"),
    ("status", "Check your conversation"),
    ("pending", "Show approvals waiting for you"),
    ("bots", "Choose a bot"),
    ("jobs", "Show scheduled jobs"),
    ("help", "Show these commands"),
]
HELP = "\n".join(
    [
        "/new — start a new conversation",
        "/stop — stop the work in progress",
        "/steer TEXT — redirect the work in progress",
        "/status — check your conversation",
        "/pending — show approvals waiting for you",
        "/bots — choose a bot",
        "/jobs — show scheduled jobs",
    ]
)
DECISIONS = {
    "o": (True, "once", "Allowed once"),
    "c": (True, "chat", "Allowed for this chat"),
    "a": (True, "always", "Always allowed"),
    "d": (False, "once", "Declined"),
}
VERBS = {
    "desktop": {
        "observe": "Looking at the desktop",
        "launch": "Opening an application",
        "click": "Clicking on the desktop",
        "type": "Typing on the desktop",
        "press": "Using the keyboard",
        "scroll": "Scrolling",
        "screenshot": "Sharing a desktop screenshot",
    },
    "browser": {
        "goto": "Opening a webpage",
        "observe": "Reading the webpage",
        "click": "Clicking on the page",
        "fill": "Filling a field",
        "press": "Using the keyboard",
        "back": "Going back",
        "close": "Closing the browser",
        "tabs": "Checking browser tabs",
        "screenshot": "Sharing a browser screenshot",
    },
    "notes": {"search": "Searching notes", "save": "Saving a note", "delete": "Deleting a note"},
    "memory": {
        "search": "Checking saved preferences",
        "remember": "Saving a preference",
        "forget": "Removing a saved preference",
    },
    "plugins": {
        "search": "Finding a plugin",
        "read_skill": "Reading a skill",
        "stage": "Preparing a plugin",
        "install": "Installing a plugin",
        "activate": "Activating a plugin",
        "learn": "Preparing a reusable skill",
    },
}
TOOLS = {
    "run_command": "Running a command",
    "list_files": "Listing files",
    "read_file": "Reading a file",
    "write_file": "Writing a file",
    "attach_file": "Attaching a file",
    "shinbot_help": "Reading ShinBot help",
    "status": "Checking workspace status",
    "delegate": "Delegating work",
    "delegate_task": "Delegating work",
    "list_projects": "Listing projects",
    "card_history": "Reading card history",
    "updates": "Checking for updates",
}


def describe(tool, args):
    """The same wording the web app uses for an action, plus its most telling argument."""
    name = tool.split(":", 3)[3] if tool.count(":") >= 3 and tool.startswith("plugin:") else tool
    label = (
        VERBS.get(tool, {}).get(args.get("operation"))
        or TOOLS.get(name)
        or "Using " + name.replace("_", " ")
    )
    detail = ""
    if tool == "browser" and isinstance(args.get("url"), str):
        url = urlsplit(args["url"])
        detail = (url.hostname or "") + url.path
    elif tool in {"list_files", "read_file", "write_file", "attach_file"}:
        detail = str(args.get("path") or "")
    elif tool == "run_command":
        detail = str(args.get("command") or "").split("\n")[0]
    return label, detail[:180]


def enqueue(db, text, owner, task_id=None, **extra):
    db.execute(
        "INSERT INTO outbox VALUES (?,?,?,'pending',?,?,NULL)",
        (uid(), PLUGIN, packed({"text": text, "chat_id": str(owner), **extra}), time.time(), task_id),
    )


class Telegram:
    def __init__(self, core):
        self.core, self.db = core, core.db

    def settings(self):
        """The bot token and owner of the enabled plugin, or None while Telegram is off."""
        row = self.db.one("SELECT version FROM plugins WHERE id=? AND status='active'", (PLUGIN,))
        if not row:
            return None
        values = self.core.plugins.configuration(PLUGIN, row["version"], environment=True)
        owner = values.get(OWNER, "").strip()
        if owner and (not owner.isdigit() or int(owner) <= 0):
            raise ValueError(
                "Your Telegram user ID is a number. Message your bot to see it, then update the connection."
            )
        return {"token": values[TOKEN].strip(), "owner": owner}

    def connection(self):
        return self.db.setting("telegram_connection", {"state": "off", "detail": "", "username": ""})

    def status(self, state, detail="", username=""):
        value = {"state": state, "detail": detail, "username": username}
        if self.connection() != value:
            self.db.set("telegram_connection", value)

    def owner(self):
        if self.connection()["state"] != "connected":
            return ""
        return (self.db.setting("telegram_account") or {}).get("owner_id", "")

    def held(self):
        return self.core.config.safe_mode or self.db.setting("restore_hold", False)

    def register(self, owner, bot_id):
        account = {"owner_id": str(owner), "bot_id": str(int(bot_id))}
        previous = self.db.setting("telegram_account")
        with self.db.transaction() as db:
            if previous and previous != account:
                # Offsets, chats and queued replies belong to the previous bot or owner.
                db.execute("DELETE FROM channel_updates WHERE channel='telegram'")
                db.execute("DELETE FROM channel_chats WHERE channel='telegram'")
                db.execute("DELETE FROM settings WHERE key IN ('telegram_offset','telegram_selected_bot')")
                db.execute(
                    "UPDATE outbox SET status='failed' WHERE channel='telegram' AND status IN ('pending','sending')"
                )
            db.execute("INSERT OR REPLACE INTO settings VALUES ('telegram_account',?)", (packed(account),))

    def offset(self):
        return self.db.setting("telegram_offset", 0)

    def checkpoint(self, value, db=None):
        def save(db):
            old = db.execute("SELECT value FROM settings WHERE key='telegram_offset'").fetchone()
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('telegram_offset',?)",
                (packed(max(value, json.loads(old[0]) if old else 0)),),
            )

        if db is not None:
            return save(db)
        with self.db.transaction() as db:
            save(db)

    def bots(self):
        every = self.core.bots.list()
        return [bot for bot in every if self.core.plugins.allowed(PLUGIN, bot["id"])] or every

    def jobs(self):
        rows = self.db.all(
            "SELECT name,enabled,next_run,timezone FROM schedules WHERE archived=0 ORDER BY enabled DESC,next_run"
        )
        if not rows:
            return f"No scheduled jobs yet. Ask your bot to create one, or open {self.core.config.origin}"
        lines = []
        for job in rows[:20]:
            when = "paused"
            if job["enabled"] and job["next_run"]:
                try:
                    zone = ZoneInfo(job["timezone"])
                except (KeyError, ValueError):
                    zone = ZoneInfo("UTC")
                when = "next " + datetime.fromtimestamp(job["next_run"], zone).strftime("%a %d %b %H:%M %Z")
            lines.append(f"• {job['name']} — {when}")
        more = f"\n…and {len(rows) - 20} more in the app." if len(rows) > 20 else ""
        return "Scheduled jobs:\n" + "\n".join(lines) + more

    def inbound(self, update_id, content, notice=""):
        """Record one owner message exactly once and turn it into a command reply or task work."""
        owner = self.owner()
        if not owner:
            raise ValueError("Telegram is not linked to an owner.")
        prompt, update_key = content, str(update_id)
        selected = self.db.setting("telegram_selected_bot", "default")
        bots = self.bots()
        bot = next((bot for bot in bots if bot["id"] == selected), bots[0])
        origin = self.core.config.origin
        with self.db.transaction() as db:
            prior = db.execute(
                "SELECT task_id FROM channel_updates WHERE channel='telegram' AND update_id=?", (update_key,)
            ).fetchone()
            if prior:
                return {"task_id": prior["task_id"] or None, "duplicate": True}
            chat = db.execute(
                "SELECT task_id FROM channel_chats WHERE channel='telegram' AND chat_id=?", (owner,)
            ).fetchone()
            existing = (
                db.execute("SELECT id,status,bot_id FROM tasks WHERE id=?", (chat["task_id"],)).fetchone()
                if chat
                else None
            )
            busy = existing and existing["status"] in {"queued", "running", "waiting"}
            task_id, reply, extra, cancel = None, None, {}, None
            command, _, argument = prompt.strip().partition(" ")
            command, argument = command.split("@", 1)[0].lower(), argument.strip()
            if command in {"/start", "/help"}:
                reply = (
                    f"You’re talking to {bot['name']}. Send a message to begin.\n\n{HELP}\n\n"
                    f"Approvals arrive here with buttons. Manage everything else at {origin}"
                )
            elif command == "/bots":
                reply = "Choose who you want to talk to. Switching starts a new conversation."
                extra["buttons"] = [
                    [[("✓ " if b["id"] == bot["id"] else "") + b["name"][:40], "b:" + b["id"]]] for b in bots[:30]
                ]
            elif command == "/bot":
                matches = [b for b in bots if b["id"] == argument or b["name"].casefold() == argument.casefold()]
                if len(matches) != 1:
                    reply = "Send /bots and tap a bot, or use /bot followed by its exact name."
                else:
                    bot = matches[0]
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES ('telegram_selected_bot',?)",
                        (packed(bot["id"]),),
                    )
                    db.execute("DELETE FROM channel_chats WHERE channel='telegram' AND chat_id=?", (owner,))
                    reply = f"Switched to {bot['name']}. Your next message starts a new conversation."
            elif command == "/new":
                db.execute("DELETE FROM channel_chats WHERE channel='telegram' AND chat_id=?", (owner,))
                reply = f"Ready for a new conversation with {bot['name']}. Send your first message."
                if busy:
                    reply += " The earlier one keeps working; its reply will still arrive here."
            elif command == "/status":
                state = {
                    "queued": "waiting to start",
                    "running": "working",
                    "waiting": "waiting for your approval — send /pending to see it",
                    "completed": "finished; send a message to continue",
                    "failed": "the last reply failed; send a message to try again",
                    "cancelled": "stopped",
                }
                reply = (
                    f"Bot: {bot['name']}\n"
                    f"Conversation: {state.get(existing['status'], existing['status']) if existing else 'not started'}\n"
                    f"{origin}"
                )
            elif command == "/stop":
                if busy:
                    cancel = existing["id"]
                    db.execute("DELETE FROM channel_chats WHERE channel='telegram' AND chat_id=?", (owner,))
                    reply = "Stopped. Your next message starts a new conversation."
                else:
                    reply = "Nothing is running right now."
            elif command == "/pending":
                waiting = db.execute(
                    "SELECT a.id,a.task_id FROM actions a JOIN tasks t ON t.id=a.task_id "
                    "WHERE a.status='pending' AND a.expires>? AND (t.source=? OR t.source LIKE 'job:%') "
                    "ORDER BY a.created LIMIT 10",
                    (time.time(), f"telegram:{owner}"),
                ).fetchall()
                for action in waiting:
                    enqueue(db, "", owner, action["task_id"], approval=action["id"])
                if not waiting:
                    reply = "Nothing is waiting for your approval."
            elif command == "/jobs":
                reply = self.jobs()
            elif command == "/steer" and not argument:
                reply = "Add your new direction after the command, for example: /steer use the staging server"
            elif command == "/unsupported":
                reply = "Send text for now. I can’t open photos, voice messages or files sent on Telegram yet."
            elif command.startswith("/") and command != "/steer":
                reply = "I don’t recognize that command. Send /help for the available commands."
            elif not prompt.strip():
                reply = "Send a message with some text to start."
            elif (
                not existing
                or existing["bot_id"] != bot["id"]
                or existing["status"] in {"cancelled", "paused", "uncertain"}
            ):
                try:
                    task_id = self.core.tasks.create(
                        argument if command == "/steer" else prompt,
                        source=f"telegram:{owner}",
                        connection=db,
                        bot_id=bot["id"],
                    )["id"]
                except ValueError as exc:
                    reply = f"{str(exc)[:500]} Open {origin}, then send your message again."
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO channel_chats VALUES ('telegram',?,?)", (owner, task_id)
                    )
            else:
                task_id = chat["task_id"]
                steer = command == "/steer"
                self.core.tasks._message(
                    db,
                    task_id,
                    argument if steer else prompt,
                    f"telegram:{update_id}",
                    "steer" if steer and busy else "queue",
                )
                db.execute(
                    "UPDATE tasks SET status='queued' WHERE id=? AND status IN ('completed','failed')",
                    (task_id,),
                )
            if task_id and notice:
                enqueue(db, notice, owner)
            if reply:
                enqueue(db, reply, owner, **extra)
            db.execute("INSERT INTO channel_updates VALUES ('telegram',?,?)", (update_key, task_id or ""))
            self.checkpoint(update_id + 1, db)
        return {"task_id": task_id, "duplicate": False, "cancel": cancel}

    def request_approval(self, db, task, action_id):
        """Queue an approval prompt for work that started on Telegram or in a job that notifies."""
        source, recipient = task["source"], ""
        if source.startswith("telegram:"):
            recipient = source.split(":", 1)[1]
        elif source.startswith("job:"):
            job = db.execute("SELECT notify FROM schedules WHERE id=?", (source[4:],)).fetchone()
            recipient = self.owner() if job and job["notify"] else ""
        if recipient:
            enqueue(db, "", recipient, task["id"], approval=action_id)

    def approval_prompt(self, action_id):
        row = self.db.one(
            "SELECT a.tool,a.args,a.status,a.expires,t.title,t.source,r.reason,r.once_only FROM actions a "
            "JOIN tasks t ON t.id=a.task_id LEFT JOIN action_reviews r ON r.action_id=a.id WHERE a.id=?",
            (action_id,),
        )
        if not row or row["status"] != "pending" or row["expires"] <= time.time():
            return None
        label, detail = describe(row["tool"], json.loads(row["args"]))
        lines = ["<b>Approval needed</b>"]
        if row["source"].startswith("job:"):
            lines.append("Scheduled job: " + html.escape(row["title"][:120], quote=False))
        lines.append(html.escape(label, quote=False))
        if detail:
            lines.append("<pre>" + html.escape(detail, quote=False) + "</pre>")
        if row["reason"]:
            lines.append(html.escape(row["reason"][:600], quote=False))
        buttons = [[["Allow once", f"a:{action_id}:o"], ["Decline", f"a:{action_id}:d"]]]
        if not row["once_only"]:
            buttons.append([["Allow for this chat", f"a:{action_id}:c"], ["Always allow", f"a:{action_id}:a"]])
        return {"html": "\n".join(lines), "buttons": buttons}

    def decide(self, action_id, choice):
        if choice not in DECISIONS:
            return False, "This button is no longer valid."
        approved, scope, outcome = DECISIONS[choice]
        try:
            self.core.approvals.decide(action_id, approved, scope)
        except ValueError as exc:
            return False, str(exc)
        return True, outcome

    def progress(self):
        """Steps of the reply in progress in the owner's current conversation."""
        owner = self.owner()
        task = self.db.one(
            "SELECT t.id,t.status FROM channel_chats c JOIN tasks t ON t.id=c.task_id "
            "WHERE c.channel='telegram' AND c.chat_id=?",
            (owner,),
        )
        if not owner or not task or task["status"] not in {"queued", "running"}:
            return None
        started = self.db.one(
            "SELECT MIN(created) AS at FROM messages WHERE task_id=? AND status IN ('dispatching','running')",
            (task["id"],),
        )["at"]
        actions = (
            self.db.all(
                "SELECT tool,args FROM actions WHERE task_id=? AND created>=? ORDER BY created,rowid",
                (task["id"], started),
            )
            if started
            else []
        )
        return {"task_id": task["id"], "steps": [describe(a["tool"], json.loads(a["args"])) for a in actions]}

    def attention(self):
        """Tell the owner, once, when their conversation has stopped and needs them in the app."""
        owner = self.owner()
        task = self.db.one(
            "SELECT t.id,t.status,t.error FROM channel_chats c JOIN tasks t ON t.id=c.task_id "
            "WHERE c.channel='telegram' AND c.chat_id=?",
            (owner,),
        )
        if not owner or not task or task["status"] not in {"uncertain", "paused"}:
            return
        mark = f"{task['id']}:{task['status']}"
        if self.db.setting("telegram_attention") == mark:
            return
        reason = (
            "I couldn’t confirm whether the last action finished, so this conversation is on hold."
            if task["status"] == "uncertain"
            else "This conversation is paused." + (f" {task['error'][:300]}" if task["error"] else "")
        )
        with self.db.transaction() as db:
            enqueue(
                db,
                f"{reason}\nReview it at {self.core.config.origin} or send your next message to start a new conversation.",
                owner,
                task["id"],
            )
            db.execute("INSERT OR REPLACE INTO settings VALUES ('telegram_attention',?)", (packed(mark),))

    def files(self, ids):
        rows = [self.db.one("SELECT * FROM attachments WHERE id=?", (key,)) for key in ids]
        return [
            {**row, "path": self.core.attachments.files.path(row["digest"])}
            for row in rows
            if row
        ]

    def claim(self):
        owner = self.owner()
        if self.held() or not owner:
            return None
        with self.db.transaction() as db:
            db.execute(
                "UPDATE outbox SET status='uncertain' WHERE channel='telegram' AND status='sending' AND claim_until<?",
                (time.time(),),
            )
            # Messages retain their intended recipient even if the owner changes, and day-old
            # ones would only confuse after a long disconnection.
            db.execute(
                "UPDATE outbox SET status='failed' WHERE channel='telegram' AND status='pending' AND "
                "(COALESCE(json_extract(body,'$.chat_id'),'')<>? OR created<?)",
                (owner, time.time() - 86400),
            )
            item = db.execute(
                "SELECT * FROM outbox WHERE channel='telegram' AND status='pending' ORDER BY created,rowid LIMIT 1"
            ).fetchone()
            if not item:
                return None
            db.execute(
                "UPDATE outbox SET status='sending',claim_until=? WHERE id=?", (time.time() + 300, item["id"])
            )
            return {"id": item["id"], "task_id": item["task_id"], "body": json.loads(item["body"])}

    def receipt(self, item_id, status):
        if status not in {"sent", "uncertain", "failed"}:
            raise ValueError("Invalid delivery receipt.")
        self.db.execute(
            "UPDATE outbox SET status=? WHERE id=? AND channel='telegram' AND status='sending'",
            (status, item_id),
        )
