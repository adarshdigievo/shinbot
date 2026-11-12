import hashlib
import json
import subprocess
import sys

import pytest

from shinbot.bots import BOT_COLORS, Bots, Template
from shinbot.volume_admin import service_filter, validate_archive


def test_automatic_colors_use_whole_palette_before_repeating(application):
    bots = application.state.core.bots
    created = [bots.save({"name": f"Helper {i}"}) for i in range(len(BOT_COLORS) - 1)]
    assert {b["profile"]["color"] for b in bots.list()} == set(BOT_COLORS)
    bots.archive(created[0]["id"])
    replacement = bots.save({"name": "Replacement"})
    assert replacement["profile"]["color"] == created[0]["profile"]["color"]
    for i in range(len(BOT_COLORS)):
        bots.save({"name": f"Another helper {i}"})
    colors = [b["profile"]["color"] for b in bots.list()]
    assert all(colors.count(color) == 2 for color in BOT_COLORS)


def test_bot_color_edit_persists_and_legacy_profiles_stay_blue(owner, application):
    bots = application.state.core.bots
    assert owner.get("/api/bots").json()["colors"] == list(BOT_COLORS)
    bot = owner.post("/api/bots", json={"profile": {"name": "Writer", "color": "rose"}}).json()
    changed = owner.post("/api/bots", json={"id": bot["id"], "profile": {**bot["profile"], "color": "teal"}})
    assert changed.status_code == 200
    # A client that omits cosmetic settings must not reset an existing choice.
    bots.save({"name": "Renamed"}, bot["id"])
    reloaded = Bots(bots.db)
    assert reloaded.get(bot["id"])["profile"]["color"] == "teal"
    invalid = owner.post("/api/bots", json={"profile": {"name": "Bad colour", "color": "red;url(x)"}})
    assert invalid.status_code == 400
    legacy = reloaded.get("default")["profile"]
    legacy.pop("color")
    bots.db.execute("UPDATE bots SET profile=? WHERE id='default'", (json.dumps(legacy),))
    assert bots.get("default")["profile"]["color"] == "blue"
    assert bots.save(legacy, "default")["profile"]["color"] == "blue"


def test_template_colors_are_assigned_once_and_exported(application):
    bots = application.state.core.bots
    template = {
        "id": "color-team",
        "version": "1.0.0",
        "name": "Team",
        "description": "Helpers",
        "kind": "group",
        "profiles": [{"name": "Automatic"}, {"name": "Violet", "color": "violet"}],
    }
    result = bots.install(template, "color-import")
    first, second = [bots.get(id)["profile"]["color"] for id in result["bot_ids"]]
    assert second == "violet" and first not in {"blue", "violet"}
    assert bots.install(template, "color-import") == result
    exported = bots.export(group_id=result["group_id"])
    assert [p["color"] for p in exported["profiles"]] == [first, second]
    imported = bots.install(exported, "color-reimport")
    assert [bots.get(id)["profile"]["color"] for id in imported["bot_ids"]] == [first, second]


def test_bot_profiles_pin_role_and_browser_and_separate_memory(application):
    core = application.state.core
    a = core.bots.save({"name": "Researcher", "instructions": "Check primary sources", "browser": "private"})
    b = core.bots.save({"name": "Editor", "browser": "shared"})
    task = core.tasks.create("Help me", bot_id=a["id"])
    old = core.tasks.detail(task["id"])
    core.bots.save({"name": "Researcher", "instructions": "New instructions", "browser": "shared"}, a["id"])
    assert json.loads(old["bot_profile"])["instructions"] == "Check primary sources"
    assert old["browser_id"] == "bot-" + a["id"]
    new = core.tasks.detail(core.tasks.create("Again", bot_id=a["id"])["id"])
    assert new["browser_id"] == "shared"
    core.notebook.remember("Private research fact", bot_id=a["id"])
    assert core.notebook.memories(bot_id=b["id"]) == []
    assert "Private research fact" in core.notebook.context(a["id"])
    assert "Private research fact" not in core.instructions(
        core.tasks.detail(core.tasks.create("Hi", bot_id=b["id"])["id"])
    )


def test_group_template_is_idempotent_and_export_excludes_data(application):
    core = application.state.core
    template = next(
        t for market in core.bots.templates() for t in market["templates"] if t["kind"] == "group"
    )
    installed = core.bots.install(template, "once")
    # An installation receipt from a release without colours remains retryable.
    legacy = Template.model_validate(template).model_dump(exclude={"profiles": {"__all__": {"color"}}})
    legacy_hash = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    core.db.execute("UPDATE template_installs SET hash=? WHERE request_id='once'", (legacy_hash,))
    assert core.bots.install(template, "once") == installed
    a, b = [core.bots.get(id) for id in installed["bot_ids"]]
    assert a["browser_id"] == b["browser_id"] == "group-" + installed["group_id"]
    core.tasks.create("PRIVATE CHAT", bot_id=a["id"])
    core.notebook.remember("PRIVATE MEMORY", bot_id=a["id"])
    exported = core.bots.export(group_id=installed["group_id"])
    assert "PRIVATE" not in json.dumps(exported)
    assert len(Template.model_validate(exported).profiles) == 2
    with pytest.raises(ValueError):
        core.bots.install({**template, "credentials": {"secret": "bad"}}, "other")


