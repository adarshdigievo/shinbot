import asyncio

import httpx
import pytest
from test_actions import action

from shinbot.approvals import rule_for
from shinbot.browser_policy import classify, navigation_safe
from shinbot.db import Database

PROVIDERS = [
    {
        "id": "test",
        "name": "Test",
        "configured": True,
        "models": [
            {"id": "quick", "name": "Quick", "thinkingLevels": ["off"]},
            {"id": "deep", "name": "Deep", "thinkingLevels": ["low", "high"]},
        ],
    }
]


def test_workspace_model_and_effort_defaults_apply_only_to_new_chats(owner, application, monkeypatch):
    core = application.state.core

    async def runtime(method, path, **kwargs):
        assert path == "/v1/providers"
        return PROVIDERS

    monkeypatch.setattr(core.coordinator, "request", runtime)
    previous = core.tasks.create("Existing chat")
    preferences = owner.get("/api/settings").json()
    preferences.update(provider="test", model="deep", thinking_level="high")
    owner.put("/api/settings", json=preferences).raise_for_status()
    assert Database(core.db.path).setting("preferences")["thinking_level"] == "high"
    response = owner.post("/api/tasks", json={"content": "Use workspace defaults"})
    response.raise_for_status()
    inherited = core.tasks.detail(response.json()["id"])
    assert (inherited["provider"], inherited["model"], inherited["thinking_level"]) == (
        "test", "deep", "high"
    )
    existing = core.tasks.detail(previous["id"])
    assert (existing["model"], existing["thinking_level"]) == ("test-model", "off")
    response = owner.post("/api/tasks", json={
        "content": "Use a different effort", "options": {"thinking_level": "low"}
    })
    response.raise_for_status()
    assert core.tasks.detail(response.json()["id"])["thinking_level"] == "low"
    assert owner.get("/api/settings").json()["thinking_level"] == "high"


def test_chat_choices_are_validated_persisted_and_do_not_change_other_chats(owner, application, monkeypatch):
    core = application.state.core

    async def runtime(method, path, **kwargs):
        assert path == "/v1/providers"
        return PROVIDERS

    monkeypatch.setattr(core.coordinator, "request", runtime)
    choices = {"provider": "test", "model": "deep", "thinking_level": "high", "approval_mode": "auto"}
    body = {"content": "Research", "request_id": "chosen", "options": choices}
    response = owner.post("/api/tasks", json=body)
    response.raise_for_status()
    task_id = response.json()["id"]
    assert owner.post("/api/tasks", json=body).json()["id"] == task_id
    task = owner.get(f"/api/tasks/{task_id}").json()
    assert all(task[k] == v for k, v in choices.items())
    assert (
        Database(core.db.path).one("SELECT thinking_level FROM tasks WHERE id=?", (task_id,))[
            "thinking_level"
        ]
        == "high"
    )
    assert owner.patch(f"/api/tasks/{task_id}/options", json={"thinking_level": "low"}).status_code == 400
    # Approval remains owner-configurable while work is queued or waiting.
    owner.patch(f"/api/tasks/{task_id}/options", json={"approval_mode": "ask"}).raise_for_status()
    core.db.execute("UPDATE messages SET status='completed' WHERE task_id=?", (task_id,))
    owner.patch(f"/api/tasks/{task_id}/options", json={"thinking_level": "low"}).raise_for_status()
    assert owner.patch(f"/api/tasks/{task_id}/options", json={"thinking_level": "max"}).status_code == 400
    assert owner.patch(f"/api/tasks/{task_id}/options", json={"approval_mode": "anything"}).status_code == 422
    assert (
        owner.patch(
            f"/api/tasks/{task_id}/options", json={"provider": "test", "model": "missing"}
        ).status_code
        == 400
    )
    unrelated = core.tasks.detail(core.tasks.create("Other chat")["id"])
    assert unrelated["approval_mode"] == "auto" and unrelated["thinking_level"] == "off"
    assert core.db.setting("preferences")["model"] == "test-model"


async def test_selected_model_and_effort_are_dispatched_on_each_turn(application, monkeypatch):
    core = application.state.core
    calls = []

    async def runtime(method, path, **kwargs):
        if path == "/v1/submit":
            calls.append(kwargs["json"])
            return {"submissionId": len(calls), "conversationId": 1}
        return {"held": False, "status": "done", "answer": "Finished"}

    async def remote(*args, **kwargs):
        return {}

    monkeypatch.setattr(core.coordinator, "request", runtime)
    monkeypatch.setattr(core, "remote", remote)
    task = core.tasks.create("One", options={"provider": "test", "model": "deep", "thinking_level": "high"})
    await core.coordinator.tick()
    await core.coordinator.tick()
    core.tasks.options(task["id"], {"model": "quick", "provider": "test", "thinking_level": "off"})
    core.tasks.message(task["id"], "Two", "second")
    await core.coordinator.tick()
    assert [(c["model"], c["thinkingLevel"]) for c in calls] == [("deep", "high"), ("quick", "off")]


