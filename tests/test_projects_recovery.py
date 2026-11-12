import asyncio

import httpx
import pytest
from fastapi import HTTPException
from test_projects import finish, start
from test_projects import project_setup as project_setup

from shinbot.core import Core
from shinbot.db import uid
from shinbot.services.projects import git


@pytest.mark.parametrize("operation", ["inspect", "create"])
@pytest.mark.parametrize("rejection", [False, True])
def test_service_error_reports_whether_project_request_is_stored(
    owner, project_setup, monkeypatch, operation, rejection
):
    core, _, _, remote = project_setup
    project = owner.post("/api/projects", json={"path": "demo"}).json()

    async def failed(service, path, body=None, **kwargs):
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == operation:
            request = httpx.Request("POST", "http://computer/projects")
            if rejection:
                response = httpx.Response(400, json={"detail": "Repository unavailable"}, request=request)
                response.raise_for_status()
            raise httpx.ReadTimeout("Response lost", request=request)
        return result

    monkeypatch.setattr(core, "remote", failed)
    result = owner.post(
        f"/api/projects/{project['id']}/tasks",
        json={"prompt": "Feature", "request_id": "retry-form"},
    )
    assert result.status_code == (502 if rejection else 503)
    assert result.json()["request_stored"] is (operation == "create")
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 0
    if rejection:
        assert result.json()["detail"] == "Repository unavailable"


async def test_worktree_request_can_wait_for_computer_worker_timeout(application):
    core = application.state.core
    seen = {}

    async def transport(request):
        import json

        operation = json.loads(request.content)["operation"]
        seen[operation] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        core.service_client = client
        for operation in ["create", "archive", "inspect"]:
            await core.projects.computer(operation)
    core.service_client = None
    assert seen["create"] > 360
    assert seen["archive"] > 360
    assert seen["inspect"] <= 60


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_archive_preserves_modified_files_hidden_from_git_status(project_setup, flag):
    core, root, _, _ = project_setup
    project = await core.projects.add("demo")
    task = await start(core, project)
    workspace = core.projects.workspace(task["workspace_id"])
    path = root / workspace["path"]
    git(path, "update-index", flag, "hello.py")
    content = "unsaved owner edit\n"
    (path / "hello.py").write_text(content)
    assert git(path, "status", "--porcelain", "--ignored") == ""
    finish(core, task)

    with pytest.raises(HTTPException, match="hidden from Git status"):
        await core.projects.archive(workspace["id"])

    assert (path / "hello.py").read_text() == content
    assert git(path, "branch", "--show-current").strip() == workspace["branch"]


async def test_child_result_is_visible_without_resuming_archived_parent(project_setup):
    core, root, _, _ = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    parent_row = core.db.one("SELECT * FROM tasks WHERE id=?", (parent["id"],))
    child = await core.projects.tool(
        "delegate_task", {"bot_id": "default", "prompt": "Review"}, parent_row, uid()
    )
    finish(core, parent)
    await core.projects.archive(parent["workspace_id"])
    finish(core, child, "Child review evidence")

    core.projects.deliver_results()
    core.projects.deliver_results()

    detail = core.tasks.detail(parent["id"])
    assert detail["status"] == "completed"
    assert not (root / parent_row["workspace_path"]).exists()
    assert len(detail["messages"]) == 2
    assert detail["messages"][-1]["status"] == "completed"
    assert "Child review evidence" in detail["messages"][-1]["content"]
    assert core.projects.workspace(child["workspace_id"])["notified"] == 1


async def test_archive_cannot_remove_worktree_while_creation_response_is_pending(project_setup, monkeypatch):
    core, root, _, remote = project_setup
    project = await core.projects.add("demo")
    created = asyncio.Event()
    release = asyncio.Event()

    async def delayed(service, path, body=None, **kwargs):
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == "create":
            created.set()
            await release.wait()
        return result

    monkeypatch.setattr(core, "remote", delayed)
    pending = asyncio.create_task(start(core, project))
    try:
        await asyncio.wait_for(created.wait(), 10)
        workspace = core.db.one("SELECT * FROM project_workspaces")
        with pytest.raises(ValueError, match="setup is still pending"):
            await core.projects.archive(workspace["id"])
        assert (root / workspace["path"] / "hello.py").is_file()
    finally:
        release.set()
        task = await pending

    assert core.projects.workspace(workspace["id"])["state"] == "ready"
    assert core.tasks.detail(task["id"])["status"] == "queued"
    assert (root / workspace["path"] / "hello.py").is_file()