def test_scheduled_job_uses_selected_bot_and_task_requests_are_bot_bound(owner, application):
    core = application.state.core
    bot = core.bots.save({"name": "Daily helper"})
    job = core.jobs.save(
        {
            "name": "Reminder",
            "prompt": "hello",
            "kind": "interval",
            "spec": "60",
            "timezone": "UTC",
            "bot_id": bot["id"],
        }
    )
    task_id = core.jobs.tick(job["next_run"] + 1)[0]
    assert core.tasks.detail(task_id)["bot_id"] == bot["id"]
    request = {"content": "hello", "request_id": "bot-bound", "bot_id": bot["id"]}
    assert owner.post("/api/tasks", json=request).status_code == 200
    assert owner.post("/api/tasks", json={**request, "bot_id": "default"}).status_code == 400


def test_public_welcome_does_not_reveal_profiles(client, application):
    core = application.state.core
    core.bots.save({"name": "PRIVATE NAME", "instructions": "PRIVATE INSTRUCTIONS"})
    response = client.get("/welcome")
    assert response.status_code == 200 and "PRIVATE" not in response.text
    assert client.get("/api/bots").status_code == 401
    assert response.json()["templates"], "Bundled templates should be available before sign-in"


def test_template_shared_logins_require_explicit_owner_choice(owner, application):
    core = application.state.core
    template = {
        "id": "personal-helper",
        "version": "1.0.0",
        "name": "Helper",
        "description": "A helper",
        "profiles": [{"name": "Helper", "browser": "shared"}],
    }
    installed = owner.post(
        "/api/templates/install", json={"template": template, "request_id": "private"}
    ).json()
    assert core.bots.get(installed["bot_ids"][0])["profile"]["browser"] == "private"
    installed = owner.post(
        "/api/templates/install",
        json={"template": template, "request_id": "shared", "allow_shared_browser": True},
    ).json()
    assert core.bots.get(installed["bot_ids"][0])["profile"]["browser"] == "shared"
    template["kind"] = "group"
    installed = core.bots.install(template, "group")
    assert core.bots.get(installed["bot_ids"][0])["profile"]["browser"] == "group"


def test_forgetting_is_scoped_to_bot(application):
    core = application.state.core
    other = core.bots.save({"name": "Other"})
    memory = core.notebook.remember("I like trains")
    core.notebook.forget(memory["id"])
    with pytest.raises(ValueError, match="forgotten"):
        core.notebook.remember("I like trains", "task:old")
    assert core.notebook.remember("I like trains", "task:new", other["id"])


def test_archived_bot_cannot_resume_jobs(application):
    core = application.state.core
    other = core.bots.save({"name": "Other"})
    job = core.jobs.save(
        {
            "name": "Routine",
            "prompt": "Hi",
            "kind": "interval",
            "spec": "60",
            "timezone": "UTC",
            "bot_id": other["id"],
        }
    )
    core.bots.archive(other["id"])
    with pytest.raises(ValueError, match="archived"):
        core.jobs.toggle(job["id"], True)
    with pytest.raises(ValueError, match="active bot"):
        core.jobs.save(
            {
                "name": "Routine",
                "prompt": "Hi",
                "kind": "interval",
                "spec": "60",
                "timezone": "UTC",
                "bot_id": other["id"],
            }
        )


def test_bootstrap_secret_is_atomic_and_empty_secret_fails_closed(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from shinbot.config import secret_file

    path = tmp_path / "secret"
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: secret_file(path), range(24)))
    assert len(set(values)) == 1 and len(values[0]) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("")
    with pytest.raises(ValueError, match="Invalid secret"):
        secret_file(path)


def test_worker_cannot_import_workspace_python(tmp_path):
    (tmp_path / "asyncio.py").write_text('raise RuntimeError("WORKSPACE IMPORTED")')
    (tmp_path / "shinbot").mkdir()
    (tmp_path / "shinbot/__init__.py").write_text('raise RuntimeError("SHADOW IMPORTED")')
    (tmp_path / "note.txt").write_text("safe")
    result = subprocess.run(
        [sys.executable, "-I", "-m", "shinbot.services.worker"],
        cwd=tmp_path,
        input=json.dumps({"operation": "file", "tool": "read_file", "args": {"path": "note.txt"}}),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"]["text"] == "safe"


def test_backup_preserves_absolute_symlink_without_extracting_through_it(tmp_path):
    import io
    import tarfile

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        directory = tarfile.TarInfo("workspace")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid = 1001
        directory.gid = 1001
        archive.addfile(directory)
        link = tarfile.TarInfo("workspace/python")
        link.type = tarfile.SYMTYPE
        link.linkname = "/usr/local/bin/python"
        link.uid = 1001
        link.gid = 1001
        archive.addfile(link)
    data.seek(0)
    with tarfile.open(fileobj=data) as archive:
        validate_archive(archive)
        archive.extractall(tmp_path, filter=service_filter)
    assert (tmp_path / "workspace/python").is_symlink()
    assert (tmp_path / "workspace").stat().st_mode & 0o777 == 0o755
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        link = tarfile.TarInfo("workspace/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp"
        archive.addfile(link)
        nested = tarfile.TarInfo("workspace/link/escape")
        archive.addfile(nested)
    data.seek(0)
    with tarfile.open(fileobj=data) as archive, pytest.raises(ValueError, match="through a link"):
        validate_archive(archive)
