"""Persistent work cards and bounded, restart-safe agent runs."""

import time

from .db import uid
from .errors import Conflict, NotFound

STATES = {"backlog", "ready", "running", "review", "done", "blocked"}


class Board:
    def __init__(self, db, tasks, plugins):
        self.db, self.tasks, self.plugins = db, tasks, plugins

    def list(self, bot_id=None):
        cards = self.db.all(
            "SELECT * FROM work_cards"
            + (" WHERE bot_id=?" if bot_id else "")
            + " ORDER BY updated DESC LIMIT 200",
            (bot_id,) if bot_id else (),
        )
        for card in cards:
            if card["status"] == "ready":
                card["waiting_for"] = self.db.one(
                    "SELECT id,title,status FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting') ORDER BY created LIMIT 1",
                    (card["bot_id"],),
                )
        return cards

    def get(self, key, bot_id=None):
        row = self.db.one("SELECT * FROM work_cards WHERE id=?", (key,))
        if not row or (bot_id and row["bot_id"] != bot_id):
            raise NotFound("Work card not found for this bot.")
        return row

    def save(self, data, key=None, bot_id=None):
        old = self.get(key, bot_id) if key else None
        if old and old["status"] in {"ready", "running"}:
            raise ValueError("Pause this card before editing its goal or limits.")
        values = {**(old or {}), **data}
        title, goal = values.get("title", ""), values.get("goal", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("Give the card a title of at most 200 characters.")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
            raise ValueError("Describe the goal and how to verify it, using at most 12,000 characters.")
        assigned = bot_id or values.get("bot_id", "default")
        if not self.db.one("SELECT 1 FROM bots WHERE id=? AND archived=0", (assigned,)):
            raise ValueError("Choose an active bot for this card.")
        runs, tokens = values.get("max_runs", 8), values.get("max_tokens", 0)
        if type(runs) is not int or not 1 <= runs <= 100:
            raise ValueError("Allow between 1 and 100 runs.")
        if type(tokens) is not int or (tokens != 0 and not 1000 <= tokens <= 10000000):
            raise ValueError("Use 0 for no token limit, or a budget between 1,000 and 10,000,000.")
        auto = values.get("auto_continue", True)
        if auto not in (True, False, 0, 1):
            raise ValueError("Choose whether the bot may continue between runs.")
        now, key = time.time(), key or uid()
        with self.db.transaction() as sql:
            if old:
                changed = sql.execute(
                    "UPDATE work_cards SET title=?,goal=?,bot_id=?,max_runs=?,max_tokens=?,auto_continue=?,revision=revision+1,updated=? WHERE id=? AND revision=? AND status NOT IN ('ready','running')",
                    (
                        title.strip(),
                        goal.strip(),
                        assigned,
                        runs,
                        tokens,
                        int(auto),
                        now,
                        key,
                        data.get("revision"),
                    ),
                ).rowcount
                if not changed:
                    raise Conflict("This card changed. Reload it before saving.")
            else:
                sql.execute(
                    "INSERT INTO work_cards(id,title,goal,bot_id,max_runs,max_tokens,auto_continue,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, title.strip(), goal.strip(), assigned, runs, tokens, int(auto), now, now),
                )
        return self.get(key)

    def move(self, key, status, revision, bot_id=None):
        old = self.get(key, bot_id)
        if status not in STATES - {"running"}:
            raise ValueError("Choose Backlog, Ready, Review, Done or Blocked.")
        if bot_id and status == "done":
            raise ValueError("Send completed work for owner review with finish_run.")
        if old["status"] == "running" and status != "blocked":
            raise ValueError("Pause running work before moving it.")
        if status == "ready":
            if old["runs"] >= old["max_runs"] or (
                old["max_tokens"] > 0 and old["tokens"] >= old["max_tokens"]
            ):
                raise ValueError("This card reached its budget. Increase the limits before resuming.")
            if not self.plugins.allowed("kanban", old["bot_id"]) or not self.db.one(
                "SELECT 1 FROM plugins WHERE id='kanban' AND status='active'"
            ):
                raise ValueError("Enable the Kanban extension for this bot before starting work.")
            if not self.db.one("SELECT 1 FROM bots WHERE id=? AND archived=0", (old["bot_id"],)):
                raise ValueError("Choose an active bot before starting work.")
        if status == "done" and not old["evidence"].strip():
            raise ValueError("Record evidence of the outcome before marking the card done.")
        with self.db.transaction() as sql:
            changed = sql.execute(
                "UPDATE work_cards SET status=?,revision=revision+1,updated=? WHERE id=? AND revision=?",
                (status, time.time(), key, revision),
            ).rowcount
            if not changed:
                raise Conflict("This card changed. Reload it before moving it.")
            if old["status"] == "running":
                # Fence dispatch before the asynchronous runtime/executor cancellation.
                sql.execute(
                    "UPDATE tasks SET status='cancelled',wait_reason=NULL WHERE id=?", (old["task_id"],)
                )
                sql.execute(
                    "UPDATE messages SET status='cancelled' WHERE task_id=? AND status IN ('queued','dispatching','running')",
                    (old["task_id"],),
                )
                sql.execute(
                    "UPDATE actions SET status='cancelled' WHERE task_id=? AND status IN ('pending','approved','retrying')",
                    (old["task_id"],),
                )
                sql.execute("INSERT OR IGNORE INTO pending_aborts VALUES (?)", (old["task_id"],))
        return self.get(key)

    def finish_run(self, task, outcome, summary, evidence, next_step=""):
        if outcome not in {"completed", "continue", "blocked"}:
            raise ValueError("Report completed, continue or blocked.")
        for value in (summary, evidence, next_step):
            if not isinstance(value, str) or len(value) > 6000:
                raise ValueError("Run notes must be text of at most 6,000 characters each.")
        if (
            not summary.strip()
            or (outcome == "completed" and not evidence.strip())
            or (outcome == "continue" and not next_step.strip())
        ):
            raise ValueError("Include a summary, evidence for completion, and a next step for continuation.")
        with self.db.transaction() as sql:
            card = sql.execute(
                "SELECT * FROM work_cards WHERE task_id=? AND bot_id=? AND status='running'",
                (task["id"], task["bot_id"]),
            ).fetchone()
            if not card:
                raise ValueError("This task is not an active work-card run.")
            sql.execute(
                "UPDATE work_runs SET outcome=?,summary=?,evidence=?,next_step=? WHERE task_id=? AND finished IS NULL",
                (outcome, summary, evidence, next_step, task["id"]),
            )
        return {
            "recorded": True,
            "note": "The card updates when this run finishes. Completed work goes to owner review.",
        }

    def history(self, key, bot_id=None):
        self.get(key, bot_id)
        return self.db.all("SELECT * FROM work_runs WHERE card_id=? ORDER BY created DESC LIMIT 100", (key,))

    def tick(self):
        """Reconcile finished runs, then admit at most one card per idle bot."""
        now = time.time()
        with self.db.transaction() as sql:
            sql.execute(
                "UPDATE work_cards SET tokens=(SELECT COALESCE(SUM(t.tokens),0) FROM work_runs r JOIN tasks t ON t.id=r.task_id WHERE r.card_id=work_cards.id) WHERE EXISTS (SELECT 1 FROM work_runs r WHERE r.card_id=work_cards.id)"
            )
            for run in sql.execute(
                "SELECT r.*,t.status AS task_status,t.tokens AS used_tokens,t.error FROM work_runs r JOIN tasks t ON t.id=r.task_id WHERE r.finished IS NULL AND t.status IN ('completed','failed','cancelled')"
            ).fetchall():
                card = sql.execute("SELECT * FROM work_cards WHERE id=?", (run["card_id"],)).fetchone()
                used = card["tokens"]
                status = "review"
                summary = (
                    run["summary"]
                    or "The run ended without a work report. Review the conversation before continuing."
                )
                if run["task_status"] != "completed" or run["outcome"] == "blocked":
                    status = "blocked"
                    summary = (
                        run["error"]
                        or run["summary"]
                        or "The run stopped. Review the conversation before resuming."
                    )
                elif run["outcome"] == "continue" and card["auto_continue"]:
                    status = (
                        "ready"
                        if card["runs"] < card["max_runs"]
                        and (card["max_tokens"] == 0 or used < card["max_tokens"])
                        else "blocked"
                    )
                    if status == "blocked":
                        summary += " The card reached its run or token budget."
                sql.execute("UPDATE work_runs SET finished=? WHERE task_id=?", (now, run["task_id"]))
                # A manual pause/move wins over a late completion.
                sql.execute(
                    "UPDATE work_cards SET tokens=?,status=CASE WHEN status='running' THEN ? ELSE status END,summary=?,evidence=?,next_step=?,revision=revision+1,updated=? WHERE id=? AND task_id=?",
                    (
                        used,
                        status,
                        summary,
                        run["evidence"],
                        run["next_step"],
                        now,
                        card["id"],
                        run["task_id"],
                    ),
                )
            for card in sql.execute(
                "SELECT * FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY bot_id ORDER BY updated,id) AS position FROM work_cards WHERE status='ready') WHERE position=1 ORDER BY updated,id"
            ).fetchall():
                if sql.execute(
                    "SELECT 1 FROM work_runs r JOIN pending_aborts a ON a.task_id=r.task_id WHERE r.card_id=?",
                    (card["id"],),
                ).fetchone():
                    continue
                if card["runs"] >= card["max_runs"] or (
                    card["max_tokens"] > 0 and card["tokens"] >= card["max_tokens"]
                ):
                    sql.execute(
                        "UPDATE work_cards SET status='blocked',summary='Run or token budget reached.',revision=revision+1 WHERE id=?",
                        (card["id"],),
                    )
                    continue
                if sql.execute(
                    "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting')",
                    (card["bot_id"],),
                ).fetchone():
                    continue
                if not sql.execute(
                    "SELECT 1 FROM bots WHERE id=? AND archived=0", (card["bot_id"],)
                ).fetchone():
                    continue
                if (
                    not self.plugins.allowed("kanban", card["bot_id"])
                    or not sql.execute(
                        "SELECT 1 FROM plugins WHERE id='kanban' AND status='active'"
                    ).fetchone()
                ):
                    continue
                prompt = (
                    f"Work card {card['id']}: {card['title']}\nOriginal goal (keep its full scope):\n{card['goal']}\n"
                    f"Previous run: {card['summary'][:1500]}\nEvidence: {card['evidence'][:1500]}\nNext step: {card['next_step'][:1500]}\n"
                    "Make concrete progress. Inspect current evidence, avoid repeating completed or uncertain actions. "
                    "Use Kanban finish_run before ending: completed only with evidence covering the entire goal; "
                    "continue with a concrete next step; blocked when owner input is needed. Completion goes to owner review."
                )
                try:
                    task = self.tasks.create(
                        prompt[:20000],
                        title=card["title"],
                        source="board:" + card["id"],
                        bot_id=card["bot_id"],
                        connection=sql,
                    )
                except ValueError as exc:
                    sql.execute(
                        "UPDATE work_cards SET status='blocked',summary=?,revision=revision+1,updated=? WHERE id=?",
                        (str(exc)[:1000], now, card["id"]),
                    )
                    continue
                limit = min(
                    (
                        value
                        for value in (
                            self.db.setting("preferences")["task_tokens"],
                            card["max_tokens"] - card["tokens"],
                        )
                        if value > 0
                    ),
                    default=0,
                )
                sql.execute("UPDATE tasks SET max_tokens=? WHERE id=?", (limit, task["id"]))
                sql.execute(
                    "INSERT INTO work_runs(card_id,task_id,created) VALUES (?,?,?)",
                    (card["id"], task["id"], now),
                )
                sql.execute(
                    "UPDATE work_cards SET status='running',task_id=?,runs=runs+1,revision=revision+1,updated=? WHERE id=?",
                    (task["id"], now, card["id"]),
                )

    def tool(self, name, args, task):
        bot_id = task["bot_id"]
        if name == "list_cards":
            return self.list(bot_id)
        if name == "save_card":
            return self.save(args, args.get("id"), bot_id)
        if name == "card_history":
            return self.history(args["id"], bot_id)
        if name == "finish_run":
            return self.finish_run(task, **args)
        raise ValueError("Unknown Kanban tool.")
