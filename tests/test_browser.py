import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from shinbot.services import browser as service
from shinbot.services.common import Journal


@pytest.fixture
def browser_state(monkeypatch, tmp_path):
    locator = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    page = SimpleNamespace(
        url="https://example.org",
        locator=lambda _: locator,
        is_closed=lambda: False,
        title=AsyncMock(side_effect=RuntimeError("navigation race")),
    )
    for name, value in {
        "lock": asyncio.Lock(),
        "page": page,
        "browser": SimpleNamespace(pages=[page], close=AsyncMock()),
        "backend_driver": None,
        "active_backend": "chromium",
        "lease": None,
        "human_pending": False,
        "cancelled": set(),
        "observations": {"task": (0, page.url)},
        "control_generation": 0,
        "last_agent": "task",
        "active_browser": "shared",
        "journal": Journal(tmp_path),
        "select_profile": AsyncMock(),
    }.items():
        monkeypatch.setattr(service, name, value)
    return locator


@pytest.mark.asyncio
async def test_successful_click_survives_failed_observation_and_replay(browser_state):
    payload = {
        "id": "click",
        "approval_mode": "full",
        "task_id": "task",
        "epoch": "e",
        "args": {"operation": "click", "selector": "button"},
    }
    result = await service.execute(payload)
    assert "completed" in result["content"][0]["text"]
    assert not result.get("isError")
    # DOM no longer matches after the click; a delivery retry returns the old receipt without acting.
    browser_state.count.return_value = 0
    assert await service.execute(payload) == result
    assert browser_state.click.await_count == 2  # trial and the single real click


async def test_shared_screenshot_survives_close_and_service_restart(browser_state, tmp_path, monkeypatch):
    import base64

    monkeypatch.setattr(service, "DATA", tmp_path)
    pixels = b"\x89PNG\r\n\x1a\nviewport"
    service.page.screenshot = AsyncMock(return_value=pixels)
    payload = {
        "id": "share",
        "task_id": "task",
        "epoch": "e",
        "approval_mode": "auto",
        "args": {"operation": "screenshot"},
    }
    result = await service.execute(payload)
    assert result["file"]["preview"] is True
    assert service.page.screenshot.await_count == 1
    await service.close_browser()
    service.journal.db.close()
    service.journal = Journal(tmp_path)
    assert await service.execute(payload) == result
    assert base64.b64decode((await service.attachment_file(result["file"]["digest"]))["data"]) == pixels
    # A new screenshot must still respect human takeover.
    monkeypatch.setattr(service, "lease", {"kind": "human", "owner": "owner"})
    with pytest.raises(HTTPException) as caught:
        await service.execute({**payload, "id": "new-screenshot"})
    assert caught.value.status_code == 423


@pytest.mark.asyncio
async def test_missing_selector_never_records_an_effect(browser_state):
    browser_state.count.return_value = 0
    with pytest.raises(HTTPException) as caught:
        await service.execute(
            {
                "id": "missing",
                "task_id": "task",
                "epoch": "e",
                "args": {"operation": "click", "selector": "#missing"},
            }
        )
    assert caught.value.status_code == 400
    assert not service.journal.db.execute("SELECT 1 FROM effects").fetchone()
    browser_state.click.assert_not_awaited()


async def test_unsafe_port_is_a_failed_navigation_with_a_replayable_result(browser_state):
    service.page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_UNSAFE_PORT"))
    payload = {
        "id": "unsafe-port",
        "task_id": "task",
        "epoch": "e",
        # A public address: loopback now waits for the owner before any navigation.
        "args": {"operation": "goto", "url": "http://93.184.216.34:9/"},
    }
    result = await service.execute(payload)
    assert result["isError"] and "could not be reached" in result["content"][0]["text"]
    assert service.journal.receipt(payload["id"])["status"] == "completed"
    assert await service.execute(payload) == result
    service.page.goto.assert_awaited_once()


