import json

import httpx
import pytest
from fastapi import HTTPException

from shinbot import __version__
from shinbot.db import uid
from shinbot.plugins import validate_package
from shinbot.self_knowledge import help_topic
from shinbot.services.worker import mcp_operation


async def invoke(core, task, tool, args):
    payload = {
        "id": uid(),
        "task_id": task["id"],
        "epoch": core.db.setting("epoch"),
        "tool": tool,
        "args": args,
    }
    result = await core.action(payload)
    if result["status"] == "executing":
        await core.running_actions[payload["id"]]
        result = await core.action(payload)
    return result, payload


async def test_status_reports_actual_identity_model_other_bots_and_private_browser(application, monkeypatch):
    core = application.state.core
    bot = core.bots.save(
        {
            "name": "Atlas",
            "description": "Research assistant",
            "provider": "custom-local",
            "model": "local-model",
            "browser": "private",
        }
    )
    reviewer = core.bots.save({"name": "Code reviewer", "description": "Review changes", "browser": "shared"})
    task = core.tasks.create("Who are you?", bot_id=bot["id"])

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("Browser offline")

    monkeypatch.setattr(core, "remote", unavailable)
    result, _ = await invoke(core, task, "status", {})
    data = result["result"]
    assert result["status"] == "completed"
    assert data["name"] == "Atlas" and data["product"] == "ShinBot" and data["version"] == __version__
    assert data["model"] == {"provider": "custom-local", "id": "local-model"}
    assert data["browser_profile"] == "bot-" + bot["id"]
    assert next(b for b in data["other_bots"] if b["id"] == reviewer["id"])["name"] == "Code reviewer"
    assert not data["browser"]["available"]
    assert not any("delegation" in item for item in data["capabilities"])
    serialized = json.dumps(data)
    for secret in [core.config.service_token, core.config.computer_token, core.auth.bootstrap]:
        assert secret not in serialized


async def test_status_stops_advertising_revoked_and_unselected_native_plugins(application, monkeypatch):
    core = application.state.core

    async def remote(*args, **kwargs):
        return {}

    monkeypatch.setattr(core, "remote", remote)
    old = core.tasks.create("Existing conversation")
    core.plugins.stage_from_catalog("official", "projects", "1.0.0")
    core.plugins.activate("projects", "1.0.0")
    result, _ = await invoke(core, old, "status", {})
    assert "projects" not in result["result"]["usable_plugin_ids"]
    new = core.tasks.create("New conversation")
    result, _ = await invoke(core, new, "status", {})
    assert "projects" in result["result"]["usable_plugin_ids"]
    other = core.bots.save({"name": "Other"})["id"]
    core.plugins.set_access("projects", {"mode": "bots", "bots": [other]})
    result, _ = await invoke(core, new, "status", {})
    assert "projects" not in result["result"]["usable_plugin_ids"]
    assert not any("delegation" in item for item in result["result"]["capabilities"])
    core.plugins.set_access("projects", {"mode": "shared", "bots": []})
    core.plugins.disable("projects")
    result, _ = await invoke(core, new, "status", {})
    assert "projects" not in result["result"]["usable_plugin_ids"]


async def test_authoring_guide_produces_executable_mcp_and_activation_is_approved(
    application, tmp_path, monkeypatch
):
    core = application.state.core
    task = core.tasks.create("Build a plugin")
    result, _ = await invoke(core, task, "shinbot_help", {"topic": "plugins"})
    files = result["result"]["example_files"]
    manifest, _ = validate_package({name: text.encode() for name, text in files.items()})
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    # This runs the guide's actual server, discovers its tools and verifies the declared schema.
    output = await mcp_operation(
        {
            "manifest": manifest.model_dump(),
            "directory": str(tmp_path),
            "name": "count_words",
            "arguments": {"text": "three small words"},
        }
    )
    assert json.loads(output["content"][0]["text"]) == {"words": 3}
    result, _ = await invoke(core, task, "plugins", {"operation": "stage", "files": files})
    assert result["status"] == "completed"
    assert core.plugins.access("text-helper") == {"mode": "bots", "bots": ["default"]}
    approval, payload = await invoke(
        core, task, "plugins", {"operation": "activate", "id": "text-helper", "version": "1.0.0"}
    )
    assert approval["status"] == "pending"
    assert not core.plugins.versions()
    assert "plugin:text-helper:1.0.0" not in json.loads(
        core.db.one("SELECT extensions FROM tasks WHERE id=?", (task["id"],))["extensions"]
    )


