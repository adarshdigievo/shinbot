import asyncio
import hashlib
import time

import pytest
from test_actions import action

from shinbot.approvals import Approvals, rule_for
from shinbot.db import packed, uid


def pending(core, task, tool, args):
    key = uid()
    core.db.execute(
        "INSERT INTO actions(id,task_id,epoch,tool,args,hash,status,created,expires) "
        "VALUES (?,?,?,?,?,?,'pending',?,?)",
        (key, task["id"], core.db.setting("epoch"), tool, packed(args), key, time.time(), time.time() + 60),
    )
    return key


def allows(core, task, tool, args):
    with core.db.transaction() as db:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
        return Approvals(core.db).allows(row, tool, args, db)


def test_always_allows_new_arguments_for_the_same_plugin_tool(owner, application):
    core = application.state.core
    task = core.tasks.create("Search the web")
    tool = "plugin:serpapi:1.0.0:web_search"
    key = pending(core, task, tool, {"query": "first query"})
    owner.post(f"/api/actions/{key}/decision", json={"approved": True, "scope": "always"}).raise_for_status()
    other = core.tasks.create("Another search")
    args = {"query": "another query", "limit": 5, "operation": "an ordinary plugin argument"}
    assert allows(core, other, tool, args)
    for different in [
        "plugin:serpapi:1.0.0:other_tool",
        "plugin:other:1.0.0:web_search",
        "plugin:serpapi:2.0.0:web_search",
        "run_command",
    ]:
        assert not allows(core, other, different, args)
    # Repeated choices produce one standing rule rather than one rule per query.
    another = pending(core, other, tool, args)
    owner.post(
        f"/api/actions/{another}/decision", json={"approved": True, "scope": "always"}
    ).raise_for_status()
    rules = owner.get("/api/approvals").json()
    assert len(rules) == 1 and "any arguments" in rules[0]["label"]
    owner.delete("/api/approvals/" + rules[0]["id"]).raise_for_status()
    assert not allows(core, other, tool, args)


@pytest.mark.parametrize(
    "tool,operation,other",
    [
        ("browser", "goto", "fill"),
        ("plugins", "activate", "set_access"),
        ("memory", "remember", "forget"),
        ("jobs", "create", "pause"),
        ("bots", "save", "install"),
    ],
)
def test_saved_builtin_approvals_keep_operations_separate(application, tool, operation, other):
    core = application.state.core
    task = core.tasks.create("Use a built-in command")
    key = pending(core, task, tool, {"operation": operation, "value": "first"})
    core.approvals.decide(key, True, "always")
    assert allows(core, task, tool, {"operation": operation, "value": "changed"})
    assert not allows(core, task, tool, {"operation": other, "value": "first"})


def test_legacy_rules_stay_exact_and_chat_rules_stay_local(application):
    core = application.state.core
    task = core.tasks.create("Original conversation")
    other = core.tasks.create("Another conversation")
    tool, args = "plugin:serpapi:1.0.0:web_search", {"query": "original"}
    legacy = hashlib.sha256(packed({"tool": tool, "args": args}).encode()).hexdigest()
    core.db.execute(
        "INSERT INTO approval_rules VALUES (?,?,?,?,?,?,?)",
        (uid(), "always", "", legacy, "Old exact approval", time.time(), ""),
    )
    assert allows(core, other, tool, args)
    assert not allows(core, other, tool, {"query": "new"})
    key = pending(core, task, tool, args)
    core.approvals.decide(key, True, "chat")
    assert allows(core, task, tool, {"query": "new"})
    assert not allows(core, other, tool, {"query": "new"})
    core.db.execute("UPDATE tasks SET parent_task_id=? WHERE id=?", (task["id"], other["id"]))
    assert not allows(core, other, tool, args)
    child_key = pending(core, other, tool, args)
    core.approvals.decide(child_key, True, "chat")
    assert allows(core, other, tool, {"query": "child's new query"})


async def test_saved_rule_releases_waiting_actions_without_repeating_or_reviving_them(
    application, monkeypatch
):
    core = application.state.core
    calls = []

    async def remote(service, path, body=None, **kwargs):
        if path == "/agent/release":
            return {}
        calls.append(body)
        return {"status": "completed", "result": {"stdout": "done"}}

    monkeypatch.setattr(core, "remote", remote)
    task = core.tasks.create("First chat")
    grant = action(core, task, "run_command", {"command": "echo one"})
    waiting, expired, denied, cancelled = [
        action(core, core.tasks.create(name), "run_command", {"command": "echo " + name})
        for name in ("waiting", "expired", "denied", "cancelled")
    ]
    for payload in (grant, waiting, expired, denied, cancelled):
        assert (await core.action(payload))["status"] == "pending"
    core.db.execute("UPDATE actions SET expires=? WHERE id=?", (time.time() - 1, expired["id"]))
    core.approvals.decide(denied["id"], False)
    core.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (cancelled["task_id"],))
    core.approvals.decide(grant["id"], True, "always")
    assert (await core.action(waiting))["status"] == "executing"
    assert core.tasks.detail(waiting["task_id"])["status"] == "running"
    await asyncio.gather(*core.running_actions.values())
    assert (await core.action(waiting))["status"] == "completed"
    assert len(calls) == 1
    assert (await core.action(expired))["status"] == "denied"
    assert (await core.action(denied))["status"] == "denied"
    assert (await core.action(cancelled))["status"] == "blocked"
    with pytest.raises(ValueError, match="different inputs"):
        await core.action({**waiting, "args": {"command": "changed receipt"}})


async def test_saved_browser_rule_cannot_bypass_human_takeover(application, monkeypatch):
    core = application.state.core
    task = core.tasks.create("Browse", options={"approval_mode": "ask"})
    lease = None

    async def remote(service, path, body=None, **kwargs):
        if path == "/agent/release":
            return {}
        if path == "/assess":
            return {"safe": True, "reason": "Open a page.", "context": "page"}
        assert service == "browser" and path == "/lease"
        return {"lease": lease}

    monkeypatch.setattr(core, "remote", remote)
    payload = action(core, task, "browser", {"operation": "goto", "url": "https://example.com"})
    assert (await core.action(payload))["status"] == "pending"
    key, label = rule_for("browser", {"operation": "goto"})
    core.db.execute(
        "INSERT INTO approval_rules VALUES (?,?,?,?,?,?,?)",
        (uid(), "always", "", key, label, time.time(), payload["id"]),
    )
    lease = {"kind": "human", "owner": "owner"}
    assert (await core.action(payload))["error"] == "Waiting for the shared browser."
    assert core.db.one("SELECT status FROM actions WHERE id=?", (payload["id"],))["status"] == "pending"
    assert not core.running_actions
