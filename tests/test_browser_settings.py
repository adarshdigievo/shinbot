from unittest.mock import AsyncMock

import httpx
import pytest

from shinbot.services.browser_backends import profile_path


def test_existing_browser_choice_survives_upgrade_and_partial_settings(application, owner):
    from shinbot.db import Database

    core = application.state.core
    old = core.db.setting("preferences")
    old.pop("browser_backend")
    core.db.set("preferences", old)
    assert "browser_backend" not in Database(core.db.path).setting("preferences")
    saved = owner.put("/api/settings", json=old)
    assert saved.status_code == 200
    assert saved.json()["browser_backend"] == "chromium"
    assert Database(core.db.path).setting("preferences")["browser_backend"] == "chromium"


def test_browser_defaults_and_unavailable_selection(application, owner, monkeypatch):
    core = application.state.core
    preferences = owner.get("/api/settings").json()
    assert preferences["browser_backend"] == "camoufox"
    assert preferences["browser_close_on_finish"] is True
    remote = AsyncMock(return_value={"backends": [{"id": "chromium", "ready": False}]})
    monkeypatch.setattr(core, "remote", remote)
    rejected = owner.put("/api/settings", json={**preferences, "browser_backend": "chromium"})
    assert rejected.status_code == 400
    assert owner.get("/api/settings").json() == preferences
    remote.return_value["backends"][0]["ready"] = True
    changed = owner.put(
        "/api/settings", json={**preferences, "browser_backend": "chromium", "browser_close_on_finish": False}
    )
    assert changed.status_code == 200
    assert owner.get("/api/settings").json()["browser_backend"] == "chromium"
    assert core.db.setting("preferences")["browser_close_on_finish"] is False
    assert (
        owner.put("/api/settings", json={**preferences, "browser_backend": "untrusted-command"}).status_code
        == 422
    )


async def test_gateway_uses_saved_backend_without_trusting_agent_choice(application):
    core = application.state.core
    core.db.set("preferences", {**core.db.setting("preferences"), "browser_backend": "camoufox"})
    requests = []

    def receive(request):
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    core.service_client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    try:
        body = {"browser_id": "private-bot", "backend": "agent-browser"}
        for path in ["/select", "/assess", "/takeover", "/execute"]:
            await core.remote("browser", path, body)
        assert all(r == {"browser_id": "private-bot", "backend": "camoufox"} for r in requests)
        assert body["backend"] == "agent-browser"
    finally:
        await core.service_client.aclose()
        core.service_client = None


def test_profiles_keep_existing_chromium_data_and_isolate_other_backends(tmp_path):
    old_profile = tmp_path / "profile"
    old_profile.mkdir()
    (old_profile / "Cookies").write_text("existing sign-in")
    assert profile_path(tmp_path, "chromium", "shared") == old_profile
    directories = {
        profile_path(tmp_path, backend, profile)
        for backend in ["chromium", "camoufox", "agent-browser"]
        for profile in ["shared", "private-bot"]
    }
    assert len(directories) == 6
    assert (old_profile / "Cookies").read_text() == "existing sign-in"


@pytest.mark.parametrize("enabled,expected", [(True, True), (False, False)])
async def test_completed_turn_respects_browser_cleanup_preference(
    application, monkeypatch, enabled, expected
):
    from test_coordinator import Runtime

    core = application.state.core
    core.db.set("preferences", {**core.db.setting("preferences"), "browser_close_on_finish": enabled})
    runtime = Runtime()

    async def release_after_save(service, path, payload):
        if path == "/agent/release":
            assert core.tasks.detail(payload["task_id"])["messages"][0]["answer"] == "done"
        return {}

    remote = AsyncMock(side_effect=release_after_save)
    monkeypatch.setattr(core.coordinator, "request", runtime)
    monkeypatch.setattr(core, "remote", remote)
    task = core.tasks.create("Browse a page")
    await core.coordinator.tick()
    runtime.submissions[1] = "done"
    await core.coordinator.tick()
    releases = [call.args[2] for call in remote.await_args_list if call.args[1] == "/agent/release"]
    assert releases == [{"task_id": task["id"], "close": expected}]


async def test_queued_followup_keeps_browser_open_until_its_reply_finishes(application, monkeypatch):
    from test_coordinator import Runtime

    core = application.state.core
    runtime = Runtime()
    remote = AsyncMock(return_value={})
    monkeypatch.setattr(core.coordinator, "request", runtime)
    monkeypatch.setattr(core, "remote", remote)
    task = core.tasks.create("Browse")
    await core.coordinator.tick()
    core.tasks.message(task["id"], "Now read the next page", "queued-next")
    runtime.submissions[1] = "done"
    await core.coordinator.tick()
    releases = [call.args[2] for call in remote.await_args_list if call.args[1] == "/agent/release"]
    assert releases[-1]["close"] is False
    await core.coordinator.tick()
    runtime.submissions[2] = "done"
    await core.coordinator.tick()
    releases = [call.args[2] for call in remote.await_args_list if call.args[1] == "/agent/release"]
    assert releases[-1]["close"] is True