@pytest.mark.parametrize("stale", [False, True])
async def test_predispatch_browser_rejection_reaches_agent_without_halting_chat(
    application, browser_state, monkeypatch, stale
):
    import httpx
    from test_actions import action

    core = application.state.core
    task = core.tasks.create("Use the browser", options={"approval_mode": "full"})
    service.last_agent = task["id"]
    service.observations = {task["id"]: (-1 if stale else 0, service.page.url)}
    browser_state.count.return_value = 0

    async def remote(name, path, body=None, **kwargs):
        if path != "/execute":
            return {}
        try:
            return await service.execute(body)
        except HTTPException as exc:
            request = httpx.Request("POST", "http://browser/execute")
            response = httpx.Response(exc.status_code, json={"detail": exc.detail}, request=request)
            response.raise_for_status()

    monkeypatch.setattr(core, "remote", remote)
    payload = action(core, task, "browser", {"operation": "click", "selector": "#missing"})
    await core.action(payload)
    await asyncio.gather(*core.running_actions.values())
    result = await core.action(payload)
    assert result["status"] == "failed" and "Observe again" in result["error"]
    assert core.tasks.detail(task["id"])["status"] != "uncertain"
    assert service.journal.receipt(payload["id"])["status"] == "missing"
    browser_state.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_observation_failure_is_retryable(browser_state):
    result = await service.execute(
        {"id": "read", "task_id": "task", "epoch": "e", "args": {"operation": "observe"}}
    )
    assert result["isError"]
    assert service.journal.db.execute("SELECT status FROM effects").fetchone()[0] == "completed"


@pytest.mark.asyncio
async def test_takeover_selects_profile_under_human_lease(browser_state):
    async def select(profile, backend=None):
        assert service.lock.locked()
        assert service.human_pending
        service.active_browser = profile

    service.select_profile.side_effect = select
    result = await service.takeover({"owner": "owner", "browser_id": "bot-private"})
    assert result["browser_id"] == "bot-private"
    assert not service.human_pending


@pytest.mark.asyncio
async def test_lease_status_never_waits_for_running_navigation(browser_state):
    async with service.lock:
        result = await asyncio.wait_for(service.lease_state(), timeout=0.2)
    assert result["lease"] is None and result["human_pending"] is False
    assert result["browser_id"] == "shared" and result["url"] == "https://example.org"
    service.page.title.assert_not_awaited()


async def test_native_preview_does_not_take_control_or_wait_for_navigation(browser_state, monkeypatch):
    monkeypatch.setenv("SHINBOT_BROWSER_VIEW", "native")
    service.page.screenshot = AsyncMock(return_value=b"jpeg")
    async with service.lock:
        result = await asyncio.wait_for(service.view(), timeout=0.2)
    assert result == {"screenshot": "anBlZw==", "empty": False, "generation": 0}
    assert service.lease is None
    service.select_profile.assert_not_awaited()


async def test_native_takeover_stays_in_background_and_blocks_agent(browser_state, monkeypatch):
    monkeypatch.setenv("SHINBOT_BROWSER_VIEW", "native")
    service.page.bring_to_front = AsyncMock()
    await service.takeover({"owner": "owner"})
    service.page.bring_to_front.assert_not_awaited()
    with pytest.raises(HTTPException) as caught:
        await service.execute(
            {"id": "blocked", "task_id": "task", "epoch": "e", "args": {"operation": "observe"}}
        )
    assert caught.value.status_code == 423


async def test_native_idle_preview_and_selection_do_not_launch_a_browser(browser_state, monkeypatch):
    monkeypatch.setenv("SHINBOT_BROWSER_VIEW", "native")
    service.browser = service.page = None
    await service.select_browser({"browser_id": "private-bot"})
    assert (await service.view())["empty"] is True
    assert service.active_browser == "private-bot"
    service.select_profile.assert_not_awaited()


