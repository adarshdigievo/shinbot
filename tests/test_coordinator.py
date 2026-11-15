import asyncio
import json
import time

from shinbot.db import packed


class Runtime:
    def __init__(self):
        self.calls = []
        self.submissions = {}
        self.active = 0
        self.peak = 0

    async def __call__(self, method, path, **kwargs):
        self.calls.append(path)
        if path == "/v1/health":
            return {"held": False}
        if path == "/v1/submit":
            key = len(self.submissions) + 1
            self.submissions[key] = "running"
            return {"conversationId": key, "submissionId": key}
        if path.startswith("/v1/submissions/"):
            self.active += 1
            self.peak = max(self.active, self.peak)
            await asyncio.sleep(0.02)
            self.active -= 1
            key = int(path.rsplit("/", 1)[1])
            return {"status": self.submissions[key], "tokens": 0, "cost": 0, "answer": "done"}
        return {}


async def test_reply_completion_time_survives_reload_and_repeated_poll(application, monkeypatch):
    from shinbot.db import Database
    from shinbot.tasks import Tasks

    core = application.state.core
    fake = Runtime()
    monkeypatch.setattr(core.coordinator, "request", fake)

    async def remote(*args, **kwargs):
        return {}

    monkeypatch.setattr(core, "remote", remote)
    task = core.tasks.create("First turn")
    await core.coordinator.tick()
    core.tasks.message(task["id"], "Queued follow-up", "follow-up")
    fake.submissions[1] = "done"
    await core.coordinator.tick()
    detail = core.tasks.detail(task["id"])
    first, followup = detail["messages"]
    assert first["finished"] >= followup["created"]
    assert first["answer"] == "done" and followup["finished"] is None
    reloaded = Tasks(Database(core.db.path), core.plugins, core.bots).detail(task["id"])
    assert reloaded["messages"][0]["finished"] == first["finished"]
    await core.coordinator.tick()
    assert core.tasks.detail(task["id"])["messages"][0]["finished"] == first["finished"]


def test_legacy_reply_uses_completion_event_even_with_queued_followup(application):
    core = application.state.core
    task = core.tasks.create("First turn")
    first = core.tasks.detail(task["id"])["messages"][0]
    second = core.tasks.message(task["id"], "Follow-up", "follow-up")
    core.db.execute(
        "UPDATE messages SET status='completed',answer='Same reply' WHERE task_id=?", (task["id"],)
    )
    stamp = time.time()
    core.db.execute(
        "INSERT INTO events(kind,title,task_id,created) VALUES ('completed','Same reply',?,?)",
        (task["id"], stamp),
    )
    core.db.execute(
        "INSERT INTO events(kind,title,task_id,created) VALUES ('completed','Same reply',?,?)",
        (task["id"], stamp + 10),
    )
    detail = core.tasks.detail(task["id"])
    assert [(m["id"], m["finished"]) for m in detail["messages"]] == [
        (first["id"], stamp),
        (second["id"], stamp + 10),
    ]
    assert all(
        m["finished"] is None
        for m in core.db.all("SELECT finished FROM messages WHERE task_id=?", (task["id"],))
    )


async def test_global_capacity_and_fair_bot_admission(application, monkeypatch):
    core = application.state.core
    fake = Runtime()
    monkeypatch.setattr(core.coordinator, "request", fake)

    async def remote(*args, **kwargs):
        return {}

    monkeypatch.setattr(core, "remote", remote)
    bots = [core.bots.save({"name": f"Worker {i}"})["id"] for i in range(6)]
    for bot in bots:
        for i in range(5):
            core.tasks.create(f"Work {i}", bot_id=bot)
    for _ in range(3):
        await core.coordinator.tick()
    running = core.db.all(
        "SELECT t.bot_id,m.submission_id FROM messages m JOIN tasks t ON t.id=m.task_id WHERE m.status='running'"
    )
    assert len(running) == 4 and len({r["bot_id"] for r in running}) == 4
    first_bots = {r["bot_id"] for r in running}
    for row in running:
        fake.submissions[row["submission_id"]] = "done"
    core.coordinator.polls.clear()
    await core.coordinator.tick()
    await core.coordinator.tick()
    next_bots = {
        r["bot_id"]
        for r in core.db.all(
            "SELECT t.bot_id FROM messages m JOIN tasks t ON t.id=m.task_id WHERE m.status='running'"
        )
    }
    assert set(bots) - first_bots <= next_bots
    assert fake.peak <= 4


async def test_unchanged_submission_polls_back_off_and_pool_is_bounded(application, monkeypatch):
    core = application.state.core
    fake = Runtime()
    monkeypatch.setattr(core.coordinator, "request", fake)
    core.tasks.create("Long run")
    await core.coordinator.tick()
    await core.coordinator.tick()
    count = len(fake.calls)
    for _ in range(5):
        await core.coordinator.tick()
    assert len(fake.calls) == count
    row = core.db.one("SELECT * FROM messages WHERE status='running'")
    due, delay, state = core.coordinator.polls[row["id"]]
    core.coordinator.polls[row["id"]] = (0, delay, state)
    await core.coordinator.tick()
    assert core.coordinator.polls[row["id"]][1] > delay


