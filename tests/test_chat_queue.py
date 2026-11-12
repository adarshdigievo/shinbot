import time

import pytest
from test_coordinator import Runtime


async def test_fifo_steering_and_recovery_keep_one_lane_and_one_budget(application, monkeypatch):
    core = application.state.core
    runtime = Runtime()
    payloads = []

    async def request(method, path, **kwargs):
        if path == "/v1/submit":
            payloads.append(kwargs["json"])
        return await runtime(method, path, **kwargs)

    async def remote(*args, **kwargs):
        return {}

    monkeypatch.setattr(core.coordinator, "request", request)
    monkeypatch.setattr(core, "remote", remote)
    core.db.set("preferences", {**core.db.setting("preferences"), "max_active_runs": 1})
    task = core.tasks.create("First")["id"]
    await core.coordinator.tick()
    core.db.record_usage(task, 900, 0)
    second = core.tasks.message(task, "Second", "second")
    core.tasks.message(task, "Third", "third")
    core.tasks.message(task, "Change direction", "redirect", mode="steer")
    await core.coordinator.tick()
    assert [p["content"] for p in payloads] == ["First", "Change direction"]
    assert payloads[-1]["mode"] == "steer"
    assert core.db.all("SELECT DISTINCT baseline FROM submission_budgets") == [{"baseline": 0}]
    # Reopening the coordinator does not submit an acknowledged steer a second time.
    from shinbot.coordinator import Coordinator

    core.coordinator = Coordinator(core)
    monkeypatch.setattr(core.coordinator, "request", request)
    await core.coordinator.tick()
    assert len(payloads) == 2
    runtime.submissions.update({1: "done", 2: "done"})
    core.coordinator.polls.clear()
    await core.coordinator.tick()
    assert core.tasks.detail(task)["status"] == "queued"
    await core.coordinator.tick()
    assert [p["content"] for p in payloads] == ["First", "Change direction", "Second"]
    assert core.db.one("SELECT status FROM messages WHERE id=?", (second["id"],))["status"] == "running"
    assert core.db.one("SELECT COUNT(*) AS n FROM messages WHERE status='queued'")["n"] == 1


def test_steering_is_idempotent_scoped_and_cannot_change_dispatched_messages(owner, application):
    core = application.state.core
    task = core.tasks.create("First")["id"]
    message = core.tasks.message(task, "Second", "second")
    route = f"/api/tasks/{task}/messages/{message['id']}/steer"
    assert owner.post(route).status_code == 200
    assert owner.post(route).status_code == 200
    assert core.tasks.message(task, "Second", "second")["mode"] == "steer"
    assert len(core.tasks.detail(task)["messages"]) == 2
    other = core.tasks.create("Another chat")["id"]
    assert owner.post(f"/api/tasks/{other}/messages/{message['id']}/steer").status_code == 400
    plain = core.tasks.message(task, "Already starting", "plain")
    core.db.execute("UPDATE messages SET status='dispatching' WHERE id=?", (plain["id"],))
    assert owner.post(f"/api/tasks/{task}/messages/{plain['id']}/steer").status_code == 400
    result = owner.post(
        f"/api/tasks/{task}/messages", json={"content": "Redirect", "request_id": "steer", "mode": "steer"}
    )
    assert result.status_code == 200
    assert (
        owner.post(
            f"/api/tasks/{task}/messages",
            json={"content": "Redirect", "request_id": "steer", "mode": "steer"},
        ).status_code
        == 200
    )
    assert (
        owner.post(
            f"/api/tasks/{task}/messages",
            json={"content": "Redirect", "request_id": "steer", "mode": "queue"},
        ).status_code
        == 400
    )
    assert (
        owner.post(f"/api/tasks/{task}/messages", json={"content": "Bad mode", "mode": "invalid"}).status_code
        == 422
    )
    core.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task,))
    with pytest.raises(ValueError, match="resume"):
        core.tasks.steer(task, message["id"])


def test_steer_cannot_reset_running_message_budget(application, client):
    core = application.state.core
    task = core.tasks.create("First")["id"]
    first = core.tasks.detail(task)["messages"][0]
    second = core.tasks.message(task, "Redirect", "steer", mode="steer")
    core.db.execute("UPDATE tasks SET max_tokens=1000,tokens=900 WHERE id=?", (task,))
    core.db.execute("UPDATE messages SET status='running',started=? WHERE task_id=?", (time.time(), task))
    for message in (first, second):
        core.db.execute("INSERT INTO submission_budgets VALUES (?,0)", (message["id"],))
    response = client.post(
        "/internal/admit",
        json={"task_id": task, "tokens": 1100, "cost": 0},
        headers={"Authorization": "Bearer " + core.config.service_token, "Host": "control:8765"},
    )
    assert response.is_success and response.json()["allowed"] is False


async def test_rejected_steer_does_not_mark_the_active_response_failed(application, monkeypatch):
    import httpx

    core = application.state.core
    runtime = Runtime()

    async def request(method, path, **kwargs):
        if path == "/v1/submit" and kwargs["json"]["mode"] == "steer":
            response = httpx.Response(
                400,
                json={"error": "Rejected redirect"},
                request=httpx.Request("POST", "http://runtime/v1/submit"),
            )
            raise httpx.HTTPStatusError("Rejected", request=response.request, response=response)
        return await runtime(method, path, **kwargs)

    monkeypatch.setattr(core.coordinator, "request", request)
    task = core.tasks.create("First")["id"]
    await core.coordinator.tick()
    core.tasks.message(task, "Redirect", "steer", mode="steer")
    await core.coordinator.tick()
    result = core.tasks.detail(task)
    assert result["status"] == "running" and result["error"] is None
    assert result["messages"][1]["status"] == "failed"
    assert result["messages"][0]["status"] == "running"


async def test_steering_keeps_browser_lease_and_emits_one_shared_reply_event(application, monkeypatch):
    core = application.state.core
    task = core.tasks.create("First")["id"]
    core.tasks.message(task, "Redirect", "redirect", mode="steer")
    core.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
    core.db.execute("UPDATE messages SET status='running',submission_id=1 WHERE task_id=?", (task,))
    active = True
    released = []

    async def request(*args, **kwargs):
        return {
            "status": "done",
            "answer": "Shared reply",
            "answerId": 10,
            "active": active,
            "tokens": 100,
            "cost": 0,
        }

    async def remote(*args, **kwargs):
        released.append(args)
        return {}

    monkeypatch.setattr(core.coordinator, "request", request)
    monkeypatch.setattr(core, "remote", remote)
    rows = core.db.all(
        "SELECT m.*,t.extensions,t.bot_id,t.status AS task_status FROM messages m JOIN tasks t ON t.id=m.task_id WHERE m.task_id=? ORDER BY m.created",
        (task,),
    )
    await core.coordinator.process(rows[0])
    assert not released
    active = False
    await core.coordinator.process(rows[1])
    assert len(released) == 1
    assert (
        core.db.one("SELECT COUNT(*) AS n FROM events WHERE task_id=? AND kind='completed'", (task,))["n"]
        == 1
    )
    assert core.tasks.detail(task)["status"] == "completed"
    assert core.db.one("SELECT SUM(tokens) AS n FROM usage_ledger")["n"] == 100
