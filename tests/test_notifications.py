import time

from shinbot.db import Database, packed


def event(core, kind="completed", title="Task result"):
    task = core.tasks.create("Do the work", title=title)
    core.db.execute("UPDATE tasks SET status=? WHERE id=?", (kind, task["id"]))
    core.db.event(kind, "A result that need not be copied into the header", task["id"])
    return task["id"]


def action(core, status="pending", expires=None):
    task = core.tasks.create("Check the deployment", title="Release review")
    core.db.execute(
        "INSERT INTO actions(id,task_id,epoch,tool,args,hash,status,created,expires) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task["id"],
            task["id"],
            "epoch",
            "run_command",
            '{"private":"argument"}',
            "hash",
            status,
            time.time(),
            expires if expires is not None else time.time() + 60,
        ),
    )
    return task["id"]


def acknowledge(owner, items):
    return owner.post(
        "/api/notifications/read",
        json={"items": [{"id": item["id"], "revision": item["revision"]} for item in items]},
    )


def test_notifications_require_auth_and_validate_writes(client, application):
    assert client.get("/api/notifications").status_code == 401
    assert client.post("/api/notifications/read", json={"items": []}).status_code == 401


def test_notification_read_persists_and_does_not_hide_new_events(owner, application):
    core = application.state.core
    task = event(core)
    before = owner.get("/api/notifications").json()
    assert before["unread"] == 1
    assert owner.get("/api/notifications").json()["unread"] == 1
    item = before["items"][0]
    assert item["task_id"] == task
    assert item["title"] == "Task result"
    core.db.event("completed", "A new result", task)
    assert acknowledge(owner, before["items"]).json()["unread"] == 1
    assert len(Database(core.db.path).all("SELECT * FROM notification_reads")) == 1
    assert acknowledge(owner, owner.get("/api/notifications").json()["items"]).json()["unread"] == 0
    core.db.event("failed", "Follow-up failed", task)
    assert owner.get("/api/notifications").json()["unread"] == 1
    assert (
        owner.post(
            "/api/notifications/read", json={"items": [{"id": "invented", "revision": "1"}]}
        ).status_code
        == 200
    )
    assert not core.db.one("SELECT 1 FROM notification_reads WHERE id='invented'")
    assert owner.post("/api/notifications/read", json={"items": [{}]}).status_code == 422
    assert (
        owner.post(
            "/api/notifications/read", json={"items": [{"id": "e", "revision": "1"}] * 101}
        ).status_code
        == 422
    )


def test_pending_actions_expire_and_reading_never_approves(owner, application):
    core = application.state.core
    task = action(core)
    action(core, expires=time.time() - 1)
    data = owner.get("/api/notifications").json()
    assert data["unread"] == 1
    assert "private" not in str(data)
    assert data["items"][0]["detail"].startswith("Approval needed")
    assert acknowledge(owner, data["items"]).json()["unread"] == 0
    assert core.db.one("SELECT status FROM actions WHERE id=?", (task,))["status"] == "pending"
    core.db.execute("UPDATE actions SET status='uncertain' WHERE id=?", (task,))
    assert acknowledge(owner, data["items"]).json()["unread"] == 1
    assert owner.get("/api/notifications").json()["items"][0]["detail"].startswith("Action needs review")
    core.db.execute("UPDATE actions SET status='completed' WHERE id=?", (task,))
    assert owner.get("/api/notifications").json()["items"] == []


def test_github_notification_scope_snooze_revision_and_plugin(owner, application):
    core = application.state.core
    core.plugins.stage_from_catalog("official", "projects", "1.1.0")
    core.plugins.activate("projects", "1.1.0")
    core.db.execute("DELETE FROM events")
    core.inbox.configuration({"repositories": ["demo/app"], "token": "fixture-token"})
    item = core.inbox.normalize(
        {"number": 1, "title": "Review login", "updated_at": "2026-10-26T10:00:00Z"},
        "demo/app",
        "owner",
        ["mention"],
    )
    core.db.execute(
        "INSERT INTO github_items VALUES (?,?,?,?,?)",
        (item["id"], item["repo"], 1, packed(item), time.time()),
    )
    data = owner.get("/api/notifications").json()
    assert data["unread"] == 1
    assert data["items"][0]["item_id"] == item["id"]
    core.inbox.state(item["id"], {"seen": False, "snoozed_until": time.time() + 60})
    assert owner.get("/api/notifications").json()["unread"] == 0
    core.inbox.state(item["id"], {"seen": False, "snoozed_until": 0})
    newer = {**item, "updated_at": "2026-10-26T11:00:00Z"}
    core.db.execute("UPDATE github_items SET payload=? WHERE id=?", (packed(newer), item["id"]))
    assert acknowledge(owner, data["items"]).json()["unread"] == 1
    assert core.inbox.snapshot("new")["counts"]["new"] == 1
    current = owner.get("/api/notifications").json()
    assert acknowledge(owner, current["items"]).json()["unread"] == 0
    assert core.inbox.snapshot("new")["counts"]["new"] == 0
    assert core.inbox.snapshot()["counts"]["attention"] == 1
    core.db.execute("DELETE FROM github_item_state")
    core.db.execute("DELETE FROM notification_reads")
    core.db.execute("UPDATE plugins SET status='disabled' WHERE id='projects'")
    assert owner.get("/api/notifications").json()["unread"] == 0
    core.db.execute("UPDATE plugins SET status='active' WHERE id='projects'")
    core.db.set("github-inbox-scopes", {"repositories": ["elsewhere/app"], "organizations": []})
    assert owner.get("/api/notifications").json()["unread"] == 0


def test_muted_jobs_and_automatic_service_waits_do_not_notify(owner, application):
    core = application.state.core
    job = core.jobs.save(
        {
            "name": "Quiet check",
            "prompt": "check",
            "kind": "interval",
            "spec": "60",
            "timezone": "UTC",
            "notify": False,
        }
    )
    task = event(core)
    core.db.execute("UPDATE tasks SET source=? WHERE id=?", ("job:" + job["id"], task))
    waiting = core.tasks.create("Automatic retry")
    core.db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (waiting["id"],))
    assert owner.get("/api/notifications").json()["unread"] == 0
    core.db.event("failed", "Job failed", task)
    assert owner.get("/api/notifications").json()["unread"] == 1


def test_mark_shown_read_keeps_notifications_beyond_first_page(owner, application):
    core = application.state.core
    for number in range(105):
        core.db.event("warning", f"Notice {number}")
    first = owner.get("/api/notifications").json()
    assert first["unread"] == 105
    assert len(first["items"]) == 100 and first["has_more"]
    next_page = acknowledge(owner, first["items"]).json()
    assert next_page["unread"] == 5
    assert sum(item["unread"] for item in next_page["items"]) == 5