def test_overlap_creates_one_actionable_event_per_streak(application):
    core = application.state.core
    job = core.jobs.save(
        {
            "name": "Check news",
            "prompt": "Look for updates",
            "kind": "interval",
            "spec": "60",
            "timezone": "UTC",
        }
    )
    task = core.jobs.tick(job["next_run"] + 1)[0]
    core.db.execute("UPDATE tasks SET status='uncertain' WHERE id=?", (task,))
    for i in range(1, 4):
        core.jobs.tick(job["next_run"] + 1 + i * 60)
    events = core.db.all("SELECT * FROM events WHERE kind='warning'")
    assert len(events) == 1 and events[0]["task_id"] == task and "uncertain" in events[0]["title"]


def test_chat_polling_does_not_copy_images(application):
    core = application.state.core
    task = core.tasks.create("Browse")
    result = {
        "content": [
            {"type": "image", "data": "A" * 1000000, "mimeType": "image/jpeg"},
            {"type": "text", "text": "Visible page"},
        ]
    }
    core.db.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "shot",
            task["id"],
            "epoch",
            "browser",
            "{}",
            "hash",
            "completed",
            packed(result),
            None,
            time.time(),
            time.time() + 60,
        ),
    )
    detail = core.tasks.detail(task["id"])
    assert len(json.dumps(detail)) < 10000 and "Visible page" in detail["actions"][0]["result"]
    assert len(core.db.one("SELECT result FROM actions WHERE id='shot'")["result"]) > 1000000


def test_card_token_limit_is_enforced_at_generation_boundary(application, client):
    core = application.state.core
    core.plugins.stage_from_catalog("official", "kanban", "1.0.0")
    core.plugins.activate("kanban", "1.0.0")
    card = core.board.save({"title": "Budget", "goal": "Stay within this goal budget", "max_tokens": 2000})
    core.board.move(card["id"], "ready", card["revision"])
    core.board.tick()
    task_id = core.board.get(card["id"])["task_id"]
    core.db.execute("UPDATE messages SET status='running' WHERE task_id=?", (task_id,))
    core.db.execute("INSERT INTO submission_budgets SELECT id,0 FROM messages WHERE task_id=?", (task_id,))
    result = client.post(
        "/internal/admit",
        json={"task_id": task_id, "tokens": 2500, "cost": 0},
        headers={"Authorization": "Bearer " + core.config.service_token, "Host": "control:8765"},
    )
    assert result.status_code == 200 and not result.json()["allowed"]


async def test_pause_collects_final_usage_before_readmitting_card(application, monkeypatch):
    core = application.state.core
    core.plugins.stage_from_catalog("official", "kanban", "1.0.0")
    core.plugins.activate("kanban", "1.0.0")
    card = core.board.save({"title": "Budget", "goal": "Finish this goal", "max_tokens": 20000})
    core.board.move(card["id"], "ready", card["revision"])
    core.board.tick()
    card = core.board.get(card["id"])
    task_id = card["task_id"]
    core.db.execute("UPDATE messages SET status='running',submission_id=1 WHERE task_id=?", (task_id,))
    core.db.record_usage(task_id, 1000, 0)
    core.board.move(card["id"], "blocked", card["revision"])
    paused = core.board.get(card["id"])
    core.board.move(card["id"], "ready", paused["revision"])
    core.board.tick()
    assert core.board.get(card["id"])["runs"] == 1

    async def request(method, path, **kwargs):
        if path == "/v1/health":
            return {"held": False}
        if path.startswith("/v1/submissions/"):
            return {"status": "unanswered", "tokens": 40000, "cost": 0}
        return {}

    monkeypatch.setattr(core.coordinator, "request", request)
    await core.coordinator.tick()
    await core.coordinator.tick()
    after = core.board.get(card["id"])
    assert after["tokens"] == 40000 and after["status"] == "blocked" and after["runs"] == 1
    assert core.db.one("SELECT SUM(tokens) AS total FROM usage_ledger")["total"] == 40000


async def test_cancellation_during_submit_preserves_submission_and_final_usage(application, monkeypatch):
    core = application.state.core
    task = core.tasks.create("Start work")

    async def remote(*args, **kwargs):
        return {}

    monkeypatch.setattr(core, "remote", remote)

    async def request(method, path, **kwargs):
        if path == "/v1/health":
            return {"held": False}
        if path == "/v1/submit":
            await core.cancel(task["id"])
            return {"conversationId": 1, "submissionId": 101}
        if path == "/v1/submissions/101":
            return {"status": "unanswered", "tokens": 30000, "cost": 0}
        return {}

    monkeypatch.setattr(core.coordinator, "request", request)
    await core.coordinator.tick()
    row = core.db.one("SELECT * FROM messages WHERE task_id=?", (task["id"],))
    assert row["status"] == "cancelled" and row["submission_id"] == 101
    await core.coordinator.tick()
    assert core.db.one("SELECT tokens FROM tasks WHERE id=?", (task["id"],))["tokens"] == 30000
    assert not core.db.all("SELECT * FROM pending_aborts")