async def test_auto_blocks_destructive_commands_even_with_broad_grants_and_rules(application, monkeypatch):
    core = application.state.core
    task = core.tasks.create("Change files", grants={"computer": True})
    payload = action(core, task, "run_command", {"command": "rm -rf old-project"})
    key, label = rule_for("run_command", payload["args"])
    core.db.execute("INSERT INTO approval_rules VALUES ('broad','always','',?,?,0,'original')", (key, label))
    assert (await core.action(payload))["status"] == "pending"
    with pytest.raises(ValueError, match="separate decision"):
        core.approvals.decide(payload["id"], True, "always")
    core.approvals.decide(payload["id"], False)
    core.tasks.options(task["id"], {"approval_mode": "full"})
    assert (await core.action(payload))["status"] == "denied"
    assert not core.running_actions


async def test_auto_rechecks_old_blanket_approval(application):
    core = application.state.core
    task = core.tasks.create("Change files", grants={"computer": True})
    payload = action(core, task, "run_command", {"command": "git diff --output=important-file"})
    assert (await core.action(payload))["status"] == "pending"
    core.db.execute("DELETE FROM action_reviews WHERE action_id=?", (payload["id"],))
    core.db.execute("UPDATE actions SET status='approved' WHERE id=?", (payload["id"],))
    assert (await core.action(payload))["status"] == "pending"
    assert not core.running_actions


@pytest.mark.parametrize("code,expected", [(400, "failed"), (423, "pending")])
async def test_invalid_browser_target_fails_but_busy_browser_waits(application, monkeypatch, code, expected):
    core = application.state.core
    task = core.tasks.create("Browse")

    async def remote(service, path, body=None, **kwargs):
        if path == "/assess":
            httpx.Response(code, request=httpx.Request("POST", "http://browser/assess")).raise_for_status()
        assert path in {"/lease", "/agent/release"}
        return {}

    monkeypatch.setattr(core, "remote", remote)
    payload = action(core, task, "browser", {"operation": "click", "selector": "missing"})
    assert (await core.action(payload))["status"] == expected
    assert (await core.action(payload))["status"] == expected
    assert not core.running_actions


async def test_auto_browser_uses_live_assessment_and_rechecks_changed_target(application, monkeypatch):
    core = application.state.core
    task = core.tasks.create("Browse")
    revision, safe, calls = "first", True, []

    async def remote(service, path, body=None, **kwargs):
        if path == "/lease":
            return {"lease": None}
        if path == "/assess":
            return {"safe": safe, "context": revision, "reason": "Review this control."}
        if path == "/execute":
            calls.append(body)
            if not safe and body.get("approval_context") != revision:
                return {
                    "status": "approval_required",
                    "safe": False,
                    "context": revision,
                    "reason": "The target changed.",
                }
            return {"text": "Done"}
        return {}

    monkeypatch.setattr(core, "remote", remote)
    payload = action(core, task, "browser", {"operation": "click", "selector": "button"})
    assert (await core.action(payload))["status"] == "executing"
    safe = False  # Changed after assessment but before execution.
    await asyncio.gather(*core.running_actions.values())
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)
    revision = "changed-again"
    assert (await core.action(payload))["status"] == "executing"
    await asyncio.gather(*core.running_actions.values())
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)
    await core.action(payload)
    await asyncio.gather(*core.running_actions.values())
    assert (await core.action(payload))["status"] == "completed"
    assert calls[-1]["approval_context"] == revision
    assert len(calls) == 3
    await core.action(payload)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "url,safe",
    [
        ("https://example.com/docs", True),
        ("https://www.google.com/search?q=delete+old+files", True),
        ("https://example.com/account/delete", False),
        ("https://example.com/settings?action=delete", False),
        ("javascript:alert(1)", False),
        ("https://user:password@example.com", False),
    ],
)
def test_navigation_policy(url, safe):
    assert navigation_safe(url) is safe


def test_google_search_is_allowed_but_post_and_sensitive_forms_are_not():
    target = {
        "url": "https://www.google.com/",
        "action": "https://www.google.com/search",
        "method": "get",
        "search_form": True,
        "search_input": True,
        "tag": "input",
        "type": "submit",
        "label": "Google Search",
    }
    for args in ({"operation": "click"}, {"operation": "fill"}, {"operation": "press", "key": "Enter"}):
        assert classify(args, target)[0]
        assert not classify(args, {**target, "method": "post"})[0]
        assert not classify(args, {**target, "sensitive_form": True})[0]
    assert not classify({"operation": "click"}, {**target, "label": "Delete account"})[0]
    assert not classify({"operation": "click"}, {"label": "Search", "tag": "button", "type": "button"})[0]
