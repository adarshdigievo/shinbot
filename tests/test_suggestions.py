import asyncio
import time

import httpx


def test_suggestions_require_owner(client):
    assert client.post("/api/prompt-suggestion", json={}).status_code == 401


async def test_context_is_scoped_cached_and_usage_counts(application, monkeypatch):
    core = application.state.core
    first = core.tasks.create("Compare the two routes")["id"]
    core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (first,))
    core.db.execute(
        "UPDATE messages SET status='completed',answer='The train takes 90 minutes.' WHERE task_id=?",
        (first,),
    )
    core.tasks.create("PRIVATE OTHER CONVERSATION")
    calls = []

    async def runtime(method, path, **kwargs):
        calls.append(kwargs["json"])
        await asyncio.sleep(0.01)
        return {"suggestion": "Compare train fares for those routes.", "tokens": 120, "cost": 0.001}

    monkeypatch.setattr(core.coordinator, "request", runtime)
    results = await asyncio.gather(*(core.suggestions.get("default", first) for _ in range(3)))
    assert all(r["suggestion"] == "Compare train fares for those routes." for r in results)
    assert len(calls) == 1
    assert "90 minutes" in calls[0]["context"] and "PRIVATE OTHER" not in calls[0]["context"]
    assert core.db.one("SELECT SUM(tokens) AS used FROM usage_ledger")["used"] == 120
    assert core.tasks.detail(first)["tokens"] == 0
    assert core.db.one("SELECT COUNT(*) AS count FROM tasks")["count"] == 2


async def test_running_failed_tasks_and_exhausted_caps_do_not_generate(application, monkeypatch):
    core = application.state.core
    calls = []

    async def runtime(*args, **kwargs):
        calls.append(kwargs)
        return {"suggestion": "Plan a first task.", "tokens": 0, "cost": 0}

    monkeypatch.setattr(core.coordinator, "request", runtime)
    task = core.tasks.create("hello")["id"]
    for status in ("queued", "running", "waiting", "failed", "cancelled"):
        core.db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
        assert await core.suggestions.get("default", task) == {"suggestion": None}
    core.db.execute("UPDATE tasks SET status='completed',tokens=1000,max_tokens=1000 WHERE id=?", (task,))
    assert await core.suggestions.get("default", task) == {"suggestion": None}
    core.db.set("preferences", {**core.db.setting("preferences"), "daily_tokens": 1000})
    core.db.execute(
        "INSERT INTO usage_ledger(task_id,tokens,cost,created) VALUES (?,1000,0,?)", (task, time.time())
    )
    assert await core.suggestions.get("default") == {"suggestion": None}
    assert calls == []


async def test_unavailable_model_is_quiet_and_does_not_retry_on_every_poll(application, monkeypatch):
    core = application.state.core
    calls = []

    async def runtime(*args, **kwargs):
        calls.append(kwargs)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(core.coordinator, "request", runtime)
    assert await core.suggestions.get("default") == {"suggestion": None}
    assert await core.suggestions.get("default") == {"suggestion": None}
    assert len(calls) == 1
