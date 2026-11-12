import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from test_actions import action

from shinbot.hooks import HookBlocked
from shinbot.plugins import validate_package
from shinbot.security import Security
from shinbot.services import computer
from shinbot.services.common import Journal


def package(name="paint"):
    root = Path(__file__).resolve().parents[1] / "examples/plugins" / name
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


@pytest.fixture
def approved(monkeypatch):
    # Scanner behavior is covered by test_security; these tests exercise extension behavior.
    monkeypatch.setattr(Security, "scan", lambda *a, **kw: {"status": "passed"})


def test_custom_ui_manifest_and_permissions():
    manifest, _ = validate_package(package())
    assert manifest.kind == "extension" and manifest.ui.pages[0].title == "Paint"
    files = package()
    for change in (
        {"permissions": []},
        {"ui": {"pages": [{"id": "p", "title": "P", "entrypoint": "../bad.js"}]}},
        {"hooks": [{"event": "unknown", "entrypoint": "paint.js"}]},
    ):
        raw = json.loads(files["shinbot-plugin.json"])
        raw.update(change)
        invalid = {**files, "shinbot-plugin.json": json.dumps(raw).encode()}
        with pytest.raises(ValueError):
            validate_package(invalid)


def test_ui_asset_activation_scope_storage_and_disable(owner, application, approved):
    core = application.state.core
    core.plugins.stage(package())
    path = "/api/plugin-ui/default/paint/1.0.0"
    assert owner.get(path + "/assets/paint.js").status_code == 400
    core.plugins.activate("paint", "1.0.0")
    response = owner.get(path + "/assets/paint.js")
    assert response.status_code == 200 and "javascript" in response.headers["content-type"]
    assert owner.get(path + "/assets/LICENSE").status_code == 404
    assert owner.get(path + "/assets/%2e%2e/shinbot-plugin.json").status_code != 200
    assert owner.put(path + "/state/drawing", json={"value": "a drawing"}).status_code == 200
    assert owner.get(path + "/state/drawing").json()["value"] == "a drawing"
    other = core.bots.save({"name": "Other"})
    otherpath = f"/api/plugin-ui/{other['id']}/paint/1.0.0"
    assert owner.get(otherpath + "/state/drawing").json()["value"] is None
    core.plugins.set_access("paint", {"mode": "bots", "bots": ["default"]})
    assert owner.get(otherpath + "/assets/paint.js").status_code == 400
    core.plugins.disable("paint")
    assert owner.get(path + "/assets/paint.js").status_code == 400
    core.plugins.activate("paint", "1.0.0")
    assert owner.get(path + "/state/drawing").json()["value"] == "a drawing"


@pytest.fixture
def hook_core(application, approved, monkeypatch):
    core = application.state.core
    core.plugins.stage(package("workflow-hooks"))
    core.plugins.activate("workflow-hooks", "1.0.0")
    results = {}
    calls = []

    async def remote(service, path, body=None, **kwargs):
        if path.startswith("/actions/"):
            key = path.split("/")[-1]
            return (
                {"status": "completed", "result": results[key]} if key in results else {"status": "missing"}
            )
        if path == "/plugins/stage":
            return {"ok": True}
        if path == "/plugins/hook":
            calls.append(body)
            result = {"args": {"command": "rm -rf example"}} if body["event"] == "before_tool" else {}
            results[body["id"]] = result
            return result
        return {}

    monkeypatch.setattr(core, "remote", remote)
    return core, calls, results


async def test_before_tool_transformation_precedes_approval_and_is_not_repeated(hook_core):
    core, calls, _ = hook_core
    task = core.tasks.create("Inspect", grants={"computer": True})
    payload = action(core, task, "run_command", {"command": "pwd"})
    assert (await core.action(payload))["status"] == "pending"
    row = core.db.one("SELECT * FROM actions WHERE id=?", (payload["id"],))
    assert json.loads(row["args"]) == {"command": "rm -rf example"}
    assert (await core.action(payload))["status"] == "pending"
    assert len(calls) == 1
    core.plugins.disable("workflow-hooks")
    task = core.db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    assert (await core.hooks.run("before_tool", "other", task, {"tool": "status", "args": {}}))["args"] == {}
    assert len(calls) == 1


