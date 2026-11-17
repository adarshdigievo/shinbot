"""Regressions for the gateway review: approvals, sign-in limits, cancellation, the coordinator,
retention, daylight-saving schedules, request validation, settings and password changes."""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from test_actions import action
from test_plugins import skill
from typer.testing import CliRunner

from shinbot import cli, maintenance, plugins, retention
from shinbot.approvals import guarded_action, rule_for
from shinbot.config import Config
from shinbot.db import Database, uid
from shinbot.jobs import cron_run, next_run

PASSWORD = "test-password-with-length"
NEW_YORK = "America/New_York"
DAY = 86400


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def login(client, password):
    return client.post("/auth/login", json={"password": password, "cli": True})


async def quiet_remote(service, path, body=None, **kwargs):
    return {"lease": None}


# 1. Ask mode is never weaker than Auto.

GUARDED = [
    ("write_file", {"path": "run.sh", "content": "echo changed"}),
    ("run_command", {"command": "rm -rf build"}),
    ("jobs", {"operation": "pause", "id": "job"}),
    ("notes", {"operation": "delete", "id": "0" * 32}),
    ("desktop", {"operation": "click", "x": 1, "y": 1}),
]


@pytest.mark.parametrize("mode", ["auto", "ask"])
@pytest.mark.parametrize("tool,args", GUARDED)
async def test_guarded_actions_need_a_separate_decision_in_auto_and_ask(
    application, monkeypatch, tool, args, mode
):
    core = application.state.core
    monkeypatch.setattr(core, "remote", quiet_remote)
    assert guarded_action(tool, args)
    # Neither the widest grants nor a saved rule may replace the one-time decision.
    grants = {"computer": True, "browser": True, "extensions": True, "memory": True}
    task = core.tasks.create("Work", grants=grants, options={"approval_mode": mode})
    key, label = rule_for(tool, args)
    core.db.execute(
        "INSERT INTO approval_rules VALUES (?,?,?,?,?,?,?)",
        (uid(), "always", "", key, label, time.time(), ""),
    )
    payload = action(core, task, tool, args)
    assert (await core.action(payload))["status"] == "pending"
    assert not core.running_actions
    review = core.db.one("SELECT once_only FROM action_reviews WHERE action_id=?", (payload["id"],))
    assert review["once_only"] == 1
    with pytest.raises(ValueError, match="separate decision"):
        core.approvals.decide(payload["id"], True, "always")


async def test_full_access_still_runs_guarded_actions_without_asking(application, monkeypatch):
    core = application.state.core
    monkeypatch.setattr(core, "remote", quiet_remote)
    task = core.tasks.create("Work", options={"approval_mode": "full"})
    payload = action(core, task, "write_file", {"path": "a.txt", "content": "b"})
    assert (await core.action(payload))["status"] == "executing"
    await asyncio.gather(*core.running_actions.values())


# 2. Sign-in limits count failures only.


def test_failed_sign_ins_are_limited_but_never_refuse_the_correct_password(owner, application):
    application.state.core.auth.penalty = 0
    owner.headers.pop("Authorization")
    assert [login(owner, PASSWORD).status_code for _ in range(12)] == [200] * 12
    assert [login(owner, "a-wrong-password-guess").status_code for _ in range(10)] == [401] * 8 + [429] * 2
    assert login(owner, PASSWORD).status_code == 200


async def test_guesses_past_the_limit_take_turns_and_a_flood_is_refused(application):
    auth = application.state.core.auth
    auth.penalty = 0.05
    auth.attempts["peer"] = [time.time()] * 8

    async def guess():
        async with auth.attempt("peer"):
            with pytest.raises(HTTPException) as error:
                await auth.failed("peer", 401, "Incorrect password.")
            return error.value.status_code

    started = time.monotonic()
    assert await asyncio.gather(*(guess() for _ in range(4))) == [429] * 4
    assert time.monotonic() - started >= 0.18

    async def hold():
        async with auth.attempt("peer"):
            await asyncio.sleep(0.01)

    waiting = [asyncio.create_task(hold()) for _ in range(12)]
    await asyncio.sleep(0)
    with pytest.raises(HTTPException) as error:
        async with auth.attempt("peer"):
            pass
    assert error.value.status_code == 429
    await asyncio.gather(*waiting)
    assert not auth.pending and not auth.turns


