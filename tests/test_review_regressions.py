import asyncio
import io
import time
import zipfile

import httpx
import pytest

from shinbot.plugins import unpack_archive
from shinbot.services.worker import file_operation


def test_manual_compaction_progress_on_completed_conversation(owner, application, monkeypatch):
    core = application.state.core
    task_id = owner.post("/api/tasks", json={"content": "Hello", "request_id": "compact"}).json()["id"]
    core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
    # Enable runtime requests after startup so the background coordinator stays stopped.
    monkeypatch.setattr(core.config, "coordinate", True)
    compacting = False
    unavailable = False

    async def runtime(method, path, **kwargs):
        nonlocal compacting
        if unavailable:
            raise httpx.ConnectError("Runtime unavailable")
        if method == "POST" and path == f"/v1/tasks/{task_id}/compact":
            compacting = True
            return {"taskId": "compaction-1"}
        assert method == "GET" and path == f"/v1/tasks/{task_id}/progress"
        return {"text": "", "compacting": compacting}

    monkeypatch.setattr(core.coordinator, "request", runtime)
    assert owner.post(f"/api/tasks/{task_id}/compact").status_code == 200
    detail = owner.get(f"/api/tasks/{task_id}").json()
    assert detail["status"] == "completed"
    assert detail["progress"]["compacting"] is True
    compacting = False
    assert owner.get(f"/api/tasks/{task_id}").json()["progress"]["compacting"] is False
    unavailable = True
    response = owner.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json()["id"] == task_id
    assert response.json()["messages"][0]["content"] == "Hello"


def test_notebook_rejects_traversal_and_forgotten_fact(application):
    notebook = application.state.core.notebook
    for path in ["../outside", "x/y", "0" * 31, "/tmp/leak"]:
        with pytest.raises(ValueError):
            notebook.save("Unsafe", "test", path)
        with pytest.raises(ValueError):
            notebook.delete(path)
    saved = notebook.remember("Tea without sugar")
    notebook.forget(saved["id"])
    with pytest.raises(ValueError):
        notebook.remember("tea without sugar", "task:test")
    assert notebook.remember("Tea without sugar")["source"] == "owner"


def test_creation_idempotency_and_body_validation(owner):
    body = {"content": "hello", "request_id": "same-request"}
    first = owner.post("/api/tasks", json=body).json()
    assert owner.post("/api/tasks", json=body).json()["id"] == first["id"]
    assert owner.post("/api/tasks", json={**body, "content": "different"}).status_code == 409
    assert owner.post("/api/notes", json={}).status_code == 400
    assert owner.post("/api/notes", content=iter([b"x" * 1_000_000] * 4)).status_code == 413
    assert owner.post("/api/notes", content=b"{}", headers={"Content-Length": "invalid"}).status_code == 400


