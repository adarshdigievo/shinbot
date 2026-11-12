"""Regressions found by the live UX audit. These describe the expected behavior."""

import base64
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def echo_files(server):
    manifest = {
        "schema_version": 1,
        "id": "syntax-echo",
        "name": "Syntax echo",
        "version": "1.0.0",
        "description": "A package with invalid Python",
        "kind": "mcp",
        "license": "MIT",
        "files": ["LICENSE", "server.py"],
        "entrypoint": "server.py",
        "tools": [{"name": "echo", "input_schema": {"type": "object"}}],
    }
    return {
        "shinbot-plugin.json": json.dumps(manifest),
        "LICENSE": (ROOT / "LICENSE").read_text(),
        "server.py": server,
    }


def test_staging_invalid_python_returns_a_correctable_error(owner):
    response = owner.post("/api/plugins/stage", json={"files": echo_files("def (:\n")})
    assert response.status_code == 400, response.text
    assert "server.py" in response.json()["detail"]


def finished_once_job(core):
    spec = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 120))
    job = core.jobs.save({"name": "Once", "prompt": "Hello", "kind": "once", "spec": spec, "timezone": "UTC"})
    core.jobs.tick(job["next_run"] + 1)
    return core.db.one("SELECT * FROM schedules WHERE id=?", (job["id"],))


def test_resuming_a_finished_one_time_job_does_not_report_it_scheduled(application):
    core = application.state.core
    job = finished_once_job(core)
    assert job["enabled"] == 0 and job["next_run"] is None
    with pytest.raises(ValueError):
        core.jobs.toggle(job["id"], True)
    job = core.db.one("SELECT * FROM schedules WHERE id=?", (job["id"],))
    assert not (job["enabled"] and job["next_run"] is None)


def test_job_without_a_model_explains_why_it_paused(application):
    core = application.state.core
    job = core.jobs.save(
        {"name": "Briefing", "prompt": "Hello", "kind": "interval", "spec": "60", "timezone": "UTC"}
    )
    core.db.set("preferences", {**core.db.setting("preferences"), "provider": "", "model": ""})
    core.jobs.tick(job["next_run"] + 1)
    event = core.db.one("SELECT title FROM events ORDER BY id DESC LIMIT 1")
    assert "model" in event["title"].lower(), event
    saved = core.db.one("SELECT * FROM schedules WHERE id=?", (job["id"],))
    assert not saved["enabled"] and "model" in saved["last_error"].lower()
    run = core.db.one("SELECT * FROM occurrences WHERE schedule_id=?", (job["id"],))
    assert run["status"] == "failed" and "model" in run["error"].lower()


async def test_computer_restage_preserves_readonly_files(tmp_path, monkeypatch):
    from shinbot.plugins import validate_package
    from shinbot.services import computer

    monkeypatch.setattr(computer, "DATA", tmp_path)
    files = {name: text.encode() for name, text in echo_files("print('fixture')\n").items()}
    _, digest = validate_package(files)

    async def discovered(*args, **kwargs):
        return {"tools": [{"name": "echo"}]}

    monkeypatch.setattr(computer, "mcp_call", discovered)
    payload = {
        "files": {name: base64.b64encode(value).decode() for name, value in files.items()},
        "digest": digest,
    }
    await computer.stage(payload)
    directory = tmp_path / "plugins/syntax-echo/1.0.0"
    before = {p.name: p.stat().st_mtime_ns for p in directory.iterdir()}
    await computer.stage(payload)
    assert {p.name: p.stat().st_mtime_ns for p in directory.iterdir()} == before
    assert all(p.stat().st_mode & 0o222 == 0 for p in directory.iterdir())