async def test_delegation_action_retries_lost_create_response_without_duplicate_task(
    project_setup, monkeypatch
):
    core, _, repo, remote = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    payload = {
        "id": uid(),
        "task_id": parent["id"],
        "epoch": core.db.setting("epoch"),
        "tool": "plugin:projects:1.0.0:delegate_task",
        "args": {"bot_id": "default", "prompt": "Review"},
    }
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)

    async def disconnected(service, path, body=None, **kwargs):
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == "create":
            raise httpx.ReadTimeout("Response lost after worktree creation")
        return result

    monkeypatch.setattr(core, "remote", disconnected)
    await core.action(payload)
    await core.running_actions[payload["id"]]
    assert core.db.one("SELECT status FROM actions WHERE id=?", (payload["id"],))["status"] == "retrying"
    assert core.tasks.detail(parent["id"])["status"] == "waiting"
    workspace = core.db.one("SELECT * FROM project_workspaces WHERE parent_task_id=?", (parent["id"],))

    monkeypatch.setattr(core, "remote", remote)
    core.db.execute("UPDATE action_retries SET next_attempt=0 WHERE action_id=?", (payload["id"],))
    await core.action(payload)
    await core.running_actions[payload["id"]]
    result = await core.action(payload)

    assert result["status"] == "completed"
    assert result["result"]["workspace_id"] == workspace["id"]
    assert (await core.action(payload))["result"]["id"] == result["result"]["id"]
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 2
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 3


async def test_delegation_action_recovers_completed_creation_after_backend_restart(project_setup):
    core, _, _, remote = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    payload = {
        "id": uid(),
        "task_id": parent["id"],
        "epoch": core.db.setting("epoch"),
        "tool": "plugin:projects:1.0.0:delegate_task",
        "args": {"bot_id": "default", "prompt": "Review"},
    }
    assert (await core.action(payload))["status"] == "pending"
    core.db.execute("UPDATE actions SET status='executing' WHERE id=?", (payload["id"],))
    action = core.db.one("SELECT * FROM actions WHERE id=?", (payload["id"],))
    parent_row = core.db.one("SELECT * FROM tasks WHERE id=?", (parent["id"],))
    child = await core.perform(action, parent_row)

    # Reopen before execute_action records the result, as after an abrupt service exit.
    reopened = Core(core.config)
    reopened.remote = remote
    try:
        reopened.recover()
        assert (
            reopened.db.one("SELECT status FROM actions WHERE id=?", (payload["id"],))["status"] == "retrying"
        )
        reopened.db.execute("UPDATE action_retries SET next_attempt=0 WHERE action_id=?", (payload["id"],))
        await reopened.action(payload)
        await reopened.running_actions[payload["id"]]
        result = await reopened.action(payload)
        assert result["status"] == "completed"
        assert result["result"]["id"] == child["id"]
        assert reopened.db.one("SELECT count(*) AS n FROM tasks")["n"] == 2
    finally:
        await reopened.close()


async def test_cancel_rejects_inflight_delegation_before_waiting_for_child_abort(project_setup, monkeypatch):
    core, _, _, remote = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    parent_row = core.db.one("SELECT * FROM tasks WHERE id=?", (parent["id"],))
    first = await core.projects.tool(
        "delegate_task", {"bot_id": "default", "prompt": "First"}, parent_row, uid()
    )
    created = asyncio.Event()
    release_create = asyncio.Event()
    abort_entered = asyncio.Event()
    release_abort = asyncio.Event()

    async def delayed_create(service, path, body=None, **kwargs):
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == "create":
            created.set()
            await release_create.wait()
        return result

    async def stalled_abort(task_id):
        if task_id == first["id"]:
            abort_entered.set()
            await release_abort.wait()
        return True

    monkeypatch.setattr(core, "remote", delayed_create)
    monkeypatch.setattr(core.coordinator, "abort_task", stalled_abort)
    pending = asyncio.create_task(
        core.projects.tool("delegate_task", {"bot_id": "default", "prompt": "Second"}, parent_row, uid())
    )
    cancelling = None
    try:
        await asyncio.wait_for(created.wait(), 10)
        cancelling = asyncio.create_task(core.cancel(parent["id"]))
        await asyncio.wait_for(abort_entered.wait(), 10)
        assert core.tasks.detail(parent["id"])["status"] == "cancelled"
        release_create.set()
        with pytest.raises(ValueError, match="no longer awaiting a task"):
            await pending
    finally:
        release_create.set()
        release_abort.set()
        await asyncio.gather(pending, return_exceptions=True)
        if cancelling:
            await cancelling

    assert core.tasks.detail(first["id"])["status"] == "cancelled"
    children = core.db.all("SELECT * FROM tasks WHERE parent_task_id=?", (parent["id"],))
    assert len(children) == 1
    assert all(task["status"] == "cancelled" for task in children)