def test_trusted_proxy_setting_chooses_the_client_address(application, monkeypatch):
    auth = application.state.core.auth
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.1, 203.0.113.7, 10.0.0.2"},
        client=SimpleNamespace(host="172.18.0.1"),
    )
    assert auth.client(request) == "172.18.0.1"
    auth.config.trusted_proxies = 1
    assert auth.client(request) == "10.0.0.2"
    auth.config.trusted_proxies = 2
    assert auth.client(request) == "203.0.113.7"
    auth.config.trusted_proxies = 5
    assert auth.client(request) == "172.18.0.1"
    monkeypatch.setenv("SHINBOT_TRUSTED_PROXIES", "1")
    assert Config().trusted_proxies == 1
    monkeypatch.setenv("SHINBOT_TRUSTED_PROXIES", "all")
    assert Config().trusted_proxies == 0


def test_clients_behind_a_trusted_proxy_have_separate_limits(owner, application):
    core = application.state.core
    core.auth.penalty = 0
    owner.headers.pop("Authorization")

    def guess(address):
        return owner.post(
            "/auth/login", json={"password": "a-wrong-password-guess"}, headers={"X-Forwarded-For": address}
        ).status_code

    core.config.trusted_proxies = 1
    assert [guess("203.0.113.9") for _ in range(9)] == [401] * 8 + [429]
    assert guess("198.51.100.4") == 401
    # Without the setting the header is the client's own claim and is ignored.
    core.config.trusted_proxies = 0
    core.auth.attempts.clear()
    assert [guess(f"203.0.113.{n}") for n in range(9)] == [401] * 8 + [429]


# 3. Stop.


def test_stop_leaves_finished_chats_alone_and_unknown_tasks_are_not_found(owner, application, monkeypatch):
    core = application.state.core
    monkeypatch.setattr(core, "remote", quiet_remote)

    async def aborted(task_id):
        return True

    monkeypatch.setattr(core.coordinator, "abort_task", aborted)
    task = owner.post("/api/tasks", json={"content": "hello"}).json()["id"]
    core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task,))
    core.db.execute("UPDATE messages SET status='completed',answer='done' WHERE task_id=?", (task,))
    assert owner.post(f"/api/tasks/{task}/cancel", json={}).json() == {"cancelled": False}
    assert core.db.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert owner.post("/api/tasks/missing/cancel", json={}).status_code == 404
    assert core.db.all("SELECT * FROM pending_aborts") == []
    followup = {"content": "one more thing", "request_id": "next"}
    assert owner.post(f"/api/tasks/{task}/messages", json=followup).status_code == 200
    assert owner.post(f"/api/tasks/{task}/cancel", json={}).json() == {"cancelled": True}
    assert core.db.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "cancelled"
    stopped = owner.post(f"/api/tasks/{task}/messages", json={"content": "again", "request_id": "later"})
    assert stopped.status_code == 409 and "was stopped. Start a new one" in stopped.json()["detail"]
    # Repeating Stop retries the service cancellation without changing anything else.
    assert owner.post(f"/api/tasks/{task}/cancel", json={}).json() == {"cancelled": True}


# 4. Coordinator.


def runtime(calls):
    async def request(method, path, **kwargs):
        calls.append(path)
        if path == "/v1/health":
            return {"held": False}
        if path == "/v1/submit":
            return {"conversationId": 1, "submissionId": 1}
        return {}

    return request