def test_job_archive_cancels_queued_work_and_preserves_running_history(owner, application):
    core = application.state.core
    job = core.jobs.save(
        {"name": "Archive me", "prompt": "Hello", "kind": "interval", "spec": "60", "timezone": "UTC"}
    )
    task_id = core.jobs.tick(job["next_run"] + 1)[0]
    assert owner.delete("/api/jobs/" + job["id"]).status_code == 200
    assert owner.get("/api/jobs").json() == []
    assert owner.get("/api/jobs?archived=true").json()[0]["id"] == job["id"]
    assert core.tasks.detail(task_id)["status"] == "cancelled"
    assert core.jobs.admit(job, job["next_run"] + 120) is None
    run = owner.get(f"/api/jobs/{job['id']}/history").json()[0]
    assert run["task_id"] == task_id and run["task_status"] == "cancelled"
    assert owner.post(f"/api/jobs/{job['id']}/restore").status_code == 200
    restored = owner.get("/api/jobs").json()[0]
    assert not restored["enabled"] and not restored["archived"]
    core.jobs.toggle(job["id"], True)
    active = core.db.one("SELECT * FROM schedules WHERE id=?", (job["id"],))
    running = core.jobs.tick(active["next_run"] + 1)[0]
    core.db.execute("UPDATE tasks SET status='running' WHERE id=?", (running,))
    owner.delete("/api/jobs/" + job["id"])
    assert core.tasks.detail(running)["status"] == "running"


def test_reschedule_finished_job_explicitly_enables_new_time(application):
    core = application.state.core
    job = finished_once_job(core)
    future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
    saved = core.jobs.save({"spec": future, "enabled": True}, job["id"])
    assert saved["enabled"] and saved["next_run"] > time.time()


def test_missing_required_plugin_settings_hide_tools_for_new_chats(application):
    plugins = application.state.core.plugins
    files = echo_files('print("fixture")\n')
    manifest = json.loads(files["shinbot-plugin.json"])
    manifest["configuration"] = [
        {"key": "SHINBOT_PLUGIN_TOKEN", "label": "Token", "secret": True, "required": True}
    ]
    files["shinbot-plugin.json"] = json.dumps(manifest)
    plugins.stage({name: content.encode() for name, content in files.items()})
    plugins.configuration("syntax-echo", "1.0.0", {"SHINBOT_PLUGIN_TOKEN": "test-value"})
    plugins.activate("syntax-echo", "1.0.0")
    assert "plugin:syntax-echo:1.0.0" in plugins.versions()
    plugins.configuration("syntax-echo", "1.0.0", {"SHINBOT_PLUGIN_TOKEN": ""})
    assert "plugin:syntax-echo:1.0.0" not in plugins.versions()
    with pytest.raises(ValueError, match="Connection"):
        plugins.configuration("syntax-echo", "1.0.0", environment=True)


def test_template_reinstall_names_are_distinct_and_retry_is_idempotent(application):
    core = application.state.core
    template = next(
        t for market in core.bots.templates() for t in market["templates"] if t["kind"] == "group"
    )
    first = core.bots.install(template, "first")
    second = core.bots.install(template, "second")
    assert core.bots.install(template, "second") == second
    groups = core.db.all(
        "SELECT name FROM bot_groups WHERE id IN (?,?)", (first["group_id"], second["group_id"])
    )
    assert len({g["name"] for g in groups}) == 2
    names = [core.bots.get(key)["name"] for key in first["bot_ids"] + second["bot_ids"]]
    assert len(set(names)) == len(names)


def test_owner_validation_errors_explain_field_without_internal_links(owner):
    files = echo_files('print("fixture")\n')
    manifest = json.loads(files["shinbot-plugin.json"])
    manifest["id"] = "BAD ID"
    files["shinbot-plugin.json"] = json.dumps(manifest)
    result = owner.post("/api/plugins/stage", json={"files": files})
    assert result.status_code == 400
    assert result.json()["detail"].startswith("id:")
    assert "pydantic" not in result.text and "input_value" not in result.text
    preview = owner.post("/api/jobs/preview", json={"kind": "once", "local_time": "", "timezone": "UTC"})
    assert preview.status_code == 400 and "Choose a valid date" in preview.json()["detail"]


def test_removing_marketplace_preserves_installed_plugin_origin(owner, application):
    core = application.state.core
    core.plugins.stage_from_catalog("official", "kanban", "1.0.0")
    before = core.plugins.list()[0]["origin"]
    assert owner.delete("/api/marketplaces/official").status_code == 200
    assert core.plugins.list()[0]["origin"] == before