async def test_help_and_updates_tools_are_read_only_and_cannot_apply_updates(application):
    core = application.state.core
    task = core.tasks.create("How do I update?")
    for topic in ["overview", "bots", "approvals", "storage", "updates"]:
        result, _ = await invoke(core, task, "shinbot_help", {"topic": topic})
        assert result["status"] == "completed"
    result, _ = await invoke(core, task, "updates", {"operation": "status"})
    assert result["result"]["status"] == "unchecked"
    result, _ = await invoke(core, task, "updates", {"operation": "install"})
    assert result["status"] == "failed"
    assert "host CLI" in result["error"]
    assert len(core.bots.list()) == 1
    assert core.notebook.search() == []


@pytest.mark.parametrize("path", ["/workspace/plugin/server.py", "../outside", "plugin/../outside"])
async def test_invalid_write_is_rejected_before_an_execution_receipt(
    application, tmp_path, monkeypatch, path
):
    from shinbot.services import computer
    from shinbot.services.common import Journal

    journal = Journal(tmp_path / "executor")
    monkeypatch.setattr(computer, "journal", journal)
    core = application.state.core
    task = core.tasks.create("Create a plugin", options={"approval_mode": "full"})

    async def remote(service, route, body=None, **kwargs):
        if route == "/execute":
            try:
                return await computer.execute(body)
            except HTTPException as exc:
                response = httpx.Response(
                    exc.status_code,
                    json={"detail": exc.detail},
                    request=httpx.Request("POST", "http://computer/execute"),
                )
                response.raise_for_status()
        return {}

    monkeypatch.setattr(core, "remote", remote)
    result, payload = await invoke(core, task, "write_file", {"path": path, "content": "safe"})
    assert result["status"] == "failed" and "relative workspace path" in result["error"]
    assert journal.receipt(payload["id"])["status"] == "missing"
    assert core.tasks.detail(task["id"])["status"] != "uncertain"
    journal.db.close()


async def test_stage_rejects_workspace_paths_with_an_actionable_error(application):
    core = application.state.core
    task = core.tasks.create("Create a plugin")
    result, _ = await invoke(
        core, task, "plugins", {"operation": "stage", "files": {"plugins/example/shinbot-plugin.json": "{}"}}
    )
    assert result["status"] == "failed"
    assert "Nothing was staged" in result["error"] and "package-relative" in result["error"]
    assert not core.db.one("SELECT id FROM plugins WHERE id='example'")


@pytest.mark.parametrize("target", ["directory", "parent-file", "symlink", "missing-worktree"])
async def test_file_target_rejection_is_definite_and_survives_replay(tmp_path, monkeypatch, target):
    from shinbot.services import computer
    from shinbot.services.common import Journal

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "directory").mkdir()
    (root / "parent-file").write_text("preserve")
    (root / "symlink").symlink_to(root / "parent-file")
    monkeypatch.setattr(computer, "ROOT", root)
    journal = Journal(tmp_path / "executor")
    monkeypatch.setattr(computer, "journal", journal)
    paths = {
        "directory": "directory",
        "parent-file": "parent-file/child",
        "symlink": "symlink",
        "missing-worktree": "file",
    }
    payload = {
        "id": uid(),
        "task_id": uid(),
        "epoch": "test",
        "tool": "write_file",
        "args": {"path": paths[target], "content": "new"},
    }
    if target == "missing-worktree":
        payload["workspace_path"] = "no-worktree"
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            await computer.execute(payload.copy())
        assert error.value.status_code == 400
    assert (root / "parent-file").read_text() == "preserve"
    journal.db.close()
    reopened = Journal(tmp_path / "executor")
    assert reopened.receipt(payload["id"])["status"] == (
        "missing" if target == "missing-worktree" else "rejected"
    )
    reopened.db.close()