@pytest.mark.parametrize(
    "failure,reason",
    [
        (
            ValueError("Open Plugins → Connection and set: Token."),
            "Open Plugins → Connection and set: Token.",
        ),
        (
            KeyError("status"),
            "A plugin returned an unexpected result. Check it in Plugins, then send the message again.",
        ),
    ],
)
async def test_plugin_failures_fail_the_message_instead_of_waiting_for_the_runtime(
    application, monkeypatch, failure, reason
):
    core = application.state.core
    calls = []
    monkeypatch.setattr(core.coordinator, "request", runtime(calls))
    monkeypatch.setattr(core, "remote", quiet_remote)

    async def broken(event, event_id, task, data):
        raise failure

    monkeypatch.setattr(core.hooks, "run", broken)
    task = core.tasks.create("Hello")
    await core.coordinator.tick()
    message = core.db.one("SELECT status,error FROM messages WHERE task_id=?", (task["id"],))
    assert message == {"status": "failed", "error": reason}
    saved = core.db.one("SELECT status,error,wait_reason FROM tasks WHERE id=?", (task["id"],))
    assert saved == {"status": "failed", "error": reason, "wait_reason": None}
    assert core.coordinator.ready and "/v1/submit" not in calls


async def test_changed_plugin_files_fail_the_message_with_their_reason(application, monkeypatch):
    core = application.state.core
    monkeypatch.setattr(core.coordinator, "request", runtime([]))
    monkeypatch.setattr(core, "remote", quiet_remote)

    def instructions(task=None):
        raise ValueError("Installed plugin contents changed. Reinstall the immutable artifact.")

    monkeypatch.setattr(core, "instructions", instructions)
    task = core.tasks.create("Hello")
    await core.coordinator.tick()
    message = core.db.one("SELECT status,error FROM messages WHERE task_id=?", (task["id"],))
    assert message["status"] == "failed" and "contents changed" in message["error"]
    assert core.coordinator.ready


async def test_one_unexpected_failure_does_not_leave_other_messages_running_detached(
    application, monkeypatch
):
    core = application.state.core
    monkeypatch.setattr(core, "remote", quiet_remote)
    first = core.tasks.create("One")
    second = core.tasks.create("Two", bot_id=core.bots.save({"name": "Second"})["id"])
    finished = []

    async def request(method, path, **kwargs):
        if path == "/v1/health":
            return {"held": False}
        if path == "/v1/submit":
            if kwargs["json"]["taskId"] == first["id"]:
                raise RuntimeError("unexpected")
            await asyncio.sleep(0.05)
            finished.append(second["id"])
            return {"conversationId": 1, "submissionId": 1}
        return {}

    monkeypatch.setattr(core.coordinator, "request", request)
    with pytest.raises(RuntimeError):
        await core.coordinator.tick()
    assert finished == [second["id"]]
    assert core.db.one("SELECT status FROM messages WHERE task_id=?", (second["id"],))["status"] == "running"


async def test_after_hooks_report_a_plugin_setting_error_once_and_keep_the_result(application, monkeypatch):
    core = application.state.core

    async def broken(event, event_id, task, data):
        raise ValueError("Open Plugins → Connection and set: Token.")

    monkeypatch.setattr(core.hooks, "run", broken)
    task = core.db.one("SELECT * FROM tasks WHERE id=?", (core.tasks.create("Hello")["id"],))
    data = {"answer": "done", "error": None, "status": "completed"}
    for _ in range(2):
        assert await core.hooks.notify("after_response", "reply", task, data) == data
    events = core.db.all("SELECT title FROM events WHERE kind='plugin' AND task_id=?", (task["id"],))
    assert events == [{"title": "Open Plugins → Connection and set: Token."}]


# 5. Indexes and package validation.


def test_polling_indexes_exist_and_are_added_to_existing_databases(application):
    db = application.state.core.db
    names = {"actions_task", "actions_status", "events_task", "usage_ledger_created"}

    def indexes():
        return {row["name"] for row in db.all("SELECT name FROM sqlite_master WHERE type='index'")}

    assert names <= indexes()
    for name in names:
        db.execute(f"DROP INDEX {name}")
    Database(db.path)
    assert names <= indexes()
    plan = db.all("EXPLAIN QUERY PLAN SELECT id FROM actions WHERE status IN ('pending','uncertain')")
    assert any("actions_status" in row["detail"] for row in plan)
    plan = db.all("EXPLAIN QUERY PLAN SELECT * FROM actions WHERE task_id='t' ORDER BY created")
    assert any("actions_task" in row["detail"] for row in plan)