async def test_completed_hook_receipt_survives_gateway_loss_and_scope_change(hook_core):
    core, calls, results = hook_core
    task = core.tasks.create("Inspect")
    task = core.db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    data = {"tool": "run_command", "args": {"command": "pwd"}}
    result = await core.hooks.run("before_tool", "event", task, data)
    core.db.execute("DELETE FROM settings WHERE key LIKE 'hook-%'")
    assert await core.hooks.run("before_tool", "event", task, data) == result
    assert len(calls) == 1
    with pytest.raises(HookBlocked, match="inputs changed"):
        await core.hooks.run("before_tool", "event", task, {"tool": "status", "args": {}})


async def test_real_computer_hook_runs_once_and_does_not_receive_service_secrets(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    data = tmp_path / "computer"
    monkeypatch.setattr(computer, "ROOT", root)
    monkeypatch.setattr(computer, "DATA", data)
    journal = Journal(data)
    monkeypatch.setattr(computer, "journal", journal)
    monkeypatch.setenv("SHINBOT_EXECUTOR_TOKEN", "must-not-reach-hook")
    files = package("workflow-hooks")
    files["hooks.py"] = (
        b"""import os\nfrom pathlib import Path\ndef handle(event, context):\n    assert 'SHINBOT_EXECUTOR_TOKEN' not in os.environ\n    path=Path(context['data_dir'])/'count'\n    n=int(path.read_text()) if path.exists() else 0\n    path.write_text(str(n+1))\n    return {'context':'Hook ran'}\n"""
    )
    manifest, digest = validate_package(files)
    await computer.stage(
        {"files": {n: base64.b64encode(v).decode() for n, v in files.items()}, "digest": digest}
    )
    payload = {
        "id": "hook-test",
        "epoch": "test",
        "task_id": "test",
        "plugin_id": manifest.id,
        "version": manifest.version,
        "digest": digest,
        "event": "before_chat",
        "context": {"data": {}},
        "environment": {},
    }
    assert await computer.plugin_hook(payload) == {"context": "Hook ran"}
    assert await computer.plugin_hook(payload) == {"context": "Hook ran"}
    journal.db.close()
    journal = Journal(data)
    monkeypatch.setattr(computer, "journal", journal)
    assert await computer.plugin_hook(payload) == {"context": "Hook ran"}
    assert (root / ".shinbot/plugins/workflow-hooks/count").read_text() == "1"
    journal.db.close()


async def test_after_tool_failure_keeps_original_result(hook_core, monkeypatch):
    core, calls, _ = hook_core
    task = core.tasks.create("Read status")
    payload = action(core, task, "status", {})

    # Only after hooks affect this call.
    async def run(event, event_id, task, data):
        if event == "after_tool":
            raise HookBlocked("Hook failed")
        return data

    monkeypatch.setattr(core.hooks, "run", run)
    await core.action(payload)
    await asyncio.gather(*core.running_actions.values())
    result = await core.action(payload)
    assert result["status"] == "completed" and result["result"]
    await core.action(payload)
    assert len(core.db.all("SELECT * FROM events WHERE kind='plugin' AND title='Hook failed'")) == 1


async def test_hooks_wrap_chat_and_tool_loop_through_real_worker(
    application, tmp_path, approved, monkeypatch
):
    from test_coordinator import Runtime

    core = application.state.core
    root = tmp_path / "workspace"
    root.mkdir()
    data = tmp_path / "computer"
    journal = Journal(data)
    monkeypatch.setattr(computer, "ROOT", root)
    monkeypatch.setattr(computer, "DATA", data)
    monkeypatch.setattr(computer, "journal", journal)
    monkeypatch.setenv("SHINBOT_EXECUTOR_TOKEN", "c" * 40)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=computer.app),
        base_url="http://computer",
        headers={"Authorization": "Bearer " + "c" * 40},
    ) as client:

        async def remote(service, path, body=None, method="POST", **kwargs):
            if service != "computer":
                return {}
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(core, "remote", remote)
        files = package("workflow-hooks")
        files["hooks.py"] = (
            b"""from pathlib import Path\ndef handle(event, context):\n    path=Path(context['data_dir'])/'events'\n    with path.open('a') as file: file.write(event+'\\n')\n    if event=='before_chat': return {'context':'Use metric units.'}\n    if event=='after_tool': return {'result':{'hooked':True}}\n    return {}\n"""
        )
        core.plugins.stage(files)
        await core.activate("workflow-hooks", "1.0.0")
        runtime = Runtime()
        submissions = []

        async def request(method, path, **kwargs):
            if path == "/v1/submit":
                submissions.append(kwargs["json"])
            return await runtime(method, path, **kwargs)

        monkeypatch.setattr(core.coordinator, "request", request)
        task = core.tasks.create("Read status")
        await core.coordinator.tick()
        assert "Use metric units." in submissions[0]["instructions"]
        payload = action(core, task, "status", {})
        await core.action(payload)
        await asyncio.gather(*core.running_actions.values())
        assert (await core.action(payload))["result"] == {"hooked": True}
        assert (await core.action(payload))["result"] == {"hooked": True}
        runtime.submissions[1] = "done"
        await core.coordinator.tick()
        await core.coordinator.tick()
        assert (root / ".shinbot/plugins/workflow-hooks/events").read_text().splitlines() == [
            "before_chat",
            "before_tool",
            "after_tool",
            "after_response",
        ]
        assert core.tasks.detail(task["id"])["messages"][0]["answer"] == "done"
    journal.db.close()


