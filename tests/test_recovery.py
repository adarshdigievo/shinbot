import asyncio

import httpx
import pytest
from test_actions import action

from shinbot.core import Core
from shinbot.services.common import Journal


async def drain(core):
    await asyncio.gather(*list(core.running_actions.values()))


def due(core):
    core.db.execute("UPDATE action_retries SET next_attempt=0")


@pytest.mark.parametrize("receipt_status", ["completed", "uncertain", "executing", "missing"])
async def test_restart_reconciles_original_receipt_without_repeating_effect(
    application, monkeypatch, receipt_status
):
    core = application.state.core
    task = core.tasks.create("Append once", grants={"computer": True})
    payload = action(core, task, "run_command", {"command": "append"})
    journal = Journal(core.config.data / "executor")
    executions = []

    async def remote(service, path, body=None, **kwargs):
        if path.startswith("/actions/"):
            return journal.receipt(payload["id"])
        if path != "/execute":
            return {}
        executions.append(body["id"])
        journal.claim(body)
        journal.finish(body["id"], {"output": "appended once"})
        raise httpx.ReadError("Response connection was lost")

    monkeypatch.setattr(core, "remote", remote)
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)
    await core.action(payload)
    await drain(core)
    assert core.db.one("SELECT status FROM actions")["status"] == "retrying"
    if receipt_status == "missing":
        # No effect was sent in this branch.
        journal.db.execute("DELETE FROM effects")
        executions.clear()
    else:
        journal.db.execute("UPDATE effects SET status=?", (receipt_status,))
    core.db.execute("UPDATE actions SET status='executing'")
    reopened = Core(core.config)
    reopened.recover()
    monkeypatch.setattr(reopened, "remote", remote)
    due(reopened)
    await reopened.action(payload)
    await drain(reopened)
    if receipt_status == "missing":
        due(reopened)
        await reopened.action(payload)
        await drain(reopened)
    row = reopened.db.one("SELECT * FROM actions")
    assert row["id"] == payload["id"] and executions == [payload["id"]]
    assert (
        row["status"]
        == {
            "completed": "completed",
            "missing": "completed",
            "uncertain": "uncertain",
            "executing": "retrying",
        }[receipt_status]
    )
    if receipt_status == "uncertain":
        assert reopened.db.one("SELECT status FROM tasks")["status"] == "uncertain"
        assert (await reopened.action(payload))["status"] == "blocked"
    if row["status"] == "completed":
        assert row["error"] is None
    journal.db.close()


async def test_cancelled_offline_action_never_dispatches_after_reconnect(application, monkeypatch):
    core = application.state.core
    task = core.tasks.create("Write", grants={"computer": True})
    payload = action(core, task, "write_file", {"path": "a", "content": "b"})

    async def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(core, "remote", offline)
    monkeypatch.setattr(core.coordinator, "abort_task", offline)
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)
    await core.action(payload)
    await drain(core)
    await core.cancel(task["id"])
    due(core)
    assert (await core.action(payload))["status"] == "blocked"
    assert not core.running_actions
    assert core.db.one("SELECT status FROM tasks")["status"] == "cancelled"
    # A crash can leave an unresolved receipt after the owner has cancelled.
    core.db.execute("UPDATE actions SET status='uncertain'")
    core.recover()
    assert core.db.one("SELECT status FROM tasks")["status"] == "cancelled"


def test_executor_restart_preserves_completed_receipts_and_holds_unfinished_effects(tmp_path):
    journal = Journal(tmp_path)
    journal.claim({"id": "done", "epoch": "a"})
    journal.finish("done", {"ok": True})
    journal.claim({"id": "interrupted", "epoch": "a"})
    journal.db.close()
    restarted = Journal(tmp_path)
    assert restarted.receipt("done") == {"status": "completed", "result": {"ok": True}}
    assert restarted.receipt("interrupted")["status"] == "uncertain"
    assert restarted.receipt("missing")["status"] == "missing"
    restarted.db.close()
