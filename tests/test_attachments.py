import asyncio
import base64
from unittest.mock import AsyncMock

import httpx
import pytest
from test_actions import action

from shinbot.attachments import MAX_FILE_BYTES, Attachments, FileStore
from shinbot.db import Database
from shinbot.services.worker import FileRejected, file_operation


@pytest.mark.parametrize(
    "data,name,preview",
    [
        (b"report\nfirst,second\n1,2\n", "report.csv", False),
        (b"\x89PNG\r\n\x1a\nimage bytes", "screenshot.png", True),
        (b"<svg onload='alert(1)'/>", "unsafe.svg", False),
        (b"<script>alert(1)</script>", "picture.png", False),
    ],
)
async def test_attach_replay_download_auth_and_restart(
    application, owner, monkeypatch, tmp_path, data, name, preview
):
    core = application.state.core
    task = core.tasks.create("Create a file and attach it")
    executor = FileStore(tmp_path / "executor-files")
    file = executor.put(data, name)
    remote = AsyncMock(
        side_effect=lambda service, path, *a, **kw: (
            {"file": file} if path == "/execute" else executor.export(file["digest"])
        )
    )
    monkeypatch.setattr(core, "remote", remote)
    payload = action(core, task, "attach_file", {"path": "output/" + name})
    assert (await core.action(payload))["status"] == "executing"
    await asyncio.gather(*core.running_actions.values())
    result = await core.action(payload)
    assert result["status"] == "completed", result
    attachment = result["result"]["attachment"]
    assert attachment["preview"] is preview
    assert remote.await_count == 2
    assert (await core.action(payload)) == result
    assert remote.await_count == 2
    assert len(core.tasks.detail(task["id"])["attachments"]) == 1
    download = owner.get(attachment["url"] + "?download=true")
    assert download.content == data
    assert download.headers["content-disposition"].startswith("attachment;")
    response = owner.get(attachment["url"])
    assert response.headers["content-disposition"].startswith("inline;" if preview else "attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    other = core.tasks.create("Unrelated conversation")
    assert owner.get(attachment["url"].replace(task["id"], other["id"])).status_code == 404
    headers = dict(owner.headers)
    owner.headers.pop("authorization")
    assert owner.get(attachment["url"]).status_code == 401
    owner.headers.update(headers)
    # Opening the persisted database and store again must preserve the same file and URL.
    reopened = Attachments(Database(core.db.path), core.config.data / "attachments")
    assert reopened.existing(payload["id"]) == attachment
    assert base64.b64decode(reopened.files.export(file["digest"])["data"]) == data
    journal = core.db.one("SELECT result FROM actions WHERE id=?", (payload["id"],))["result"]
    assert "digest" not in journal and '"data"' not in journal


async def test_lost_file_transfer_resumes_from_executor_receipt(application, monkeypatch, tmp_path):
    core = application.state.core
    task = core.tasks.create("Attach a result")
    store = FileStore(tmp_path / "executor")
    file = store.put(b"immutable result", "result.txt")
    calls = []

    async def remote(service, path, *a, **kw):
        calls.append(path)
        if path == "/execute":
            return {"file": file}
        if path.startswith("/actions/"):
            return {"status": "completed", "result": {"file": file}}
        if calls.count(path) == 1:
            raise httpx.ConnectError("temporarily offline")
        return store.export(file["digest"])

    monkeypatch.setattr(core, "remote", remote)
    payload = action(core, task, "attach_file", {"path": "result.txt"})
    await core.action(payload)
    await asyncio.gather(*core.running_actions.values())
    saved = core.db.one("SELECT * FROM actions WHERE id=?", (payload["id"],))
    assert saved["status"] == "retrying"
    assert core.tasks.detail(task["id"])["attachments"] == []
    core.db.execute("UPDATE actions SET status='executing' WHERE id=?", (payload["id"],))
    await core.execute_action({**saved, "recovering": True}, task)
    assert (await core.action(payload))["status"] == "completed"
    assert calls.count("/execute") == 1
    assert len(core.tasks.detail(task["id"])["attachments"]) == 1


def test_workspace_binary_export_and_path_boundaries(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    data = b"\x00\xff" * 300000
    (root / "binary.zip").write_bytes(data)
    result = file_operation(root, "attach_file", {"path": "binary.zip"})
    assert base64.b64decode(result["data"]) == data
    file_operation(root, "write_file", {"path": "notes/result.txt", "content": "Created for you"})
    assert (
        base64.b64decode(file_operation(root, "attach_file", {"path": "notes/result.txt"})["data"])
        == b"Created for you"
    )
    (tmp_path / "secret").write_text("private")
    (root / "link").symlink_to(tmp_path / "secret")
    for path in ["../secret", "/etc/passwd", "link", ".", "notes"]:
        with pytest.raises(FileRejected):
            file_operation(root, "attach_file", {"path": path})
    (root / "large").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(FileRejected, match="10 MB"):
        file_operation(root, "attach_file", {"path": "large"})


def test_invalid_names_and_damaged_snapshot_are_rejected(tmp_path):
    store = FileStore(tmp_path)
    for name in ["../file", "a/b", "a\\b", "x\nheader", ".", ""]:
        with pytest.raises(ValueError):
            store.put(b"safe", name)
    file = store.put(b"snapshot", "file.txt")
    store.path(file["digest"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="damaged"):
        store.export(file["digest"])
    with pytest.raises(ValueError):
        store.export("../secrets")
