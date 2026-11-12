import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from test_actions import action

from shinbot.desktop_host import container_name, create_command
from shinbot.services import browser, desktop
from shinbot.services.common import Journal


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("SHINBOT_DESKTOP", "1")
    monkeypatch.setenv("SHINBOT_DESKTOP_STATE", str(tmp_path / "state"))
    for key, value in {
        "lock": asyncio.Lock(),
        "lease": None,
        "human_pending": False,
        "journal": Journal(tmp_path / "browser"),
        "cancelled": set(),
        "observations": {},
        "control_generation": 0,
        "last_agent": None,
        "browser": None,
        "page": None,
    }.items():
        monkeypatch.setattr(browser, key, value)
    monkeypatch.setattr(
        desktop, "observe", AsyncMock(return_value={"content": [{"type": "text", "text": "screen"}]})
    )
    monkeypatch.setattr(desktop, "perform", AsyncMock())
    yield
    browser.journal.db.close()


async def test_takeover_does_not_launch_browser_and_fences_workspace(screen, monkeypatch):
    launch = AsyncMock()
    monkeypatch.setattr(browser, "select_profile", launch)
    await browser.takeover({"owner": "device"})
    launch.assert_not_called()
    with pytest.raises(HTTPException) as blocked:
        async with desktop.access():
            pytest.fail("Workspace executed while human owns desktop")
    assert blocked.value.status_code == 423
    with pytest.raises(HTTPException):
        await browser.release({"owner": "other-device"})
    assert desktop.owner() == "device"
    await browser.release({"owner": "device"})
    async with desktop.access():
        pass


async def test_takeover_waits_for_workspace_effect_to_finish(screen):
    async with desktop.access():
        with pytest.raises(HTTPException) as busy:
            await browser.takeover({"owner": "device"})
        assert busy.value.status_code == 423
        assert desktop.owner() is None
    await browser.takeover({"owner": "device"})
    assert desktop.owner() == "device"


async def test_desktop_input_needs_fresh_observation_and_replays_once(screen):
    payload = {
        "id": "click",
        "task_id": "task",
        "epoch": "epoch",
        "approval_mode": "full",
        "args": {"operation": "click", "x": 10, "y": 20},
    }
    with pytest.raises(HTTPException):
        await browser.execute_desktop(payload)
    await browser.execute_desktop({**payload, "id": "observe", "args": {"operation": "observe"}})
    await browser.execute_desktop(payload)
    assert desktop.perform.await_count == 1
    await browser.takeover({"owner": "device"})
    # Delivery retries read the receipt even during human control.
    await browser.execute_desktop(payload)
    assert desktop.perform.await_count == 1
    await browser.release({"owner": "device"})
    with pytest.raises(HTTPException):
        await browser.execute_desktop({**payload, "id": "stale"})
    assert desktop.perform.await_count == 1


async def test_capture_failure_after_input_never_duplicates_action(screen):
    payload = {
        "id": "open-files",
        "task_id": "task",
        "epoch": "e",
        "approval_mode": "full",
        "args": {"operation": "launch", "application": "files"},
    }
    desktop.observe.side_effect = RuntimeError("capture failed")
    result = await browser.execute_desktop(payload)
    assert "completed" in result["content"][0]["text"]
    await browser.execute_desktop(payload)
    assert desktop.perform.await_count == 1


async def test_desktop_requires_owner_approval_in_auto(application, monkeypatch):
    core = application.state.core
    core.remote = AsyncMock(return_value={"lease": None})
    task = core.tasks.create("Use the desktop")
    payload = action(core, task, "desktop", {"operation": "launch", "application": "terminal"})
    assert (await core.action(payload))["status"] == "pending"
    assert not core.running_actions
    core.approvals.decide(payload["id"], True)
    await core.action(payload)
    await asyncio.gather(*core.running_actions.values())
    calls = [c for c in core.remote.call_args_list if c.args[1] == "/desktop/execute"]
    assert len(calls) == 1
    assert calls[0].args[2]["desktop_approved"] is True
    assert core.executor({"tool": "desktop"}) == "browser"


