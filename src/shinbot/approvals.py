"""Owner-managed standing approvals, separate from agent-editable task arguments."""

import hashlib
import json
import shlex
import time

from .db import packed, uid


def outside_workspace(word):
    # An option can carry a path after "=" or glued on, as in --files0-from=/proc/1/environ or -I/etc.
    path = word.rsplit("=", 1)[-1] if word.startswith("-") else word
    return (
        ".." in path.split("/")
        or (path.startswith("/") and not (path + "/").startswith("/workspace/"))
        or (word.startswith("-") and "=" not in word and "/" in word)
    )


def guarded_action(tool, args):
    if tool == "desktop":
        return args.get("operation") not in {"observe", "screenshot"}
    if tool == "write_file":
        return True
    if tool == "run_command":
        command = args.get("command", "")
        # The shell expands globs, and paths outside the workspace reach /proc and other services' files.
        if any(character in command for character in "\n\r;&|><`$()*?[]{}~"):
            return True
        try:
            words = shlex.split(command)
        except ValueError:
            return True
        if any(outside_workspace(word) for word in words[1:]):
            return True
        # Everything beyond these read-only commands needs a separate decision in Auto.
        return not (
            words
            and (words[0] in {"pwd", "ls", "echo", "cat", "head", "tail", "wc"} or words == ["git", "status"])
        )
    return (
        tool in {"notes", "memory", "jobs", "bots", "plugins"}
        and args.get("operation")
        in {
            "delete",
            "forget",
            "pause",
            "save",
            "install",
            "activate",
            "connect_mcp",
            "install_skills",
            "import_source",
            "set_access",
            "save_template",
        }
        and not (tool == "notes" and args.get("operation") == "save")
    )


def exact_rule_key(tool, args):
    return hashlib.sha256(packed({"tool": tool, "args": args}).encode()).hexdigest()


def rule_for(tool, args):
    # Plugin identity includes its version and tool name; operation selects a built-in command.
    operation = args.get("operation", "") if tool in {"browser", "desktop", "plugins", "memory", "jobs", "bots"} else ""
    key = "command-v1:" + hashlib.sha256(packed({"tool": tool, "operation": operation}).encode()).hexdigest()
    name = {
        "run_command": "Run shell commands",
        "browser": "Browser action",
        "desktop": "Desktop action",
        "plugins": "Extension change",
        "memory": "Save a memory",
        "jobs": "Scheduled job",
        "bots": "Bot profile change",
    }.get(tool, tool)
    if tool.startswith("plugin:"):
        _, plugin, version, command = tool.split(":", 3)
        name = f"{plugin} {version} · {command}"
    label = f"{name}{' · ' + str(operation) if operation else ''} · any arguments"
    return key, label


class Approvals:
    def __init__(self, db):
        self.db = db

    def allows(self, task, tool, args, connection):
        key, _ = rule_for(tool, args)
        # Delegated tasks retain their inherited boundary; only owner approvals for that chat apply.
        global_scope = not task["parent_task_id"]
        return bool(
            connection.execute(
                "SELECT 1 FROM approval_rules WHERE rule_key IN (?,?) AND "
                "((scope='chat' AND task_id=?) OR (scope='always' AND ?))",
                (key, exact_rule_key(tool, args), task["id"], global_scope),
            ).fetchone()
        )

    def decide(self, action_id, approved, scope="once"):
        if scope not in ("once", "chat", "always") or (not approved and scope != "once"):
            raise ValueError("Choose Allow once, Allow for this chat, Always allow, or Decline.")
        with self.db.transaction() as db:
            action = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not action or action["status"] != "pending" or action["expires"] <= time.time():
                raise ValueError("This approval is no longer pending.")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (action["task_id"],)).fetchone()
            if not task or task["status"] in {"cancelled", "paused", "uncertain"}:
                raise ValueError("Resume this conversation before approving an action.")
            review = db.execute("SELECT * FROM action_reviews WHERE action_id=?", (action_id,)).fetchone()
            if approved and scope != "once" and review and review["once_only"]:
                raise ValueError("This action needs a separate decision. Choose Allow once.")
            if approved and scope != "once":
                key, label = rule_for(action["tool"], json.loads(action["args"]))
                db.execute(
                    "INSERT OR IGNORE INTO approval_rules VALUES (?,?,?,?,?,?,?)",
                    (
                        uid(),
                        scope,
                        action["task_id"] if scope == "chat" else "",
                        key,
                        label,
                        time.time(),
                        action_id,
                    ),
                )
            db.execute(
                "UPDATE actions SET status=? WHERE id=?", ("approved" if approved else "denied", action_id)
            )
            db.execute(
                "INSERT INTO action_reviews(action_id,approved) VALUES (?,?) ON CONFLICT(action_id) DO UPDATE SET approved=excluded.approved",
                (action_id, int(approved)),
            )
            db.execute(
                "UPDATE tasks SET status='running' WHERE id=? AND status='waiting'", (action["task_id"],)
            )

    def list(self):
        return self.db.all(
            "SELECT r.*,t.title AS chat_title,a.args FROM approval_rules r LEFT JOIN actions a ON a.id=r.action_id LEFT JOIN tasks t ON t.id=a.task_id ORDER BY r.created DESC"
        )

    def revoke(self, key):
        self.db.execute("DELETE FROM approval_rules WHERE id=?", (key,))