def test_installed_packages_are_validated_once_per_content_and_tampering_is_detected(
    application, monkeypatch
):
    store = application.state.core.plugins
    store.stage(skill())
    calls, real = [], plugins.validate_package
    monkeypatch.setattr(plugins, "validate_package", lambda files: calls.append(1) or real(files))
    row, files = store.package("test-skill", "1.0.0")
    assert store.package("test-skill", "1.0.0")[1] == files and len(calls) == 1
    (Path(row["path"]) / "SKILL.md").write_bytes(files["SKILL.md"] + b"\nDo something else.")
    for _ in range(2):
        with pytest.raises(ValueError, match="contents changed"):
            store.package("test-skill", "1.0.0")


# 6. Retention.


def test_daily_retention_prunes_only_records_nothing_reads(application):
    core = application.state.core
    db, now = core.db, time.time()
    old, legacy, recent, active = (
        core.tasks.create(title)["id"] for title in ("Old", "Legacy", "Recent", "Active")
    )
    receipt, fresh_receipt = "hook-" + "a" * 64, "hook-" + "b" * 64
    with db.transaction() as sql:
        sql.execute(
            "UPDATE tasks SET status='completed',created=? WHERE id IN (?,?)", (now - 40 * DAY, old, legacy)
        )
        sql.execute("UPDATE tasks SET status='completed' WHERE id=?", (recent,))
        sql.execute("UPDATE messages SET status='completed',answer='done' WHERE task_id=?", (legacy,))
        sql.execute(
            "UPDATE messages SET status='completed',answer='done',finished=? WHERE task_id=?", (now, old)
        )
        sql.execute("INSERT INTO sessions VALUES ('expired',?,'browser')", (now - 1,))
        sql.execute("INSERT INTO sessions VALUES ('live',?,'browser')", (now + 60,))
        for created in (now - 100 * DAY, now - 3600):
            sql.execute(
                "INSERT INTO usage_ledger(task_id,tokens,cost,created) VALUES ('t',5,0,?)", (created,)
            )
        for kind, task_id, created in [
            ("completed", old, now - 200 * DAY),
            ("completed", legacy, now - 200 * DAY),
            ("warning", None, now - 200 * DAY),
            ("warning", None, now - DAY),
        ]:
            sql.execute(
                "INSERT INTO events(kind,title,task_id,created) VALUES (?,?,?,?)",
                (kind, kind, task_id, created),
            )
        for row in sql.execute("SELECT id FROM events").fetchall():
            sql.execute("INSERT INTO notification_reads VALUES (?,'1')", (f"event:{row['id']}",))
        sql.execute("INSERT INTO notification_reads VALUES ('action:kept','pending')")
        for index in range(205):
            sql.execute(
                "INSERT INTO occurrences(id,schedule_id,revision,intended,task_id,status) VALUES (?,?,1,?,?,'admitted')",
                (f"run-{index}", "job", now - 40 * DAY - index, active if index == 204 else old),
            )
        for key, status, created in [
            ("a", "sent", now - 40 * DAY),
            ("b", "sent", now),
            ("c", "failed", now - 40 * DAY),
        ]:
            sql.execute("INSERT INTO outbox VALUES (?,'telegram','{}',?,?,NULL,NULL)", (key, status, created))
        sql.execute("INSERT INTO settings VALUES ('telegram_offset','20000')")
        for update in ("5", "15000", "callback-1"):
            sql.execute("INSERT INTO channel_updates VALUES ('telegram',?,'')", (update,))
        sql.execute("INSERT INTO task_requests VALUES ('old-request','h',?)", (old,))
        sql.execute("INSERT INTO task_requests VALUES ('recent-request','h',?)", (recent,))
    db.set(receipt, {"input_hash": "x", "result": {}})
    db.set(fresh_receipt, {"input_hash": "x", "at": now})
    db.set("hook-warning:after_tool:event", True)
    preferences = db.setting("preferences")

    removed = retention.prune(db, now)

    assert removed["sessions"] == 1 and db.all("SELECT hash FROM sessions") == [{"hash": "live"}]
    assert [round(now - row["created"]) for row in db.all("SELECT created FROM usage_ledger")] == [3600]
    kept = db.all("SELECT kind,task_id FROM events ORDER BY id")
    assert kept == [{"kind": "completed", "task_id": legacy}, {"kind": "warning", "task_id": None}]
    reads = {row["id"] for row in db.all("SELECT id FROM notification_reads")}
    assert reads == {"action:kept"} | {f"event:{row['id']}" for row in db.all("SELECT id FROM events")}
    runs = {row["id"] for row in db.all("SELECT id FROM occurrences")}
    assert runs == {f"run-{index}" for index in range(200)} | {"run-204"}
    assert {row["id"] for row in db.all("SELECT id FROM outbox")} == {"b", "c"}
    assert {row["update_id"] for row in db.all("SELECT update_id FROM channel_updates")} == {
        "15000",
        "callback-1",
    }
    assert db.all("SELECT request_id FROM task_requests") == [{"request_id": "recent-request"}]
    # Receipts saved before they carried a time start their month at the first pass.
    # SQLite writes the time with its own float precision, which differs between versions.
    stamped = db.setting(receipt)
    assert stamped.pop("at") == pytest.approx(now, abs=0.001)
    assert stamped == {"input_hash": "x", "result": {}}
    assert db.setting("hook-warning:after_tool:event") == now
    assert db.setting("preferences") == preferences and db.setting("retention_at") == now
    assert len(db.all("SELECT id FROM tasks")) == 4 and len(db.all("SELECT id FROM messages")) == 4

    retention.prune(db, now + 31 * DAY)
    assert db.setting(receipt) is None and db.setting(fresh_receipt) is None
    assert db.setting("hook-warning:after_tool:event") is None
    assert db.setting("preferences") == preferences