def test_container_launch_limits_host_mounts_and_keeps_secrets_out_of_argv(tmp_path):
    config = {
        "desktop": {"runtime": "/runtime/container", "image": "shinbot:desktop"},
        "ports": {"computer": 8082, "browser": 8083},
    }
    args = create_command(tmp_path, config, tmp_path / "desktop.env")
    assert "127.0.0.1:8082:8082" in args
    assert "127.0.0.1:8083:8083" in args
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--volume"]
    assert mounts == [
        f"{tmp_path / 'workspace'}:/workspace",
        f"{container_name(tmp_path)}-data:/data",
        f"{tmp_path / 'desktop-data/computer'}:/seed/computer:ro",
    ]
    assert "--env-file" in args
    assert "TOKEN" not in json.dumps(args)


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "click", "x": -1, "y": 2},
        {"operation": "press", "key": "Return; rm"},
        {"operation": "launch", "application": "sh"},
        {"operation": "type", "text": "bad\0text"},
    ],
)
def test_desktop_rejects_invalid_inputs(args):
    with pytest.raises(ValueError):
        desktop.validate(args)


async def test_desktop_browser_shortcut_requires_human_control(screen, monkeypatch):
    launch = AsyncMock()
    monkeypatch.setattr(browser, "select_profile", launch)
    with pytest.raises(HTTPException) as missing:
        await browser.open_desktop_browser()
    assert missing.value.status_code == 423
    monkeypatch.setattr(browser, "lease", {"kind": "agent", "owner": "task"})
    with pytest.raises(HTTPException):
        await browser.open_desktop_browser()
    launch.assert_not_called()


async def test_desktop_browser_shortcut_reuses_profile_and_opens_links_in_new_tabs(screen, monkeypatch):
    await browser.takeover({"owner": "device"})
    tab = AsyncMock()
    context = AsyncMock()
    context.new_page.return_value = tab
    launch = AsyncMock()
    monkeypatch.setattr(browser, "browser", context)
    monkeypatch.setattr(browser, "page", tab)
    monkeypatch.setattr(tab, "is_closed", lambda: False)
    monkeypatch.setattr(tab, "set_default_timeout", lambda _: None)
    monkeypatch.setattr(browser, "active_browser", "group-project")
    monkeypatch.setattr(browser, "active_backend", "camoufox")
    monkeypatch.setattr(browser, "select_profile", launch)
    await browser.open_desktop_browser()
    launch.assert_awaited_with("group-project", "camoufox")
    context.new_page.assert_not_called()
    tab.bring_to_front.assert_awaited_once()
    await browser.open_desktop_browser("https://example.org/?q=a&b=c")
    context.new_page.assert_awaited_once()
    tab.goto.assert_awaited_once_with(
        "https://example.org/?q=a&b=c", wait_until="domcontentloaded", timeout=20000
    )
    assert desktop.owner() == "device"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "--profile=/tmp/other",
        "https://user:secret@example.org",
        "https://example.org/\nfoo",
        "http://",
        "x" * 9001,
    ],
)
def test_desktop_launcher_rejects_non_web_targets(url):
    from shinbot.services.desktop_link import validate_url

    with pytest.raises(ValueError):
        validate_url(url)


def test_desktop_launcher_sends_literal_url_without_a_shell():
    import socket
    import tempfile
    import threading
    from pathlib import Path

    from shinbot.services.desktop_link import request_open

    received = []
    with (
        tempfile.TemporaryDirectory(dir="/tmp") as temp,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server,
    ):
        path = Path(temp) / "browser.sock"
        server.bind(str(path))
        server.listen(1)

        def respond():
            client, _ = server.accept()
            with client:
                received.append(json.loads(client.recv(9000)))
                client.sendall(b'{"ok":true}\n')

        thread = threading.Thread(target=respond)
        thread.start()
        url = "https://example.org/?q=$(touch%20/tmp/no)&more='test'"
        request_open(url, path)
        thread.join(3)
    assert received == [{"url": url}]
