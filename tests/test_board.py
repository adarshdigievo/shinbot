import json

import pytest

from shinbot.core import Core
from shinbot.plugins import validate_package


@pytest.fixture
def board_app(application):
    core = application.state.core
    core.plugins.stage_from_catalog("official", "kanban", "1.0.0")
    core.plugins.activate("kanban", "1.0.0")
    return application


def start(core, **overrides):
    card = core.board.save(
        {"title": "Research a trip", "goal": "Compare three routes and verify dates.", **overrides}
    )
    core.board.move(card["id"], "ready", card["revision"])
    core.board.tick()
    return core.board.get(card["id"])


def finish(core, card, outcome="continue", tokens=100, **notes):
    task = core.db.one("SELECT * FROM tasks WHERE id=?", (card["task_id"],))
    core.board.finish_run(
        task,
        outcome,
        notes.get("summary", "Compared two routes."),
        notes.get("evidence", "Saved route sources."),
        notes.get("next_step", "Check the third route."),
    )
    core.db.execute("UPDATE tasks SET status='completed',tokens=? WHERE id=?", (tokens, task["id"]))
    core.db.execute("UPDATE messages SET status='completed' WHERE task_id=?", (task["id"],))
    return task


def test_toolkit_is_opt_in_and_cannot_be_replaced(application):
    core = application.state.core
    assert core.plugins.versions() == []
    assert not core.db.one("SELECT 1 FROM plugins WHERE id IN ('kanban','projects')")
    core.plugins.stage_from_catalog("official", "kanban", "1.0.0")
    core.plugins.activate("kanban", "1.0.0")
    assert core.db.one("SELECT status FROM plugins WHERE id='serpapi'") is None
    _, files = core.plugins.package("kanban", "1.0.0")
    manifest = json.loads(files["shinbot-plugin.json"])
    manifest["tools"][0]["name"] = "run_untrusted_code"
    files["shinbot-plugin.json"] = json.dumps(manifest).encode()
    with pytest.raises(ValueError, match="exactly"):
        validate_package(files)
    core.plugins.disable("kanban")
    reopened = Core(core.config)
    assert reopened.plugins.versions() == []


def test_missing_model_blocks_only_affected_card(board_app):
    core = board_app.state.core
    prefs = core.db.setting("preferences")
    core.db.set("preferences", {**prefs, "provider": "", "model": ""})
    configured = core.bots.save({"name": "Configured", "provider": "fixture", "model": "fixture"})
    blocked = core.board.save({"title": "Needs setup", "goal": "Check a document"})
    ready = core.board.save({"title": "Can run", "goal": "Check a document", "bot_id": configured["id"]})
    for card in (blocked, ready):
        core.board.move(card["id"], "ready", card["revision"])
    core.board.tick()
    blocked = core.board.get(blocked["id"])
    assert blocked["status"] == "blocked" and "Choose a model" in blocked["summary"]
    assert blocked["runs"] == 0
    assert core.board.get(ready["id"])["status"] == "running"
    core.db.set("preferences", prefs)
    core.board.move(blocked["id"], "ready", blocked["revision"])
    core.board.tick()
    assert core.board.get(blocked["id"])["status"] == "running"


def test_restart_resume_run_budget_and_evidence_review(board_app):
    core = board_app.state.core
    card = start(core, max_runs=2)
    first = card["task_id"]
    core.board.tick()
    assert core.board.get(card["id"])["task_id"] == first
    finish(core, card)
    core = Core(core.config)
    core.board.tick()
    card = core.board.get(card["id"])
    assert card["runs"] == 2 and card["task_id"] != first and card["tokens"] == 100
    finish(core, card)
    core.board.tick()
    card = core.board.get(card["id"])
    assert card["status"] == "blocked" and card["runs"] == 2 and card["tokens"] == 200
    with pytest.raises(ValueError, match="budget"):
        core.board.move(card["id"], "ready", card["revision"])
    card = core.board.save({**card, "max_runs": 3}, card["id"])
    core.board.move(card["id"], "ready", card["revision"])
    core.board.tick()
    card = core.board.get(card["id"])
    finish(core, card, "completed")
    core.board.tick()
    card = core.board.get(card["id"])
    assert card["status"] == "review"
    assert len(core.board.history(card["id"])) == 3
    core.board.move(card["id"], "done", card["revision"])
    assert core.board.get(card["id"])["status"] == "done"