async def test_lost_rejection_response_recovers_as_failed_without_redispatch(application, monkeypatch):
    core = application.state.core
    task = core.tasks.create("Write a file", options={"approval_mode": "full"})
    dispatched = []

    async def remote(service, route, body=None, **kwargs):
        if route.startswith("/actions/"):
            return {"status": "rejected", "error": "File contents were not changed: target is a directory"}
        if route == "/execute":
            dispatched.append(body["id"])
            raise httpx.ReadError("lost response")
        return {}

    monkeypatch.setattr(core, "remote", remote)
    result, payload = await invoke(core, task, "write_file", {"path": "directory", "content": "new"})
    assert result["status"] == "retrying"
    core.db.execute("UPDATE action_retries SET next_attempt=0")
    await core.action(payload)
    await core.running_actions[payload["id"]]
    assert (await core.action(payload))["status"] == "failed"
    assert dispatched == [payload["id"]]
    assert core.tasks.detail(task["id"])["status"] != "uncertain"


def test_post_write_failure_keeps_an_uncertain_outcome(tmp_path, monkeypatch):
    import os

    from shinbot.services.worker import FileRejected, file_operation

    def disk_error(fd):
        raise OSError("disk sync failed")

    monkeypatch.setattr(os, "fsync", disk_error)
    with pytest.raises(OSError) as error:
        file_operation(tmp_path, "write_file", {"path": "written", "content": "effect happened"})
    assert not isinstance(error.value, FileRejected)
    assert (tmp_path / "written").read_text() == "effect happened"


async def test_stage_reads_tested_package_from_computer_without_running_it(
    application, tmp_path, monkeypatch
):
    from shinbot.services import computer

    core = application.state.core
    root = tmp_path / "workspace"
    directory = root / "plugins/text-helper"
    directory.mkdir(parents=True)
    files = help_topic("plugins")["example_files"]
    for name, content in files.items():
        (directory / name).write_text(content)
    monkeypatch.setattr(computer, "ROOT", root)
    calls = []

    async def remote(service, route, body=None, **kwargs):
        calls.append(route)
        assert route == "/plugins/package"
        return await computer.package_plugin(body)

    monkeypatch.setattr(core, "remote", remote)
    task = core.tasks.create("Stage the tested plugin")
    result, _ = await invoke(core, task, "plugins", {"operation": "stage", "path": "plugins/text-helper"})
    assert result["status"] == "completed" and result["result"]["status"] == "staged"
    assert core.plugins.access("text-helper")["bots"] == ["default"]
    assert calls == ["/plugins/package"] and not core.plugins.versions()
    _, staged = core.plugins.package("text-helper", "1.0.0")
    assert staged["server.py"] == files["server.py"].encode()


@pytest.mark.parametrize("unsafe", ["traversal", "symlink"])
def test_directory_staging_cannot_read_outside_workspace(tmp_path, unsafe):
    from shinbot.self_knowledge import help_topic
    from shinbot.services.worker import plugin_package

    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for name, content in help_topic("plugins")["example_files"].items():
        (outside / name).write_text(content)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        plugin_package(root, "../outside" if unsafe == "traversal" else "linked")


async def test_offline_package_read_does_not_claim_an_uncertain_install(application, monkeypatch):
    core = application.state.core

    async def unavailable(*args, **kwargs):
        raise httpx.ReadTimeout("offline")

    monkeypatch.setattr(core, "remote", unavailable)
    task = core.tasks.create("Stage a tested plugin")
    result, _ = await invoke(core, task, "plugins", {"operation": "stage", "path": "plugins/text-helper"})
    assert result["status"] == "failed" and "Nothing was staged" in result["error"]
    assert not core.db.one("SELECT id FROM plugins WHERE id='text-helper'")


async def test_rejected_directory_staging_reports_the_specific_fix(application, monkeypatch):
    core = application.state.core

    async def rejected(*args, **kwargs):
        response = httpx.Response(
            400,
            json={"detail": "Include the plugin's LICENSE file."},
            request=httpx.Request("POST", "http://computer/plugins/package"),
        )
        response.raise_for_status()

    monkeypatch.setattr(core, "remote", rejected)
    task = core.tasks.create("Stage a tested plugin")
    result, _ = await invoke(core, task, "plugins", {"operation": "stage", "path": "plugins/text-helper"})
    assert result["status"] == "failed"
    assert "Nothing was staged" in result["error"] and "LICENSE" in result["error"]