async def test_retention_runs_at_most_once_a_day(application, monkeypatch):
    db, passes = application.state.core.db, []
    monkeypatch.setattr(
        retention, "prune", lambda db: passes.append(1) or db.set("retention_at", time.time())
    )

    async def stop(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(retention.asyncio, "sleep", stop)
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            await retention.run(db)
    assert passes == [1]


def test_old_restore_points_and_their_images_are_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path / "home"))
    cli.write_private(cli.home() / "deploy.env", "SHINBOT_PROJECT=test\n")
    backups = cli.home() / "backups"
    backups.mkdir()
    kinds = ["update", "restore", "update", "rollback", "update", "update", "restore", "update"]
    names = [f"before-{kind}-{1000 + index}.tar.gz" for index, kind in enumerate(kinds)]
    for name in [*names, "my-own-backup.tar.gz"]:
        (backups / name).write_bytes(b"archive")
    maintenance.write_json(
        cli.home() / "recovery.json", {"pending": False, "backup": str(backups / names[0])}
    )
    monkeypatch.setattr(cli, "docker", lambda: ["docker"])
    tags = [f"shinbot-rollback/test-control:{name[:-3]}" for name in names if "update" in name]
    removed = []

    def run(command, **kwargs):
        if command[1:3] == ["image", "ls"]:
            # Another installation on this engine, one of them with a name that begins with ours.
            others = ["shinbot-rollback/other-control:before-update-1.tar", "shinbot-rollback/test-two-control:before-update-1.tar"]
            listed = "\n".join([*tags, *others])
            return subprocess.CompletedProcess(command, 0, listed, "")
        removed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(maintenance.subprocess, "run", run)
    maintenance.prune_restore_points()
    # The five newest, the checkpoint a rollback still needs, and the owner's own archive remain.
    assert {path.name for path in backups.iterdir()} == {names[0], *names[3:], "my-own-backup.tar.gz"}
    assert removed == [["docker", "image", "rm", "shinbot-rollback/test-control:" + names[2][:-3]]]


# 7. Daylight-saving schedules.


def test_times_skipped_in_spring_run_at_the_first_valid_instant():
    daily = next_run("cron", "30 2 * * *", NEW_YORK, utc("2027-03-13T17:00:00"))
    assert daily == utc("2027-03-14T07:00:00")  # 03:00 EDT; 02:30 does not exist that day
    assert next_run("cron", "30 2 * * *", NEW_YORK, daily) == utc("2027-03-15T06:30:00")
    assert next_run("cron", "30 2 * * 0", NEW_YORK, utc("2027-03-08T17:00:00")) == utc("2027-03-14T07:00:00")
    after, runs = utc("2027-03-14T06:45:00"), []
    for _ in range(3):
        after = next_run("cron", "*/15 * * * *", NEW_YORK, after)
        runs.append(after)
    assert runs == [utc("2027-03-14T07:00:00"), utc("2027-03-14T07:15:00"), utc("2027-03-14T07:30:00")]
    assert cron_run("30 2 * * *", ZoneInfo(NEW_YORK), daily + 5, -1) == daily


