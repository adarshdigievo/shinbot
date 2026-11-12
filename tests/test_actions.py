import asyncio
import uuid

import pytest


def action(core, task, tool="notes", args=None):
    return {
        "id": uuid.uuid4().hex,
        "task_id": task["id"],
        "epoch": core.db.setting("epoch"),
        "tool": tool,
        "args": args or {"operation": "save", "title": "Result", "body": "saved once"},
    }


async def test_action_retry_returns_receipt_without_repeating(application):
    core = application.state.core
    task = core.tasks.create("write a note")
    payload = action(core, task)
    assert (await core.action(payload))["status"] == "executing"
    await asyncio.gather(*core.running_actions.values())
    receipt = await core.action(payload)
    assert receipt["status"] == "completed"
    assert len(core.notebook.search()) == 1
    assert (await core.action(payload))["result"] == receipt["result"]
    with pytest.raises(ValueError, match="different inputs"):
        await core.action({**payload, "args": {"operation": "search"}})


async def test_old_epoch_never_dispatches_after_restore(application):
    core = application.state.core
    task = core.tasks.create("write a note")
    payload = action(core, task)
    core.db.set("epoch", "new-installation-epoch")
    assert (await core.action(payload))["status"] == "uncertain"
    assert core.notebook.search() == []


async def test_commands_wait_for_permission_and_cancel_blocks(application):
    core = application.state.core
    task = core.tasks.create("run a command")
    payload = action(core, task, "run_command", {"command": "echo hello"})
    assert (await core.action(payload))["status"] == "pending"
    assert not core.running_actions
    core.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task["id"],))
    assert (await core.action(payload))["status"] == "blocked"


async def test_disabled_plugin_and_unselected_version_are_blocked(application):
    core = application.state.core
    task = core.tasks.create("plugin")
    with pytest.raises(ValueError, match="not selected"):
        await core.action(action(core, task, "plugin:missing:1.0.0:run", {}))


async def test_approved_agent_job_preserves_permissions_and_runs_once(application):
    import json

    core = application.state.core
    task = core.tasks.create("Remind me every hour")
    payload = action(
        core,
        task,
        "jobs",
        {
            "operation": "create",
            "job": {
                "name": "Hourly reminder",
                "prompt": "Check my plan",
                "kind": "interval",
                "spec": "3600",
                "timezone": "Asia/Kolkata",
            },
        },
    )
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)
    await core.action(payload)
    await asyncio.gather(*core.running_actions.values())
    assert (await core.action(payload))["status"] == "completed"
    (job,) = core.db.all("SELECT * FROM schedules")
    grants = json.loads(core.db.one("SELECT grants FROM tasks WHERE id=?", (task["id"],))["grants"])
    assert json.loads(job["grants"]) == grants
    (run,) = core.jobs.tick(job["next_run"] + 1)
    assert json.loads(core.db.one("SELECT grants FROM tasks WHERE id=?", (run,))["grants"]) == grants
    assert core.jobs.tick(job["next_run"] + 1) == []


@pytest.mark.parametrize("tool,operation", [("notes", "delete"), ("memory", "forget")])
async def test_agent_deletion_requires_owner_approval(application, tool, operation):
    core = application.state.core
    saved = (
        core.notebook.save("Keep", "My note") if tool == "notes" else core.notebook.remember("My preference")
    )
    task = core.tasks.create("Review what you remember")
    payload = action(core, task, tool, {"operation": operation, "id": saved["id"]})
    assert (await core.action(payload))["status"] == "pending"
    items = core.notebook.search() if tool == "notes" else core.notebook.memories()
    assert any(item["id"] == saved["id"] for item in items)
    core.approvals.decide(payload["id"], True)
    await core.action(payload)
    await asyncio.gather(*core.running_actions.values())
    assert (await core.action(payload))["status"] == "completed"
    items = core.notebook.search() if tool == "notes" else core.notebook.memories()
    assert not any(item["id"] == saved["id"] for item in items)