def test_file_tools_refuse_symlinks_and_never_truncate_oversized_write(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("private")
    (root / "link").symlink_to(outside)
    (root / "dir").symlink_to(tmp_path, target_is_directory=True)
    for name in ["link", "dir/secret", "../secret"]:
        for tool, args in [("read_file", {}), ("write_file", {"content": "overwrite"})]:
            with pytest.raises((ValueError, OSError)):
                file_operation(root, tool, {"path": name, **args})
    assert outside.read_text() == "private"
    file_operation(root, "write_file", {"path": "nested/saved", "content": "safe"})
    assert file_operation(root, "read_file", {"path": "nested/saved"}) == {"text": "safe"}
    with pytest.raises(ValueError):
        file_operation(root, "write_file", {"path": "nested/saved", "content": "x" * 200001})
    assert (root / "nested/saved").read_text() == "safe"


def test_marketplace_allows_repository_metadata():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("repo/.github/workflows/check.yml", "name: test")
        archive.writestr("repo/.gitignore", "tmp")
        archive.writestr("repo/shinbot-marketplace.json", "{}")
    assert "shinbot-marketplace.json" in unpack_archive(data.getvalue())


def test_job_validation_and_paused_edits(application):
    core = application.state.core
    data = {"name": "Morning", "prompt": "hello", "kind": "interval", "spec": "60", "timezone": "UTC"}
    for patch in [
        {"grants": {"computer": "yes"}},
        {"grants": {"admin": True}},
        {"lateness": -1},
        {"timezone": "not/a/zone"},
    ]:
        with pytest.raises(ValueError):
            core.jobs.save({**data, **patch})
    job = core.jobs.save(data)
    core.jobs.toggle(job["id"], False)
    changed = core.jobs.save({"name": "Evening"}, job["id"])
    assert changed["enabled"] == 0 and changed["created"] == job["created"]
    cron = core.jobs.save({**data, "kind": "cron", "spec": "* * * * *"})
    core.db.execute("UPDATE schedules SET next_run=? WHERE id=?", (time.time() - 2_000_000, cron["id"]))
    assert len(core.jobs.tick()) == 1


async def test_cancel_during_dispatch_cannot_resurrect(application):
    core = application.state.core
    task = core.tasks.create("hello")
    arrived = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def request(method, path, **kwargs):
        calls.append(path)
        if path == "/v1/health":
            return {"held": False}
        if path == "/v1/submit":
            arrived.set()
            await finish.wait()
            return {"submissionId": 1, "conversationId": 1}
        if path.endswith("/abort"):
            raise httpx.ConnectError("not ready")
        return {}

    async def remote(*args, **kwargs):
        return {}

    core.coordinator.request = request
    core.remote = remote
    core.coordinator.ready = True
    run = asyncio.create_task(core.coordinator.tick())
    await arrived.wait()
    await core.cancel(task["id"])
    finish.set()
    await run
    assert core.tasks.detail(task["id"])["status"] == "cancelled"
    assert core.db.one("SELECT * FROM pending_aborts")["task_id"] == task["id"]
    await core.coordinator.tick()
    assert calls.count(f"/v1/tasks/{task['id']}/abort") == 2


async def test_waiting_tasks_do_not_starve_new_messages(application):
    core = application.state.core
    for i in range(16):
        task = core.tasks.create("waiting")
        core.db.execute(
            "UPDATE messages SET status='running',submission_id=? WHERE task_id=?", (i, task["id"])
        )
        core.db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
    new = core.tasks.create("new task")
    dispatched = []

    async def request(method, path, **kwargs):
        if path == "/v1/health":
            return {"held": False}
        if path == "/v1/submit":
            dispatched.append(kwargs["json"]["taskId"])
            return {"submissionId": 100, "conversationId": 100}
        return {"status": "pending"}

    core.coordinator.ready = True
    core.coordinator.request = request
    await core.coordinator.tick()
    assert dispatched == [new["id"]]


def test_usage_ledger_counts_deltas_and_message_budget(owner, application):
    core = application.state.core
    task = core.tasks.create("hello")
    core.db.record_usage(task["id"], 100, 0.01)
    core.db.record_usage(task["id"], 100, 0.01)
    core.db.record_usage(task["id"], 150, 0.02)
    assert core.db.one("SELECT SUM(tokens) AS tokens FROM usage_ledger")["tokens"] == 150
    message = core.db.one("SELECT id FROM messages WHERE task_id=?", (task["id"],))
    core.db.execute("UPDATE messages SET status='running' WHERE id=?", (message["id"],))
    core.db.execute("INSERT INTO submission_budgets VALUES (?,?)", (message["id"], 150))
    response = owner.post(
        "/internal/admit",
        json={"task_id": task["id"], "tokens": 200, "cost": 0.03},
        headers={"Authorization": "Bearer " + "r" * 40},
    )
    assert response.json()["allowed"]
    assert core.db.one("SELECT SUM(tokens) AS tokens FROM usage_ledger")["tokens"] == 200


def test_approval_policy(application):
    core = application.state.core
    assert core.approval_required("browser", {"operation": "goto"}, {})
    assert not core.approval_required("browser", {"operation": "goto"}, {"browser": True})
    assert core.approval_required("memory", {"operation": "remember"}, {"computer": True})
    assert core.approval_required("jobs", {"operation": "create"}, {"computer": True})
