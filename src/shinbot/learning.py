"""Turn tested procedures into versioned, scoped skill proposals."""

import hashlib
import json
import re
import time
from pathlib import Path

from .extensions import skill_package


class Learning:
    def __init__(self, db, plugins):
        self.db, self.plugins = db, plugins

    def propose(self, task, name, description, instructions, evidence):
        if not self.db.setting("preferences").get("learn_from_tasks", True):
            raise ValueError("Learning from tasks is disabled in Settings.")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name)
            or len(name) > 40
        ):
            raise ValueError("Give the procedure a short lowercase name with hyphens.")
        if not isinstance(description, str) or not 10 <= len(description.strip()) <= 500:
            raise ValueError("Describe when this procedure is useful in 10 to 500 characters.")
        if not isinstance(instructions, str) or not 50 <= len(instructions.strip()) <= 12000:
            raise ValueError(
                "Write a reusable procedure of 50 to 12,000 characters, including how to verify it."
            )
        if (
            not isinstance(evidence, list)
            or not 1 <= len(evidence) <= 10
            or any(not isinstance(e, str) for e in evidence)
        ):
            raise ValueError("Cite 1 to 10 completed tool action IDs from this task.")
        records = []
        for key in dict.fromkeys(evidence):
            row = self.db.one(
                "SELECT id,tool,status,result,hash FROM actions WHERE id=? AND task_id=?", (key, task["id"])
            )
            if (
                not row
                or row["status"] != "completed"
                or row["tool"].split(":", 1)[0]
                in {"plugins", "notes", "memory", "bots", "status", "shinbot_help", "updates"}
                or row["tool"].startswith("plugin:kanban:")
            ):
                raise ValueError(
                    "Evidence must reference completed workspace, browser or external-tool actions from this task."
                )
            result = json.loads(row["result"] or "{}")
            if isinstance(result, dict) and (result.get("isError") or result.get("exit_code", 0) != 0):
                raise ValueError("Cite a successful verification action after correcting the failure.")
            records.append(
                {
                    "id": row["id"],
                    "tool": row["tool"],
                    "args_hash": row["hash"],
                    "result_sha256": hashlib.sha256((row["result"] or "").encode()).hexdigest(),
                }
            )
        slug = "learned-" + hashlib.sha256(task["bot_id"].encode()).hexdigest()[:8] + "-" + name
        plugin_id = "skill-" + slug
        existing = self.db.all("SELECT * FROM plugins WHERE id=? ORDER BY created DESC", (plugin_id,))
        if existing and not self.plugins.allowed(plugin_id, task["bot_id"]):
            raise ValueError("This learned skill is no longer assigned to this bot.")
        if existing and all(row["status"] == "disabled" for row in existing):
            raise ValueError("The owner disabled this learned skill. Do not recreate it automatically.")
        digest = hashlib.sha256((description.strip() + "\n" + instructions.strip()).encode()).hexdigest()
        for row in existing:
            _, files = self.plugins.package(plugin_id, row["version"])
            if json.loads(files.get("LEARNING.json", b"{}")).get("procedure_sha256") == digest:
                return {"installed": self.plugins.public(row), "unchanged": True}
        provenance = {
            "task_id": task["id"],
            "bot_id": task["bot_id"],
            "evidence": records,
            "procedure_sha256": digest,
        }
        content = {
            "SKILL.md": (
                "---\nname: "
                + slug
                + "\ndescription: "
                + json.dumps(description.strip())
                + "\nlicense: MIT\n---\n\n"
                + instructions.strip()
                + "\n"
            ).encode(),
            "LICENSE": (Path(__file__).parent / "bundled/official/plugins/kanban/LICENSE").read_bytes(),
            "LEARNING.json": json.dumps(provenance, indent=2).encode(),
        }
        files = skill_package(content, "MIT", "Learned in task " + task["id"], learned=True)
        manifest = json.loads(files["shinbot-plugin.json"])
        manifest["name"] = name.replace("-", " ").capitalize()
        files["shinbot-plugin.json"] = json.dumps(manifest).encode()
        with self.db.transaction() as sql:
            if (
                sql.execute("SELECT 1 FROM learning_proposals WHERE task_id=?", (task["id"],)).fetchone()
                or sql.execute(
                    "SELECT COUNT(*) FROM learning_proposals WHERE bot_id=? AND created>?",
                    (task["bot_id"], time.time() - 86400),
                ).fetchone()[0]
                >= 3
            ):
                raise ValueError(
                    "Learning is limited to one proposal per task and three per bot per day. Reuse or review existing skills."
                )
            sql.execute(
                "INSERT INTO learning_proposals VALUES (?,?,?)", (task["id"], task["bot_id"], time.time())
            )
        try:
            result = self.plugins.stage(files, learned=True)
            if not existing:
                self.plugins.set_access(result["id"], {"mode": "bots", "bots": [task["bot_id"]]})
        except Exception:
            self.db.execute("DELETE FROM learning_proposals WHERE task_id=?", (task["id"],))
            raise
        self.db.event("learning", "A learned skill is ready for review: " + name, task["id"])
        return {
            "installed": self.plugins.public(
                self.db.one(
                    "SELECT * FROM plugins WHERE id=? AND version=?", (result["id"], result["version"])
                )
            ),
            "note": "Review the procedure and its evidence in Plugins. Activation follows the workspace extension permission; existing chats retain their versions. Older versions remain available for rollback.",
        }