def test_repeated_hour_runs_wildcard_jobs_on_both_passes_and_fixed_times_once():
    after, gaps = utc("2026-11-01T04:40:00"), set()
    for _ in range(24):
        following = next_run("cron", "*/10 * * * *", NEW_YORK, after)
        gaps.add(following - after)
        after = following
    assert gaps == {600} and after == utc("2026-11-01T08:40:00")
    hourly = next_run("cron", "30 * * * *", NEW_YORK, utc("2026-11-01T05:30:00"))
    assert hourly == utc("2026-11-01T06:30:00")  # 01:30 EST, the second pass
    assert next_run("cron", "30 * * * *", NEW_YORK, hourly) == utc("2026-11-01T07:30:00")
    first = next_run("cron", "30 1 * * *", NEW_YORK, utc("2026-10-31T17:00:00"))
    assert first == utc("2026-11-01T05:30:00")  # 01:30 EDT, the first pass only
    assert next_run("cron", "30 1 * * *", NEW_YORK, first) == utc("2026-11-02T06:30:00")
    zone = ZoneInfo(NEW_YORK)
    assert cron_run("30 * * * *", zone, utc("2026-11-01T06:35:00"), -1) == utc("2026-11-01T06:30:00")
    assert cron_run("30 1 * * *", zone, utc("2026-11-01T06:35:00"), -1) == utc("2026-11-01T05:30:00")


def test_a_job_skipped_by_the_clock_change_is_admitted_once(application):
    core = application.state.core
    job = core.jobs.save(
        {"name": "Night", "prompt": "Check", "kind": "cron", "spec": "30 2 * * *", "timezone": NEW_YORK}
    )
    run = utc("2027-03-14T07:00:00")
    core.db.execute("UPDATE schedules SET next_run=? WHERE id=?", (run, job["id"]))
    assert len(core.jobs.tick(run + 5)) == 1
    occurrence = core.db.one("SELECT intended,status FROM occurrences WHERE schedule_id=?", (job["id"],))
    assert occurrence == {"intended": run, "status": "admitted"}
    saved = core.db.one("SELECT next_run,enabled FROM schedules WHERE id=?", (job["id"],))
    assert saved == {"next_run": utc("2027-03-15T06:30:00"), "enabled": 1}
    assert core.jobs.tick(run + 10) == []


# 8. Wrongly typed fields.


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/notes", {"title": 1, "body": "x"}),
        ("post", "/api/notes", {"title": "t", "body": None}),
        ("post", "/api/bot-groups", {"name": 5}),
        ("post", "/api/marketplaces", {"source": 5}),
        ("post", "/api/plugins/stage", {"files": {"a": 1}}),
        ("post", "/api/plugins/stage", {"files": [1]}),
        ("post", "/api/memories", {"text": 5}),
        (
            "post",
            "/api/jobs",
            {
                "name": "a",
                "prompt": "b",
                "kind": "interval",
                "spec": "3600",
                "timezone": "UTC",
                "bot_id": ["x"],
            },
        ),
        (
            "post",
            "/api/jobs",
            {"name": "a", "prompt": "b", "kind": ["cron"], "spec": "0 9 * * *", "timezone": "UTC"},
        ),
        ("post", "/api/plugins/skills/install", {"license": "MIT", "command": 5}),
        ("post", "/api/plugins/skills/install", {"license": ["MIT"], "command": "x"}),
        ("post", "/api/plugins/skills/import", {"files": {"SKILL.md": 5}, "license": "MIT"}),
        ("post", "/api/browser/select", {"bot_id": ["x"]}),
        ("post", "/api/bots", {"profile": {"name": "x"}, "id": ["a"]}),
        ("put", "/api/plugins/x/access", {"mode": ["a"], "bots": []}),
    ],
)
def test_wrongly_typed_fields_are_bad_requests_not_server_errors(owner, method, path, body):
    response = getattr(owner, method)(path, json=body)
    assert response.status_code in {400, 404}, response.text
    assert response.json()["detail"]


