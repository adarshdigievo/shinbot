import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from shinbot.core import Core
from shinbot.db import packed, uid
from shinbot.services import computer
from shinbot.services.common import Journal
from shinbot.services.projects import git, operate


@pytest.fixture
def project_setup(application, tmp_path, monkeypatch):
    root = tmp_path / "computer"
    repo = root / "demo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "hello.py").write_text("print('base')\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    git(repo, "add", ".")
    # Commits exist only in this disposable test repository.
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Fixture",
        ],
        check=True,
        capture_output=True,
    )
    core = application.state.core
    core.plugins.stage_from_catalog("official", "projects", "1.0.0")
    core.plugins.activate("projects", "1.0.0")
    monkeypatch.setattr(computer, "ROOT", root)
    monkeypatch.setattr(computer, "DATA", tmp_path / "executor")
    journal = Journal(tmp_path / "executor")
    monkeypatch.setattr(computer, "journal", journal)
    monkeypatch.setenv("SHINBOT_CHILD_PATH", os.environ["PATH"])

    async def remote(service, path, body=None, **kwargs):
        if service == "computer" and path == "/projects":
            # Exercise the real isolated worker process and Git binary.
            return await computer.projects(body)
        if service == "computer" and path == "/execute":
            return await computer.execute(body)
        return {}

    monkeypatch.setattr(core, "remote", remote)
    yield core, root, repo, remote
    journal.db.close()


async def start(core, project, prompt="Implement a feature", **kw):
    return await core.projects.start(project["id"], {"prompt": prompt, "request_id": uid(), **kw})


def finish(core, task, answer="Done with evidence"):
    core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task["id"],))
    core.db.execute("UPDATE messages SET status='completed',answer=? WHERE task_id=?", (answer, task["id"]))


def test_native_plugins_are_opt_in_and_api_is_gated(owner, application):
    assert owner.get("/api/projects").status_code == 400
    assert owner.get("/api/board").status_code == 409
    core = application.state.core
    assert not core.db.one("SELECT 1 FROM plugins WHERE id IN ('kanban','projects')")
    catalog = owner.get("/api/plugins").json()["marketplaces"]
    assert {p["id"] for p in catalog[0]["plugins"]} >= {"kanban", "projects"}
    core.plugins.stage_from_catalog("official", "projects", "1.0.0")
    core.plugins.activate("projects", "1.0.0")
    assert owner.get("/api/projects").json() == {"projects": []}
    assert owner.get("/api/board").status_code == 409
    core.plugins.disable("projects")
    assert owner.get("/api/projects").status_code == 400


async def test_parallel_worktrees_scope_commands_files_and_keep_main_unchanged(project_setup):
    core, root, repo, _ = project_setup
    project = await core.projects.add("demo")
    (repo / "hello.py").write_text("uncommitted owner work\n")
    first, second = await asyncio.gather(
        start(core, project, "Feature one"), start(core, project, "Feature two")
    )
    paths = []
    for task, content in [(first, "one"), (second, "two")]:
        row = core.db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
        paths.append(root / row["workspace_path"])
        result = await core.perform(
            {
                "id": uid(),
                "epoch": core.db.setting("epoch"),
                "tool": "write_file",
                "args": packed({"path": "hello.py", "content": content}),
            },
            row,
        )
        assert result["bytes"] == len(content)
        result = await core.perform(
            {
                "id": uid(),
                "epoch": core.db.setting("epoch"),
                "tool": "run_command",
                "args": packed({"command": "pwd"}),
            },
            row,
        )
        assert Path(result["output"].strip()).resolve() == paths[-1].resolve()
    assert paths[0] != paths[1]
    assert (paths[0] / "hello.py").read_text() == "one"
    assert (paths[1] / "hello.py").read_text() == "two"
    assert (repo / "hello.py").read_text() == "uncommitted owner work\n"
    changes = await core.projects.inspect_workspace(first["workspace_id"])
    assert "+one" in changes["patch"] and "two" not in changes["patch"]
    assert "hello.py" in [
        f["name"] for f in (await core.projects.inspect_workspace(first["workspace_id"], "files"))["files"]
    ]


async def test_lost_response_and_restart_reuses_worktree_and_task(project_setup):
    core, root, repo, remote = project_setup
    project = await core.projects.add("demo")
    payload = {"prompt": "One request", "request_id": "retry-me"}
    lost = True

    async def disconnected(service, path, body=None, **kwargs):
        nonlocal lost
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == "create" and lost:
            lost = False
            raise httpx.ReadTimeout("Response lost after Git completed")
        return result

    core.remote = disconnected
    with pytest.raises(httpx.ReadTimeout):
        await core.projects.start(project["id"], payload)
    assert core.db.one("SELECT count(*) AS n FROM project_workspaces")["n"] == 1
    reopened = Core(core.config)
    reopened.remote = remote
    result = await reopened.projects.start(project["id"], payload)
    assert (await reopened.projects.start(project["id"], payload))["id"] == result["id"]
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    with pytest.raises(ValueError, match="different"):
        await reopened.projects.start(project["id"], {**payload, "prompt": "Changed request"})
    await reopened.close()