async def test_internal_runtime_errors_are_retried_then_failed_while_holds_wait(application, monkeypatch):
    import httpx

    core = application.state.core
    status = {"code": 500}

    async def failing(method, path, **kwargs):
        if path == "/v1/health":
            return {"held": False}
        request = httpx.Request(method, "http://runtime" + path)
        response = httpx.Response(status["code"], json={"error": "database is locked"}, request=request)
        raise httpx.HTTPStatusError("runtime failed", request=request, response=response)

    async def reconciled():
        core.coordinator.ready = True

    monkeypatch.setattr(core.coordinator, "request", failing)
    monkeypatch.setattr(core.coordinator, "reconcile", reconciled)
    task = core.tasks.create("Summarise the report")
    core.coordinator.ready = True
    await core.coordinator.tick()
    message = core.db.one("SELECT * FROM messages")
    assert message["status"] != "failed" and not core.coordinator.ready
    assert core.coordinator.last_error == "HTTPStatusError"
    # A runtime that is held or restarting answers 503; that is waited out however long it takes.
    status["code"] = 503
    core.coordinator.runtime_errors[message["id"]] = time.monotonic() - 3600
    core.coordinator.ready = True
    await core.coordinator.tick()
    assert core.db.one("SELECT status FROM messages")["status"] != "failed"
    status["code"] = 500
    core.coordinator.ready = True
    await core.coordinator.tick()
    failed = core.db.one("SELECT status,error FROM messages")
    assert failed["status"] == "failed" and "kept failing" in failed["error"]
    assert core.tasks.detail(task["id"])["status"] == "failed"
    assert message["id"] not in core.coordinator.runtime_errors


async def test_stuck_accepted_work_is_stopped_in_the_runtime_too(application, monkeypatch):
    import httpx

    core = application.state.core
    fake, broken = Runtime(), {"on": False}

    async def request(method, path, **kwargs):
        if broken["on"] and path.startswith("/v1/submissions/"):
            call = httpx.Request(method, "http://runtime" + path)
            raise httpx.HTTPStatusError(
                "runtime failed", request=call, response=httpx.Response(500, json={"error": "x"}, request=call)
            )
        return await fake(method, path, **kwargs)

    async def reconciled():
        core.coordinator.ready = True

    monkeypatch.setattr(core.coordinator, "request", request)
    monkeypatch.setattr(core.coordinator, "reconcile", reconciled)
    task = core.tasks.create("Long job")
    await core.coordinator.tick()
    message = core.db.one("SELECT * FROM messages")
    assert message["status"] == "running" and message["submission_id"] == 1
    broken["on"] = True
    core.coordinator.polls.clear()
    core.coordinator.runtime_errors[message["id"]] = time.monotonic() - 3600
    await core.coordinator.tick()
    assert core.db.one("SELECT status FROM messages")["status"] == "failed"
    assert core.db.all("SELECT task_id FROM pending_aborts") == [{"task_id": task["id"]}]


async def test_a_queued_follow_up_does_not_resend_the_previous_replys_files(application, monkeypatch):
    core = application.state.core
    fake = Runtime()
    monkeypatch.setattr(core.coordinator, "request", fake)

    async def remote(*args, **kwargs):
        return {}

    monkeypatch.setattr(core, "remote", remote)
    core.telegram.register(4242, 555)
    core.telegram.status("connected")
    task_id = core.telegram.inbound(1, "Make the chart")["task_id"]
    await core.coordinator.tick()
    core.telegram.inbound(2, "Then summarise it")
    # The first reply attaches its file after the follow-up was queued.
    saved = core.attachments.files.put(b"a,b\n1,2\n", "chart.csv")
    core.db.execute(
        "INSERT INTO actions VALUES ('act',?,?,?,?,?,'completed',NULL,NULL,?,?)",
        (task_id, "e", "attach_file", "{}", "h", time.time(), time.time() + 900),
    )
    core.db.execute(
        "INSERT INTO attachments VALUES ('file','act',?,?,?,?,?,0,?)",
        (task_id, saved["digest"], "chart.csv", saved["mime_type"], saved["size"], time.time()),
    )
    fake.submissions[1] = "done"
    await core.coordinator.tick()
    await core.coordinator.tick()
    fake.submissions[2] = "done"
    core.coordinator.polls.clear()
    await core.coordinator.tick()
    answers = [
        json.loads(row["body"])
        for row in core.db.all("SELECT body FROM outbox ORDER BY created,rowid")
        if json.loads(row["body"]).get("markdown")
    ]
    assert [answer["attachments"] for answer in answers] == [["file"], []]