def test_non_ascii_credentials_are_refused_not_server_errors(client, application):
    claim = {"bootstrap": "clé-incorrecte", "password": PASSWORD}
    assert client.post("/auth/login", json=claim).status_code == 401
    header = {"Authorization": "Bearer clé".encode("latin-1")}
    assert client.post("/internal/admit", json={"task_id": "t"}, headers=header).status_code == 401


# 9. Not found and conflict.


def test_missing_resources_are_404_and_validation_stays_400(owner):
    assert owner.get("/api/tasks/missing").status_code == 404
    assert owner.post("/api/tasks/missing/messages", json={"content": "x"}).status_code == 404
    assert owner.patch("/api/tasks/missing/options", json={"approval_mode": "ask"}).status_code == 404
    assert owner.post("/api/jobs/missing/toggle", json={"enabled": True}).status_code == 404
    assert owner.delete("/api/jobs/missing").status_code == 404
    assert owner.get("/api/plugins/missing/1.0.0/files").status_code == 404
    assert owner.put("/api/plugins/missing/access", json={"mode": "shared", "bots": []}).status_code == 404
    assert owner.get("/api/tasks/missing").json() == {"detail": "Task not found."}
    assert owner.post("/api/jobs", json={"name": "x"}).status_code == 400


# 10. Settings.


def test_partial_settings_update_keeps_saved_values(owner):
    saved = owner.get("/api/settings").json()
    chosen = {**saved, "daily_tokens": 50000, "task_tokens": 5000, "learn_preferences": False}
    owner.put("/api/settings", json=chosen).raise_for_status()
    renamed = owner.put("/api/settings", json={"name": "Renamed"})
    assert renamed.status_code == 200 and renamed.json() == {**chosen, "name": "Renamed"}
    assert owner.get("/api/settings").json() == {**chosen, "name": "Renamed"}
    # The merged result is validated as a whole: a model needs its provider.
    assert owner.put("/api/settings", json={"provider": ""}).status_code == 400
    assert owner.get("/api/settings").json()["provider"] == saved["provider"]


# 11. Password change and host reset.


def test_password_change_keeps_this_session_and_signs_out_the_rest(owner, application):
    core, route = application.state.core, "/api/auth/password"
    core.auth.penalty = 0
    elsewhere = {"Authorization": "Bearer " + core.auth.session("browser")}
    wrong = owner.post(route, json={"current": "not-the-right-password", "new": "a-brand-new-password"})
    assert wrong.status_code == 403 and "not changed" in wrong.json()["detail"]
    assert len(core.auth.failures("testclient")) == 1
    assert owner.post(route, json={"current": PASSWORD, "new": "short"}).status_code == 400
    assert owner.post(route, json={"current": PASSWORD}).status_code == 422
    assert owner.get("/api/settings", headers=elsewhere).status_code == 200
    assert owner.post(route, json={"current": PASSWORD, "new": "a-brand-new-password"}).json() == {"ok": True}
    assert owner.get("/api/settings").status_code == 200
    assert owner.get("/api/settings", headers=elsewhere).status_code == 401
    owner.headers.pop("Authorization")
    assert login(owner, PASSWORD).status_code == 401
    assert login(owner, "a-brand-new-password").status_code == 200


def test_wrong_current_passwords_share_the_sign_in_limit(owner, application):
    application.state.core.auth.penalty = 0
    body = {"current": "not-the-right-password", "new": "a-brand-new-password"}
    codes = [owner.post("/api/auth/password", json=body).status_code for _ in range(9)]
    assert codes == [403] * 8 + [429]
    correct = owner.post("/api/auth/password", json={"current": PASSWORD, "new": "a-brand-new-password"})
    assert correct.json() == {"ok": True}