def test_unreported_success_and_failure_do_not_loop(board_app):
    core = board_app.state.core
    card = start(core)
    core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (card["task_id"],))
    core.board.tick()
    assert core.board.get(card["id"])["status"] == "review"
    other = start(core)
    core.db.execute(
        "UPDATE tasks SET status='failed',error='Provider unavailable' WHERE id=?", (other["task_id"],)
    )
    core.board.tick()
    assert core.board.get(other["id"])["status"] == "blocked"
    assert core.board.get(other["id"])["summary"] == "Provider unavailable"


def test_one_card_per_bot_scope_and_revision_conflict(board_app):
    core = board_app.state.core
    first = start(core)
    second = start(core)
    assert second["status"] == "ready"
    with pytest.raises(ValueError, match="this bot"):
        core.board.get(first["id"], "another-bot")
    assert core.board.list("another-bot") == []
    with pytest.raises(ValueError, match="changed"):
        core.board.move(second["id"], "backlog", second["revision"] - 1)
    task = core.db.one("SELECT * FROM tasks WHERE id=?", (first["task_id"],))
    with pytest.raises(ValueError, match="summary"):
        core.board.finish_run(task, "completed", "Done", "")
    assert not core.approval_required("plugin:kanban:1.0.0:list_cards", {}, {})


def test_token_budget_and_long_goal_prompt(board_app):
    core = board_app.state.core
    card = start(core, max_tokens=1000, goal="g" * 12000)
    task = core.db.one("SELECT * FROM tasks WHERE id=?", (card["task_id"],))
    assert task["max_tokens"] == 1000
    finish(core, card, tokens=1000, summary="s" * 6000, evidence="e" * 6000, next_step="n" * 6000)
    core.board.tick()
    assert core.board.get(card["id"])["status"] == "blocked"
    card = core.board.get(card["id"])
    core.board.save({**card, "max_tokens": 2000}, card["id"])
    card = core.board.get(card["id"])
    core.board.move(card["id"], "ready", card["revision"])
    core.board.tick()
    card = core.board.get(card["id"])
    prompt = json.loads(
        core.db.one("SELECT payload FROM messages WHERE task_id=?", (card["task_id"],))["payload"]
    )["content"]
    assert "g" * 12000 in prompt
    assert "finish_run" in prompt and len(prompt) <= 20000


def test_unlimited_default_continues_after_restart_without_losing_usage(board_app):
    core = board_app.state.core
    card = start(core, max_runs=3)
    assert card["max_tokens"] == 0
    assert core.db.one("SELECT max_tokens FROM tasks WHERE id=?", (card["task_id"],))["max_tokens"] == 0
    finish(core, card, tokens=300000)
    core = Core(core.config)
    core.board.tick()
    card = core.board.get(card["id"])
    assert (card["status"], card["runs"], card["tokens"], card["max_tokens"]) == ("running", 2, 300000, 0)
    assert core.db.one("SELECT max_tokens FROM tasks WHERE id=?", (card["task_id"],))["max_tokens"] == 0
    finish(core, card, "completed", tokens=400000)
    core.board.tick()
    card = core.board.get(card["id"])
    assert card["status"] == "review" and card["tokens"] == 700000
    assert len(core.board.history(card["id"])) == 2