async def test_interrupted_hook_is_not_repeated(tmp_path, monkeypatch):
    from fastapi import HTTPException

    root = tmp_path / "workspace"
    root.mkdir()
    data = tmp_path / "computer"
    journal = Journal(data)
    monkeypatch.setattr(computer, "ROOT", root)
    monkeypatch.setattr(computer, "DATA", data)
    monkeypatch.setattr(computer, "journal", journal)
    files = package("workflow-hooks")
    raw = json.loads(files["shinbot-plugin.json"])
    raw["hooks"] = [{"event": "before_chat", "entrypoint": "hooks.py", "timeout": 1}]
    files["shinbot-plugin.json"] = json.dumps(raw).encode()
    files["hooks.py"] = (
        b"import time\nfrom pathlib import Path\ndef handle(event,context):\n    with (Path(context['data_dir'])/'effects').open('a') as f: f.write('once\\n')\n    time.sleep(10)\n    return {}\n"
    )
    manifest, digest = validate_package(files)
    await computer.stage(
        {"files": {n: base64.b64encode(v).decode() for n, v in files.items()}, "digest": digest}
    )
    payload = {
        "id": "timeout-event",
        "epoch": "test",
        "task_id": "test",
        "plugin_id": manifest.id,
        "version": manifest.version,
        "digest": digest,
        "event": "before_chat",
        "context": {"data": {}},
        "environment": {},
    }
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            await computer.plugin_hook(payload)
        assert exc.value.status_code == 409
    assert journal.receipt("timeout-event")["status"] == "uncertain"
    assert (root / ".shinbot/plugins/workflow-hooks/effects").read_text() == "once\n"
    journal.db.close()


async def test_hook_block_stops_submission_with_a_helpful_error(application, approved, monkeypatch):
    core = application.state.core
    core.plugins.stage(package("workflow-hooks"))
    core.plugins.activate("workflow-hooks", "1.0.0")
    task = core.tasks.create("Blocked request")
    requests = []

    async def request(method, path, **kwargs):
        requests.append(path)
        return {"held": False} if path == "/v1/health" else {}

    async def run(*args, **kwargs):
        raise HookBlocked("Workflow needs a project first.")

    monkeypatch.setattr(core.coordinator, "request", request)
    monkeypatch.setattr(core.hooks, "run", run)
    await core.coordinator.tick()
    assert "/v1/submit" not in requests
    detail = core.tasks.detail(task["id"])
    assert detail["status"] == "failed"
    assert "project first" in detail["messages"][0]["error"]