@pytest.mark.parametrize(
    "owner,generation,held", [("other", 0, True), ("owner", -1, True), ("owner", 0, False)]
)
async def test_native_input_requires_current_human_lease(browser_state, monkeypatch, owner, generation, held):
    monkeypatch.setenv("SHINBOT_BROWSER_VIEW", "native")
    service.lease = {"kind": "human", "owner": "owner"} if held else None
    service.lease_touched = time.monotonic()
    service.page.keyboard = SimpleNamespace(insert_text=AsyncMock())
    with pytest.raises(HTTPException) as caught:
        await service.human_input(
            {"owner": owner, "generation": generation, "event": {"type": "text", "text": "private"}}
        )
    assert caught.value.status_code in {409, 423}
    service.page.keyboard.insert_text.assert_not_awaited()


async def test_native_input_and_release_reject_old_events(browser_state, monkeypatch):
    monkeypatch.setenv("SHINBOT_BROWSER_VIEW", "native")
    service.lease = {"kind": "human", "owner": "owner"}
    service.lease_touched = time.monotonic()
    service.page.keyboard = SimpleNamespace(insert_text=AsyncMock())
    payload = {"owner": "owner", "generation": 0, "event": {"type": "text", "text": "hello"}}
    await service.human_input(payload)
    service.page.keyboard.insert_text.assert_awaited_once_with("hello")
    await service.release({"owner": "owner"})
    with pytest.raises(HTTPException):
        await service.human_input(payload)
    service.page.keyboard.insert_text.assert_awaited_once()


async def test_gateway_owns_input_identity(owner, application, monkeypatch):
    remote = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(application.state.core, "remote", remote)
    response = owner.post(
        "/api/browser/input",
        json={"owner": "forged", "generation": 2, "event": {"type": "text", "text": "hello"}},
    )
    assert response.status_code == 200
    assert remote.call_args.args[2]["owner"] != "forged"
    assert remote.call_args.args[2]["generation"] == 2


@pytest.mark.asyncio
async def test_failed_takeover_does_not_leave_human_lease(browser_state):
    service.select_profile.side_effect = HTTPException(422, "Launch failed")
    with pytest.raises(HTTPException):
        await service.takeover({"owner": "owner", "browser_id": "bot-private"})
    assert service.lease is None and not service.human_pending


async def test_live_vnc_view_does_not_depend_on_preview_capture(browser_state, monkeypatch):
    monkeypatch.setenv("SHINBOT_HEADLESS", "0")
    service.page.screenshot = AsyncMock(side_effect=RuntimeError("Unable to capture screenshot"))
    monkeypatch.setattr(service, "vnc_password", "test-view-password")
    assert await service.view() == {"password": "test-view-password", "screenshot": ""}
    service.page.screenshot.assert_not_awaited()
    monkeypatch.setenv("SHINBOT_HEADLESS", "1")
    service.page.screenshot.side_effect = None
    service.page.screenshot.return_value = b"image"
    assert (await service.view())["screenshot"] == "aW1hZ2U="


async def test_live_view_connects_during_agent_action_without_touching_profile(browser_state, monkeypatch):
    monkeypatch.setenv("SHINBOT_HEADLESS", "0")
    monkeypatch.setattr(service, "vnc_password", "test-view-password")
    service.lease = {"kind": "agent", "owner": "task"}
    async with service.lock:
        result = await asyncio.wait_for(service.view(), timeout=0.2)
        activity = await asyncio.wait_for(service.lease_state(), timeout=0.2)
    assert result["password"] == "test-view-password"
    assert activity["lease"] == {"kind": "agent", "owner": "task"}
    assert activity["browser_id"] == "shared"
    service.select_profile.assert_not_awaited()
    assert service.lease == {"kind": "agent", "owner": "task"}