@pytest.mark.parametrize(
    "workspace_cap,card_cap,expected",
    [(0, 0, 0), (2000, 0, 2000), (0, 3000, 3000), (2000, 3000, 2000), (4000, 3000, 3000)],
)
def test_card_and_workspace_caps_use_smallest_enabled_limit(board_app, workspace_cap, card_cap, expected):
    core = board_app.state.core
    core.db.set("preferences", {**core.db.setting("preferences"), "task_tokens": workspace_cap})
    card = start(core, max_tokens=card_cap)
    assert (
        core.db.one("SELECT max_tokens FROM tasks WHERE id=?", (card["task_id"],))["max_tokens"] == expected
    )
    finish(core, card, tokens=1000)
    core.board.tick()
    card = core.board.get(card["id"])
    remaining = min([v for v in (workspace_cap, card_cap - 1000) if v > 0], default=0)
    assert card["status"] == "running"
    assert (
        core.db.one("SELECT max_tokens FROM tasks WHERE id=?", (card["task_id"],))["max_tokens"] == remaining
    )


def test_clear_exhausted_card_cap_and_plugin_tool_schema(board_app, owner):
    from jsonschema import validate

    core = board_app.state.core
    card = start(core, max_tokens=1000)
    finish(core, card, tokens=1250)
    core.board.tick()
    card = core.board.get(card["id"])
    assert card["status"] == "blocked"
    # Ordinary edits preserve existing explicit caps.
    updated = owner.post(
        "/api/board", json={"id": card["id"], "revision": card["revision"], "title": "New title"}
    )
    assert updated.status_code == 200 and updated.json()["max_tokens"] == 1000
    card = updated.json()
    core.plugins.stage_from_catalog("official", "kanban", "1.1.0")
    core.plugins.activate("kanban", "1.1.0")
    _, files = core.plugins.package("kanban", "1.1.0")
    schema = next(t for t in json.loads(files["shinbot-plugin.json"])["tools"] if t["name"] == "save_card")[
        "input_schema"
    ]
    args = {
        "id": card["id"],
        "revision": card["revision"],
        "title": card["title"],
        "goal": card["goal"],
        "max_tokens": 0,
    }
    validate(args, schema)
    card = core.board.tool("save_card", args, {"bot_id": "default"})
    core = Core(core.config)
    core.board.move(card["id"], "ready", card["revision"])
    core.board.tick()
    card = core.board.get(card["id"])
    assert (card["status"], card["runs"], card["tokens"], card["max_tokens"]) == ("running", 2, 1250, 0)
    assert core.db.one("SELECT max_tokens FROM tasks WHERE id=?", (card["task_id"],))["max_tokens"] == 0


@pytest.mark.parametrize("tokens", [-1, 1, 999, 10000001, True, None, "1000"])
def test_invalid_card_caps_are_rejected(board_app, tokens):
    with pytest.raises(ValueError, match="Use 0 for no token limit"):
        start(board_app.state.core, max_tokens=tokens)


async def test_owner_api_auth_pause_cancels_and_hold(owner, board_app, monkeypatch):
    core = board_app.state.core
    calls = []

    async def remote(service, path, body=None, **kwargs):
        calls.append((service, path))
        return {}

    async def runtime(*args, **kwargs):
        return {}

    monkeypatch.setattr(core, "remote", remote)
    monkeypatch.setattr(core.coordinator, "request", runtime)
    card = start(core)
    response = owner.post(
        f"/api/board/{card['id']}/move", json={"status": "blocked", "revision": card["revision"]}
    )
    assert response.status_code == 200, response.text
    assert ("computer", "/cancel") in calls and ("browser", "/cancel") in calls
    assert core.db.one("SELECT status FROM tasks WHERE id=?", (card["task_id"],))["status"] == "cancelled"
    core.board.tick()
    assert core.board.get(card["id"])["status"] == "blocked"
    core.db.set("restore_hold", True)
    card = core.board.get(card["id"])
    assert (
        owner.post(
            f"/api/board/{card['id']}/move", json={"status": "ready", "revision": card["revision"]}
        ).status_code
        == 409
    )
    owner.headers.pop("Authorization")
    assert owner.get("/api/board").status_code == 401
