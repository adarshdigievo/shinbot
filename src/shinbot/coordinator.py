import asyncio
import json
import logging
import time

import httpx

from .channels import enqueue

log = logging.getLogger(__name__)


class Coordinator:
    def __init__(self, core):
        self.core = core
        self.ready = False
        self.last_error = None
        self.client = None
        self.wake = asyncio.Event()
        self.last_health = 0.0
        self.polls = {}
        self.dispatch_order = {}

    def settle_task_status(self, sql, task_id, status, error=None):
        pending = sql.execute(
            "SELECT status FROM messages WHERE task_id=? AND status IN ('running','dispatching','queued') ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END LIMIT 1",
            (task_id,),
        ).fetchone()
        current = sql.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if pending:
            status = (
                ("waiting" if current["status"] == "waiting" else "running")
                if pending["status"] == "running"
                else "queued"
            )
        sql.execute(
            "UPDATE tasks SET status=?,error=?,updated=?,wait_reason=CASE WHEN ?='waiting' THEN wait_reason ELSE NULL END WHERE id=? AND status NOT IN ('cancelled','paused','uncertain')",
            (status, None if pending else error, time.time(), status, task_id),
        )

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def request(self, method, path, **kwargs):
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=30,
                trust_env=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        response = await self.client.request(
            method,
            self.core.config.runtime_url + path,
            headers={"Authorization": f"Bearer {self.core.config.service_token}"},
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def reconcile(self):
        bootstrap = self.core.runtime_bootstrap()
        await self.request("POST", "/v1/reconcile", json=bootstrap)
        self.ready = True

    async def abort_task(self, task_id):
        await self.request("POST", f"/v1/tasks/{task_id}/abort", json={})
        rows = await asyncio.to_thread(
            self.core.db.all,
            "SELECT submission_id FROM messages WHERE task_id=? AND submission_id IS NOT NULL",
            (task_id,),
        )
        finished = True
        for row in rows:
            result = await self.request("GET", f"/v1/submissions/{row['submission_id']}")
            await asyncio.to_thread(
                self.core.db.record_usage, task_id, result.get("tokens", 0), result.get("cost", 0)
            )
            finished = finished and result["status"] in {"done", "unanswered"}
        if finished:
            await asyncio.to_thread(
                self.core.db.execute, "DELETE FROM pending_aborts WHERE task_id=?", (task_id,)
            )
        return finished

    async def tick(self):
        db = self.core.db
        now = time.monotonic()
        if not self.ready or now - self.last_health >= 15:
            health = await self.request("GET", "/v1/health")
            self.last_health = now
            if health["held"] or not self.ready:
                await self.reconcile()
        if self.core.config.safe_mode or await asyncio.to_thread(db.setting, "restore_hold", False):
            return
        await asyncio.to_thread(self.core.jobs.tick)
        await asyncio.to_thread(self.core.board.tick)
        await asyncio.to_thread(self.core.projects.deliver_results)
        for abort in db.all("SELECT task_id FROM pending_aborts"):
            try:
                await self.abort_task(abort["task_id"])
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    db.execute("DELETE FROM pending_aborts WHERE task_id=?", (abort["task_id"],))
            except httpx.HTTPError:
                pass
        # Dispatch and poll separately so waiting conversations cannot consume admission slots.
        select = (
            "SELECT m.*,t.provider,t.model,t.thinking_level,t.extensions,t.grants,t.max_tokens,t.bot_id,t.bot_profile,t.browser_id,t.workspace_id,t.workspace_path,t.parent_task_id,t.approval_mode,COALESCE(t.workspace_id,t.bot_id) AS lane,t.status AS task_status "
            "FROM messages m JOIN tasks t ON t.id=m.task_id WHERE t.status NOT IN ('paused','uncertain','cancelled') AND "
        )
        queued = db.all(
            "SELECT * FROM ("
            + select.replace(
                "SELECT m.*",
                "SELECT ROW_NUMBER() OVER (PARTITION BY COALESCE(t.workspace_id,t.bot_id) ORDER BY CASE WHEN m.mode='steer' THEN 0 ELSE 1 END,m.created,m.rowid) AS position,m.*",
            )
            + "m.status IN ('queued','dispatching') AND NOT EXISTS (SELECT 1 FROM messages active WHERE active.task_id=m.task_id AND active.status='running')) WHERE position=1"
        )
        running = db.all(select + "m.status='running' ORDER BY m.created")
        active = [r for r in running if r["task_status"] != "waiting"]
        capacity = max(
            0, db.setting("preferences").get("max_active_runs", 4) - len({r["task_id"] for r in active})
        )
        busy_bots = {r["lane"] for r in running if r["workspace_id"] or r["task_status"] != "waiting"}
        candidates = {}
        for row in queued:
            if row["lane"] not in busy_bots:
                candidates.setdefault(row["lane"], row)
        dispatch = sorted(
            candidates.values(), key=lambda r: (self.dispatch_order.get(r["lane"], 0), r["created"])
        )[:capacity]
        # Steering joins an existing run; it neither opens a new lane nor consumes
        # another concurrency slot. Keep ordinary follow-ups in the durable FIFO.
        steering = db.all(
            select
            + "m.mode='steer' AND m.status IN ('queued','dispatching') AND EXISTS (SELECT 1 FROM messages active WHERE active.task_id=m.task_id AND active.status='running') ORDER BY m.created,m.rowid"
        )
        seen = set()
        for row in steering:
            if row["task_id"] not in seen:
                dispatch.append(row)
                seen.add(row["task_id"])
        for row in dispatch:
            self.dispatch_order[row["lane"]] = now
        polls = [r for r in running if self.polls.get(r["id"], (0, 0, None))[0] <= now]
        live_ids = {r["id"] for r in running}
        self.polls = {key: value for key, value in self.polls.items() if key in live_ids}
        semaphore = asyncio.Semaphore(4)

        async def process_one(row):
            try:
                async with semaphore:
                    await self.process(row)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    try:
                        error = exc.response.json().get("error", "Runtime rejected this request.")
                    except ValueError:
                        error = "Runtime rejected this request."
                    db.execute(
                        "UPDATE messages SET status='failed',error=?,finished=? WHERE id=? AND status NOT IN ('cancelled','completed')",
                        (str(error)[:1000], time.time(), row["id"]),
                    )
                    with db.transaction() as sql:
                        self.settle_task_status(sql, row["task_id"], "failed", str(error)[:1000])
                else:
                    self.ready = False
            except (httpx.TransportError, ValueError, KeyError) as exc:
                self.last_error = type(exc).__name__
                self.ready = False
                db.execute(
                    "UPDATE tasks SET wait_reason='Waiting for the runtime connection. Work will resume automatically.' WHERE id=? AND status IN ('queued','running','waiting')",
                    (row["task_id"],),
                )

        await asyncio.gather(*(process_one(row) for row in [*polls, *dispatch]))

    async def process(self, row):
        db = self.core.db
        if row["status"] in {"queued", "dispatching"}:
            active = db.one(
                "SELECT id FROM messages WHERE task_id=? AND status='running' AND id<>?",
                (row["task_id"], row["id"]),
            )
            if active and row["mode"] != "steer":
                return
            with db.transaction() as sql:
                changed = sql.execute(
                    "UPDATE messages SET status='dispatching' WHERE id=? AND status IN ('queued','dispatching')",
                    (row["id"],),
                ).rowcount
                if not changed:
                    return
                row = {
                    **row,
                    "mode": sql.execute("SELECT mode FROM messages WHERE id=?", (row["id"],)).fetchone()[
                        "mode"
                    ],
                }
                sql.execute(
                    "INSERT OR IGNORE INTO submission_budgets SELECT ?,COALESCE((SELECT MIN(b.baseline) FROM submission_budgets b JOIN messages m ON m.id=b.message_id WHERE m.task_id=t.id AND m.id<>? AND m.status IN ('running','dispatching')),t.tokens) FROM tasks t WHERE t.id=?",
                    (row["id"], row["id"], row["task_id"]),
                )
            try:
                try:
                    hook = await self.core.hooks.run(
                        "before_chat",
                        row["id"],
                        row,
                        {"content": json.loads(row["payload"])["content"], "context": ""},
                    )
                except HookBlocked as exc:
                    db.execute(
                        "UPDATE messages SET status='failed',error=?,finished=? WHERE id=?",
                        (str(exc), time.time(), row["id"]),
                    )
                    with db.transaction() as sql:
                        self.settle_task_status(sql, row["task_id"], "failed", str(exc))
                    return
                result = await self.request(
                    "POST",
                    "/v1/submit",
                    json={
                        "taskId": row["task_id"],
                        "requestId": row["request_id"],
                        "mode": row["mode"],
                        "content": json.loads(row["payload"])["content"],
                        "provider": row["provider"],
                        "model": row["model"],
                        "thinkingLevel": row["thinking_level"],
                        "extensions": json.loads(row["extensions"]),
                        "maxTokens": row["max_tokens"],
                        "instructions": self.core.instructions(row)
                        + ("\n\nPlugin context:\n" + hook["context"] if hook.get("context") else ""),
                    },
                )
                db.execute(
                    "UPDATE messages SET submission_id=?,started=COALESCE(started,?),status=CASE WHEN status='dispatching' THEN 'running' ELSE status END WHERE id=?",
                    (result["submissionId"], result.get("started"), row["id"]),
                )
                db.execute(
                    "UPDATE tasks SET status='running',conversation_id=?,updated=?,error=NULL,wait_reason=NULL WHERE id=? AND status NOT IN ('cancelled','paused','uncertain')",
                    (result["conversationId"], time.time(), row["task_id"]),
                )
                state = db.one("SELECT status FROM tasks WHERE id=?", (row["task_id"],))
                if state["status"] == "cancelled":
                    db.execute("INSERT OR IGNORE INTO pending_aborts VALUES (?)", (row["task_id"],))
            except httpx.HTTPStatusError:
                raise
            return
        result = await self.request("GET", f"/v1/submissions/{row['submission_id']}")
        if result.get("started") is not None:
            db.execute(
                "UPDATE messages SET started=COALESCE(started,?) WHERE id=?", (result["started"], row["id"])
            )
        signature = (result["status"], result.get("tokens", 0), row["task_status"])
        _, delay, previous = self.polls.get(row["id"], (0, 0, None))
        delay = min(5, max(1, delay * 1.5)) if signature == previous else 1
        self.polls[row["id"]] = (time.monotonic() + delay, delay, signature)
        db.record_usage(row["task_id"], result.get("tokens", 0), result.get("cost", 0))
        if result["status"] not in {"done", "unanswered"}:
            waiting = result.get("waiting")
            retrying = db.one(
                "SELECT error FROM actions WHERE task_id=? AND status='retrying'", (row["task_id"],)
            )
            if retrying:
                waiting = retrying["error"]
            approval = db.one("SELECT 1 FROM actions WHERE task_id=? AND status='pending'", (row["task_id"],))
            db.execute(
                "UPDATE tasks SET status=?,wait_reason=? WHERE id=? AND status IN ('running','waiting')",
                ("waiting" if waiting or approval else "running", waiting, row["task_id"]),
            )
        if result["status"] in {"done", "unanswered"}:
            status = "completed" if result["status"] == "done" else "failed"
            answer = result.get("answer", "")
            error = result.get("error")
            await self.core.hooks.notify(
                "after_response",
                f"{row['task_id']}:answer:{result['answerId']}"
                if result.get("answerId") is not None
                else row["id"],
                row,
                {"answer": answer, "error": error, "status": status},
            )
            with db.transaction() as sql:
                changed = sql.execute(
                    "UPDATE messages SET status=?,answer=?,answer_id=?,error=?,finished=? WHERE id=? AND status='running'",
                    (status, answer, result.get("answerId"), error, time.time(), row["id"]),
                ).rowcount
                if not changed:
                    return
                self.settle_task_status(sql, row["task_id"], status, error)
                duplicate_answer = (
                    result.get("answerId") is not None
                    and sql.execute(
                        "SELECT 1 FROM messages WHERE task_id=? AND id<>? AND answer_id=? AND status='completed'",
                        (row["task_id"], row["id"], result["answerId"]),
                    ).fetchone()
                )
                if not duplicate_answer:
                    sql.execute(
                        "INSERT INTO events(kind,title,task_id,created) VALUES (?,?,?,?)",
                        (status, answer[:200] or error or status, row["task_id"], time.time()),
                    )
                    task = sql.execute("SELECT source FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
                    if task["source"].startswith(("job:", "telegram:")):
                        send = True
                        if task["source"].startswith("job:"):
                            job_id = task["source"][4:]
                            job = sql.execute(
                                "SELECT notify,last_result FROM schedules WHERE id=?", (job_id,)
                            ).fetchone()
                            send = bool(job and job["notify"] and job["last_result"] != answer)
                            sql.execute("UPDATE schedules SET last_result=? WHERE id=?", (answer, job_id))
                        recipient = (
                            task["source"].split(":", 1)[1]
                            if task["source"].startswith("telegram:")
                            else self.core.telegram.owner()
                        )
                        if send and recipient:
                            files = sql.execute(
                                "SELECT f.id FROM attachments f JOIN actions a ON a.id=f.action_id "
                                "WHERE f.task_id=? AND a.status='completed' AND f.created>=? ORDER BY f.created,f.id",
                                (row["task_id"], row["created"]),
                            ).fetchall()
                            enqueue(
                                sql,
                                answer or error or status,
                                recipient,
                                row["task_id"],
                                markdown=True,
                                attachments=[file["id"] for file in files],
                            )

            if not result.get("active"):
                try:
                    await self.core.remote(
                        "browser",
                        "/agent/release",
                        {
                            "task_id": row["task_id"],
                            "close": status == "completed"
                            and not db.one(
                                "SELECT 1 FROM messages WHERE task_id=? AND status IN ('queued','dispatching','running')",
                                (row["task_id"],),
                            )
                            and db.setting("preferences").get("browser_close_on_finish", True),
                        },
                    )
                except httpx.HTTPError:
                    pass

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log HTTP request bodies or provider credentials.
                self.ready = False
                self.last_error = type(exc).__name__
                log.warning("Coordinator retry: %s", self.last_error)
            pending = await asyncio.to_thread(
                self.core.db.one,
                "SELECT 1 FROM messages WHERE status IN ('queued','dispatching','running') LIMIT 1",
            )
            delay = 5 if self.last_error or not pending else 1
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except TimeoutError:
                pass