async def test_delegation_approval_limits_inherited_permissions_and_durable_delivery(project_setup):
    core, _, _, remote = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    row = core.db.one("SELECT * FROM tasks WHERE id=?", (parent["id"],))
    assert core.approval_required(
        "plugin:projects:1.0.0:delegate_task", {}, {"computer": True, "extensions": True}
    )
    preferences = core.db.setting("preferences")
    core.db.set("preferences", {**preferences, "computer_auto": True, "browser_auto": True})
    reviewer = core.bots.save({"name": "Reviewer"})
    child = await core.projects.tool(
        "delegate_task", {"bot_id": reviewer["id"], "prompt": "Review the design"}, row, "delegate-1"
    )
    child_row = core.db.one("SELECT * FROM tasks WHERE id=?", (child["id"],))
    assert not json.loads(child_row["grants"])["computer"]
    assert child_row["workspace_id"] != row["workspace_id"]
    assert child_row["parent_task_id"] == parent["id"]
    assert (
        await core.projects.tool(
            "delegate_task", {"bot_id": reviewer["id"], "prompt": "Review the design"}, row, "delegate-1"
        )
    )["id"] == child["id"]
    finish(core, parent)
    finish(core, child, "Reviewed two changes. Findings attached.")
    reopened = Core(core.config)
    reopened.remote = remote
    reopened.projects.deliver_results()
    reopened.projects.deliver_results()
    messages = core.db.all("SELECT * FROM messages WHERE task_id=? ORDER BY created", (parent["id"],))
    assert len(messages) == 2
    assert "Reviewed two changes" in messages[-1]["payload"]
    assert core.tasks.detail(parent["id"])["status"] == "queued"
    for i in range(3):
        await core.projects.tool(
            "delegate_task", {"bot_id": "default", "prompt": str(i)}, row, "more-" + str(i)
        )
    with pytest.raises(ValueError, match="four tasks"):
        await core.projects.tool("delegate_task", {"bot_id": "default", "prompt": "too many"}, row, "fifth")
    await reopened.close()


async def test_archive_keeps_branch_refuses_dirty_files_and_blocks_followup(project_setup):
    core, root, repo, _ = project_setup
    project = await core.projects.add("demo")
    task = await start(core, project)
    workspace = core.projects.workspace(task["workspace_id"])
    with pytest.raises(ValueError, match="Stop"):
        await core.projects.archive(workspace["id"])
    finish(core, task)
    (root / workspace["path"] / "ignored.txt").write_text("keep me")
    with pytest.raises(Exception, match="changed, untracked or ignored"):
        await core.projects.archive(workspace["id"])
    assert (root / workspace["path"] / "ignored.txt").read_text() == "keep me"
    (root / workspace["path"] / "ignored.txt").unlink()
    await core.projects.archive(workspace["id"])
    assert not (root / workspace["path"]).exists()
    assert git(repo, "rev-parse", "--verify", workspace["branch"]).strip() == workspace["base_commit"]
    with pytest.raises(ValueError, match="archived"):
        core.tasks.message(task["id"], "More work", uid())


async def test_project_paths_cannot_escape_computer_and_scope_is_revocable(project_setup):
    core, root, _, _ = project_setup
    with pytest.raises(ValueError, match="leaves"):
        operate(root, {"operation": "browse", "path": "../"})
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="leaves"):
        operate(root, {"operation": "inspect", "path": "escape"})
    project = await core.projects.add("demo")
    bot = core.bots.save({"name": "Restricted"})
    core.plugins.set_access("projects", {"mode": "bots", "bots": ["default"]})
    with pytest.raises(ValueError, match="Enable Projects"):
        await start(core, project, bot_id=bot["id"])
    task = await start(core, project)
    with pytest.raises(Exception, match="traversal"):
        await core.projects.inspect_workspace(task["workspace_id"], "read", "../demo/hello.py")


async def test_same_bot_can_run_two_project_worktrees_concurrently(project_setup, monkeypatch):
    core, _, _, _ = project_setup
    project = await core.projects.add("demo")
    first = await start(core, project)
    second = await start(core, project)
    submissions = []

    async def runtime(method, path, **kwargs):
        if path == "/v1/health":
            return {"held": False}
        if path == "/v1/submit":
            submissions.append(kwargs["json"])
            return {"submissionId": len(submissions), "conversationId": len(submissions)}
        return {}

    monkeypatch.setattr(core.coordinator, "request", runtime)
    await core.coordinator.tick()
    assert {s["taskId"] for s in submissions} == {first["id"], second["id"]}
    assert all("Computer working directory" in s["instructions"] for s in submissions)


def test_owner_can_browse_register_and_retry_project_task(owner, project_setup):
    core, _, _, _ = project_setup
    folders = owner.get("/api/projects/folders").json()
    assert folders["folders"][0]["name"] == "demo"
    result = owner.post("/api/projects", json={"path": "demo"})
    assert result.status_code == 200, result.text
    project = result.json()
    payload = {"prompt": "Feature", "request_id": "owner-request"}
    first = owner.post(f"/api/projects/{project['id']}/tasks", json=payload)
    assert first.status_code == 200, first.text
    repeated = owner.post(f"/api/projects/{project['id']}/tasks", json=payload)
    assert repeated.json()["id"] == first.json()["id"]
    assert owner.get(f"/api/projects/{project['id']}/workspaces").json()[0]["display_title"] == "Feature"