def beside_gateway(core, seen):
    def compose(*args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.run(
            [sys.executable, "-c", args[-1]],
            env={**os.environ, "SHINBOT_DATA": str(core.config.data)},
            input=kwargs["input"],
            capture_output=True,
            text=True,
        )

    return compose


def test_host_reset_replaces_the_password_and_signs_every_device_out(
    owner, application, monkeypatch, tmp_path
):
    core, seen = application.state.core, []
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path / "home"))
    cli.write_private(cli.home() / "deploy.env", "SHINBOT_PROJECT=test\n")
    monkeypatch.setattr(cli, "compose", beside_gateway(core, seen))
    short = CliRunner().invoke(cli.app, ["reset-password"], input="short\nshort\n")
    assert short.exit_code != 0 and not seen
    result = CliRunner().invoke(
        cli.app, ["reset-password"], input="reset-password-long\nreset-password-long\n"
    )
    assert result.exit_code == 0, result.output
    assert "Every device was signed out" in result.output
    args, options = seen[0]
    assert args[:5] == ("exec", "-T", "control", "python", "-c")
    # The password reaches the container on standard input only.
    assert "reset-password-long" not in json.dumps(args) and options["input"] == "reset-password-long\n"
    assert owner.get("/api/settings").status_code == 401
    owner.headers.pop("Authorization")
    assert login(owner, PASSWORD).status_code == 401
    assert login(owner, "reset-password-long").status_code == 200


def test_host_reset_refuses_an_unclaimed_installation(client, application, monkeypatch, tmp_path):
    core = application.state.core
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path / "home"))
    cli.write_private(cli.home() / "deploy.env", "SHINBOT_PROJECT=test\n")
    monkeypatch.setattr(cli, "compose", beside_gateway(core, []))
    result = CliRunner().invoke(
        cli.app, ["reset-password"], input="reset-password-long\nreset-password-long\n"
    )
    assert result.exit_code != 0 and "no owner yet" in result.output
    assert core.db.setting("password") is None


def test_password_change_loses_to_a_change_made_while_it_was_hashing(owner, application, monkeypatch):
    from shinbot import app as gateway

    core = application.state.core
    hash_password = gateway.hash_password

    def racing(password):
        # Another device finishes its own change while this one is still hashing.
        core.db.set("password", hash_password("the-other-devices-password"))
        return hash_password(password)

    monkeypatch.setattr(gateway, "hash_password", racing)
    sessions = core.db.one("SELECT count(*) AS n FROM sessions")["n"]
    late = owner.post("/api/auth/password", json={"current": PASSWORD, "new": "a-brand-new-password"})
    assert late.status_code == 409 and "changed somewhere else" in late.json()["detail"]
    assert core.db.one("SELECT count(*) AS n FROM sessions")["n"] == sessions
    owner.headers.pop("Authorization")
    assert login(owner, "a-brand-new-password").status_code == 401
    assert login(owner, "the-other-devices-password").status_code == 200


def test_missing_plugin_files_are_reported_as_a_plugin_fault(application):
    import shutil

    core = application.state.core
    staged = core.plugins.stage_from_catalog("official", "research", "1.0.0")
    row = core.db.one("SELECT path FROM plugins WHERE id='research'")
    shutil.rmtree(row["path"])
    with pytest.raises(ValueError, match="missing or unreadable. Reinstall it"):
        core.plugins.package("research", staged["version"])


def test_a_sign_in_that_was_verifying_the_old_password_gets_no_session(owner, application, monkeypatch):
    from shinbot import app as gateway

    core = application.state.core
    verify_password = gateway.verify_password

    def racing(password, saved):
        # The password is changed (and every session revoked) while this sign-in is being checked.
        matches = verify_password(password, saved)
        core.db.set("password", gateway.hash_password("a-brand-new-password"))
        core.db.execute("DELETE FROM sessions")
        return matches

    monkeypatch.setattr(gateway, "verify_password", racing)
    owner.headers.pop("Authorization")
    stale = login(owner, PASSWORD)
    assert stale.status_code == 401 and "just changed" in stale.json()["detail"]
    assert core.db.one("SELECT count(*) AS n FROM sessions")["n"] == 0
    monkeypatch.setattr(gateway, "verify_password", verify_password)
    assert login(owner, PASSWORD).status_code == 401
    assert login(owner, "a-brand-new-password").status_code == 200
