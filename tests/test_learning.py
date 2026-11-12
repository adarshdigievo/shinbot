import json
import time

import pytest

from shinbot.db import packed, uid
from shinbot.results import RecentResults, without_images
from shinbot.services.common import Journal


def evidence(core, bot="default", status="completed", result=None):
    task = core.tasks.create("Solve a problem", bot_id=bot)
    row = core.db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    key = uid()
    core.db.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            key,
            task["id"],
            core.db.setting("epoch"),
            "run_command",
            "{}",
            "hash",
            status,
            packed(result or {"exit_code": 0, "output": "verified"}),
            None,
            time.time(),
            time.time() + 100,
        ),
    )
    return row, key


def propose(
    core,
    task,
    key,
    name="safe-search",
    description="Find a document and verify its source before quoting it.",
):
    return core.learning.propose(
        task,
        name,
        description,
        "Find the original document. Read the matching passage and check the publication date. Save the source URL and verify every quoted statement against the source.",
        [key],
    )


def test_learned_skill_evidence_scope_versions_and_review(application, owner):
    core = application.state.core
    task, key = evidence(core)
    first = propose(core, task, key)["installed"]
    assert first["status"] == "staged" and first["access"]["bots"] == ["default"]
    preview = owner.get(f"/api/plugins/{first['id']}/{first['version']}/files").json()
    assert json.loads(preview["files"]["LEARNING.json"])["evidence"][0]["id"] == key
    assert "hash" in preview["files"]["LEARNING.json"] and "verified" not in preview["files"]["LEARNING.json"]
    assert propose(core, task, key)["unchanged"]
    core.plugins.activate(first["id"], first["version"])
    task2, key2 = evidence(core)
    second = propose(
        core, task2, key2, description="Find original documents and check all of the quoted claims."
    )["installed"]
    assert second["version"] != first["version"] and second["status"] == "staged"
    core.plugins.activate(second["id"], second["version"])
    core.plugins.activate(first["id"], first["version"])
    assert (
        next(p for p in core.plugins.list() if p["status"] == "active" and p["id"] == first["id"])["version"]
        == first["version"]
    )
    other = core.bots.save({"name": "Other"})["id"]
    assert not core.plugins.allowed(first["id"], other)
    core.plugins.disable(first["id"])
    task3, key3 = evidence(core)
    with pytest.raises(ValueError, match="disabled"):
        propose(core, task3, key3)


def test_learning_limits_and_invalid_evidence(application):
    core = application.state.core
    task, key = evidence(core)
    other, other_key = evidence(core)
    with pytest.raises(ValueError, match="Evidence must"):
        propose(core, task, other_key)
    failed, failed_key = evidence(core, result={"exit_code": 1})
    with pytest.raises(ValueError, match="successful"):
        propose(core, failed, failed_key)
    propose(core, task, key)
    with pytest.raises(ValueError, match="one proposal"):
        propose(core, task, key, name="different-procedure")
    propose(core, other, other_key, name="second-procedure")
    third, third_key = evidence(core)
    propose(core, third, third_key, name="third-procedure")
    fourth, fourth_key = evidence(core)
    with pytest.raises(ValueError, match="three per bot"):
        propose(core, fourth, fourth_key, name="fourth-procedure")
    core.db.set("preferences", {**core.db.setting("preferences"), "learn_from_tasks": False})
    with pytest.raises(ValueError, match="disabled"):
        propose(core, fourth, fourth_key)


def test_transient_image_cache_bounds_and_durable_replay(tmp_path):
    image = {
        "content": [
            {"type": "text", "text": "Clicked once"},
            {"type": "image", "data": "A" * 1000, "mimeType": "image/jpeg"},
        ]
    }
    cache = RecentResults(limit=2000)
    safe = cache.put("a", image)
    assert safe == without_images(image) and "Clicked once" in packed(safe)
    assert cache.get("a", safe) == image
    cache.put("b", image)
    assert cache.bytes <= 2000 and cache.get("a", safe) == safe
    journal = Journal(tmp_path)
    payload = {"id": "click", "epoch": "x"}
    journal.claim(payload)
    journal.finish("click", image)
    assert journal.lookup(payload) == image
    assert len(journal.db.execute("SELECT result FROM effects").fetchone()[0]) < 300
    journal.db.close()
    reopened = Journal(tmp_path)
    assert reopened.claim(payload) == safe  # Replay never executes the click again.
    reopened.db.close()


async def test_preference_learning_opt_in_scope_receipts_and_forgetting(application, owner):
    import asyncio

    from test_actions import action

    core = application.state.core
    settings = owner.get("/api/settings").json()
    assert settings["learn_preferences"]
    owner.put("/api/settings", json={**settings, "learn_preferences": False}).raise_for_status()
    before = core.tasks.detail(core.tasks.create("I prefer short answers")["id"])
    assert core.approval_required("memory", {"operation": "remember"}, before["grants"])
    settings = owner.get("/api/settings").json()
    assert not settings["learn_preferences"]
    settings["learn_preferences"] = True
    assert owner.put("/api/settings", json=settings).status_code == 200
    task = core.tasks.detail(core.tasks.create("I prefer short answers")["id"])
    assert task["grants"]["memory"]
    payload = action(core, task, "memory", {"operation": "remember", "text": "Prefers short answers"})
    assert (await core.action(payload))["status"] == "executing"
    await asyncio.gather(*core.running_actions.values())
    assert (await core.action(payload))["status"] == "completed"
    memories = core.notebook.memories(bot_id="default")
    assert len(memories) == 1 and memories[0]["source"] == f"task:{task['id']}"
    other = core.bots.save({"name": "Another bot"})["id"]
    assert core.notebook.memories(bot_id=other) == []
    assert "Prefers short answers" in core.instructions(core.tasks.detail(core.tasks.create("hello")["id"]))
    assert "Prefers short answers" not in core.instructions(
        core.tasks.detail(core.tasks.create("hello", bot_id=other)["id"])
    )
    assert core.approval_required("memory", {"operation": "remember"}, before["grants"])
    core.notebook.forget(memories[0]["id"], "default")
    with pytest.raises(ValueError, match="forgot"):
        core.notebook.remember("Prefers short answers", source=f"task:{task['id']}")
    settings["learn_preferences"] = False
    owner.put("/api/settings", json=settings).raise_for_status()
    assert not core.tasks.detail(core.tasks.create("hello")["id"])["grants"]["memory"]


@pytest.mark.parametrize("tool", ["shinbot_help", "updates"])
def test_product_help_is_not_evidence_of_a_tested_procedure(application, tool):
    core = application.state.core
    task, key = evidence(core)
    core.db.execute("UPDATE actions SET tool=? WHERE id=?", (tool, key))
    with pytest.raises(ValueError, match="Evidence must"):
        propose(core, task, key)