async def test_archive_preserves_untracked_files_when_repository_config_hides_them(project_setup):
    core, root, _, _ = project_setup
    project = await core.projects.add("demo")
    task = await start(core, project)
    workspace = core.projects.workspace(task["workspace_id"])
    path = root / workspace["path"]
    git(path, "config", "status.showUntrackedFiles", "no")
    content = "unsaved contents\n"
    (path / "new_unsaved_file.txt").write_text(content)
    finish(core, task)

    with pytest.raises(HTTPException, match="changed, untracked or ignored"):
        await core.projects.archive(workspace["id"])

    assert (path / "new_unsaved_file.txt").read_text() == content
    assert git(path, "branch", "--show-current").strip() == workspace["branch"]


async def test_cancel_reaches_active_grandchild_beneath_completed_child(project_setup, monkeypatch):
    core, _, _, _ = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    parent_row = core.db.one("SELECT * FROM tasks WHERE id=?", (parent["id"],))
    child = await core.projects.tool(
        "delegate_task", {"bot_id": "default", "prompt": "Child"}, parent_row, uid()
    )
    child_row = core.db.one("SELECT * FROM tasks WHERE id=?", (child["id"],))
    grandchild = await core.projects.tool(
        "delegate_task", {"bot_id": "default", "prompt": "Grandchild"}, child_row, uid()
    )
    finish(core, child)
    aborted = []

    async def abort(task_id):
        aborted.append(task_id)
        assert core.tasks.detail(parent["id"])["status"] == "cancelled"
        assert core.tasks.detail(grandchild["id"])["status"] == "cancelled"
        return True

    monkeypatch.setattr(core.coordinator, "abort_task", abort)
    await core.cancel(parent["id"])

    assert set(aborted) == {parent["id"], grandchild["id"]}
    assert core.tasks.detail(child["id"])["status"] == "completed"
    assert core.tasks.detail(grandchild["id"])["messages"][0]["status"] == "cancelled"


async def test_cancelled_setup_can_be_archived_and_delayed_create_cannot_restore_it(
    project_setup, monkeypatch
):
    core, root, _, remote = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    parent_row = core.db.one("SELECT * FROM tasks WHERE id=?", (parent["id"],))
    created = asyncio.Event()
    release = asyncio.Event()

    async def delayed_create(service, path, body=None, **kwargs):
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == "create":
            created.set()
            await release.wait()
        return result

    async def abort(task_id):
        return True

    monkeypatch.setattr(core, "remote", delayed_create)
    monkeypatch.setattr(core.coordinator, "abort_task", abort)
    pending = asyncio.create_task(
        core.projects.tool("delegate_task", {"bot_id": "default", "prompt": "Child"}, parent_row, uid())
    )
    try:
        await asyncio.wait_for(created.wait(), 10)
        await core.cancel(parent["id"])
    finally:
        release.set()
    with pytest.raises(ValueError, match="no longer awaiting a task"):
        await pending

    workspace = core.db.one("SELECT * FROM project_workspaces WHERE parent_task_id=?", (parent["id"],))
    assert workspace["state"] == "abandoned"
    assert workspace["task_id"] is None
    assert (root / workspace["path"]).is_dir()
    monkeypatch.setattr(core, "remote", remote)
    await core.projects.archive(workspace["id"])
    assert core.projects.workspace(workspace["id"])["state"] == "archived"
    assert not (root / workspace["path"]).exists()

    with pytest.raises(HTTPException, match="was archived"):
        await core.projects.computer(
            "create",
            path=project["path"],
            project_id=project["id"],
            workspace_id=workspace["id"],
            base_commit=workspace["base_commit"],
        )
    assert not (root / workspace["path"]).exists()
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1


async def test_projects_access_revoked_during_setup_prevents_task_admission(project_setup, monkeypatch):
    core, root, _, remote = project_setup
    project = await core.projects.add("demo")
    bot = core.bots.save({"name": "Restricted"})
    created = asyncio.Event()
    release = asyncio.Event()

    async def delayed_create(service, path, body=None, **kwargs):
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == "create":
            created.set()
            await release.wait()
        return result

    monkeypatch.setattr(core, "remote", delayed_create)
    pending = asyncio.create_task(start(core, project, bot_id=bot["id"]))
    try:
        await asyncio.wait_for(created.wait(), 10)
        core.plugins.set_access("projects", {"mode": "bots", "bots": ["default"]})
    finally:
        release.set()
    with pytest.raises(ValueError, match="Enable Projects"):
        await pending

    assert not core.plugins.allowed("projects", bot["id"])
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 0
    workspace = core.db.one("SELECT * FROM project_workspaces")
    assert workspace["state"] == "abandoned"
    assert workspace["task_id"] is None
    monkeypatch.setattr(core, "remote", remote)
    await core.projects.archive(workspace["id"])
    assert not (root / workspace["path"]).exists()