def test_browser_watch_auth_and_takeover_keeps_actual_profile(client, owner, application, monkeypatch):
    remote = AsyncMock(return_value={"lease": {"kind": "agent", "owner": "task"}})
    monkeypatch.setattr(application.state.core, "remote", remote)
    assert owner.get("/api/browser/activity").status_code == 200
    remote.assert_awaited_with("browser", "/lease", method="GET", timeout=5)
    assert owner.post("/api/browser/takeover", json={}).status_code == 200
    assert "browser_id" not in remote.call_args.args[2]
    assert owner.post("/api/browser/takeover", json={"bot_id": "default"}).status_code == 200
    assert remote.call_args.args[2]["browser_id"] == application.state.core.bots.get("default")["browser_id"]
    client.headers.pop("Authorization")
    client.cookies.clear()
    assert client.get("/api/browser/activity").status_code == 401
    assert client.get("/api/browser/view").status_code == 401


@pytest.mark.asyncio
async def test_running_browser_action_poll_does_not_contact_browser(application, monkeypatch):
    import hashlib
    import time

    from shinbot.db import packed

    core = application.state.core
    task = core.tasks.create("slow navigation")
    epoch = core.db.setting("epoch")
    args = {"operation": "goto", "url": "https://example.org"}
    digest = hashlib.sha256(
        packed({"tool": "browser", "args": args, "task": task["id"], "epoch": epoch}).encode()
    ).hexdigest()
    core.db.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "busy",
            task["id"],
            epoch,
            "browser",
            packed(args),
            digest,
            "executing",
            None,
            None,
            time.time(),
            time.time() + 100,
        ),
    )
    remote = AsyncMock(side_effect=AssertionError("Polling must not contact the busy browser"))
    monkeypatch.setattr(core, "remote", remote)
    result = await core.action(
        {"id": "busy", "task_id": task["id"], "epoch": epoch, "tool": "browser", "args": args}
    )
    assert result["status"] == "executing"
    remote.assert_not_awaited()


@pytest.mark.parametrize(
    "lease,owner,pending,closes",
    [
        ({"kind": "agent", "owner": "task"}, "task", False, True),
        (None, "task", False, True),
        ({"kind": "agent", "owner": "other"}, "task", False, False),
        ({"kind": "human", "owner": "user"}, "task", False, False),
        ({"kind": "agent", "owner": "task"}, "task", True, False),
        (None, "other", False, False),
    ],
)
async def test_completion_closes_only_its_owned_browser(
    browser_state, monkeypatch, lease, owner, pending, closes
):
    context = service.browser
    monkeypatch.setattr(service, "lease", lease)
    monkeypatch.setattr(service, "human_pending", pending)
    await service.release_agent({"task_id": owner, "close": True})
    assert context.close.await_count == int(closes)
    if closes:
        assert service.browser is service.page is service.lease is None
        assert not service.observations
    else:
        assert service.lease == lease


async def test_browser_can_stay_open_after_completion(browser_state, monkeypatch):
    monkeypatch.setattr(service, "lease", {"kind": "agent", "owner": "task"})
    await service.release_agent({"task_id": "task", "close": False})
    service.browser.close.assert_not_awaited()
    assert service.lease is None


async def test_explicit_close_is_replayable_without_reopening(browser_state):
    context = service.browser
    payload = {"id": "close", "task_id": "task", "epoch": "e", "args": {"operation": "close"}}
    result = await service.execute(payload)
    assert "Browser closed" in result["content"][0]["text"]
    assert await service.execute(payload) == result
    context.close.assert_awaited_once()
    service.select_profile.assert_not_awaited()


async def test_human_takeover_prevents_delayed_completion_cleanup(browser_state):
    await service.takeover({"owner": "human"})
    await service.release({"owner": "human"})
    await service.release_agent({"task_id": "task", "close": True})
    service.browser.close.assert_not_awaited()


async def test_close_cannot_claim_an_unowned_browser(browser_state, monkeypatch):
    monkeypatch.setattr(service, "last_agent", "other-task")
    with pytest.raises(HTTPException, match="does not own"):
        await service.execute(
            {"id": "unowned-close", "task_id": "task", "epoch": "e", "args": {"operation": "close"}}
        )
    service.browser.close.assert_not_awaited()
    assert service.journal.receipt("unowned-close")["status"] == "missing"