async def test_cancel_after_lost_create_response_allows_unused_worktree_cleanup(project_setup, monkeypatch):
    core, root, _, remote = project_setup
    project = await core.projects.add("demo")
    parent = await start(core, project)
    payload = {
        "id": uid(),
        "task_id": parent["id"],
        "epoch": core.db.setting("epoch"),
        "tool": "plugin:projects:1.0.0:delegate_task",
        "args": {"bot_id": "default", "prompt": "Review"},
    }
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)

    async def disconnected(service, path, body=None, **kwargs):
        result = await remote(service, path, body, **kwargs)
        if body and body.get("operation") == "create":
            raise httpx.ReadTimeout("Response lost after worktree creation")
        return result

    async def abort(task_id):
        return True

    monkeypatch.setattr(core, "remote", disconnected)
    monkeypatch.setattr(core.coordinator, "abort_task", abort)
    await core.action(payload)
    await core.running_actions[payload["id"]]
    assert core.db.one("SELECT status FROM actions WHERE id=?", (payload["id"],))["status"] == "retrying"
    await core.cancel(parent["id"])

    workspace = core.db.one("SELECT * FROM project_workspaces WHERE parent_task_id=?", (parent["id"],))
    assert workspace["state"] == "abandoned"
    assert workspace["task_id"] is None
    assert (root / workspace["path"]).exists()
    assert core.db.one("SELECT status FROM actions WHERE id=?", (payload["id"],))["status"] == "denied"
    assert (await core.action(payload))["status"] == "blocked"
    monkeypatch.setattr(core, "remote", remote)
    await core.projects.archive(workspace["id"])
    assert core.projects.workspace(workspace["id"])["state"] == "archived"
    assert not (root / workspace["path"]).exists()
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1


async def test_interrupted_worktree_checkout_does_not_queue_task_on_retry(project_setup, monkeypatch):
    core, root, repo, remote = project_setup
    project = await core.projects.add("demo")
    interrupted = False

    async def failing_create(service, path, body=None, **kwargs):
        nonlocal interrupted
        if body and body.get("operation") == "create" and not interrupted:
            interrupted = True
            workspace = root / ".shinbot-worktrees" / body["project_id"] / body["workspace_id"]
            workspace.parent.mkdir(parents=True, exist_ok=True)
            git(
                repo,
                "worktree",
                "add",
                "--no-checkout",
                "-b",
                "shinbot/" + body["workspace_id"],
                "--",
                str(workspace),
                body["base_commit"],
            )
            raise ValueError("Checkout interrupted before files were written")
        return await remote(service, path, body, **kwargs)

    monkeypatch.setattr(core, "remote", failing_create)
    payload = {"prompt": "Feature", "request_id": uid()}
    with pytest.raises(ValueError, match="Checkout interrupted"):
        await core.projects.start(project["id"], payload)
    with pytest.raises(HTTPException, match="checkout did not finish cleanly"):
        await core.projects.start(project["id"], payload)

    workspace = core.db.one("SELECT * FROM project_workspaces WHERE request_id=?", (payload["request_id"],))
    path = root / workspace["path"]
    assert workspace["state"] == "preparing"
    assert workspace["task_id"] is None
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 0
    assert not (path / "hello.py").exists()

    git(path, "restore", "--source=HEAD", "--staged", "--worktree", ".")
    task = await core.projects.start(project["id"], payload)
    assert task["status"] == "queued"
    assert task["workspace_id"] == workspace["id"]
    assert (path / "hello.py").read_text() == "print('base')\n"
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1


async def test_managed_task_worktree_cannot_be_registered_as_another_project(project_setup):
    core, _, _, _ = project_setup
    project = await core.projects.add("demo")
    task = await start(core, project)
    workspace = core.projects.workspace(task["workspace_id"])

    with pytest.raises(HTTPException, match="original repository"):
        await core.projects.add(workspace["path"])

    assert core.projects.list() == [project]
    assert core.projects.workspace(workspace["id"])["state"] == "ready"
