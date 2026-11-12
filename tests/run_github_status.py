"""Verify external GitHub state changes through the real inbox UI and gateway."""

import asyncio
import copy
import json
import socket
import tempfile
import threading
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from shinbot import integrations
from shinbot.app import create_app
from shinbot.auth import hash_password
from shinbot.config import Config

OUT = Path(__file__).resolve().parents[1] / "test-results/github-status"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-github-status-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-status-test-password"))
        core.db.set("preferences", {**core.db.setting("preferences"), "provider": "test", "model": "test"})
        session = core.auth.session()
        core.plugins.stage_from_catalog("official", "projects", "1.1.0")
        core.plugins.activate("projects", "1.1.0")
        core.inbox.configuration({"repositories": ["example/project"], "token": "fixture-token"})
        remote = {
            number: {
                "number": number,
                "title": title,
                "body": "Check the current GitHub status.",
                "state": "open",
                "repository_url": "https://api.github.com/repos/example/project",
                "updated_at": "2026-10-27T09:00:00Z",
                "user": {"login": "contributor"},
                "assignees": [{"login": "tester"}],
                **({"pull_request": {}, "merged": False} if number != 76 else {}),
            }
            for number, title in (
                (73, "Add account sign-in"),
                (75, "Accept optional null parameters"),
                (76, "Fix timezone display"),
            )
        }
        unavailable = False
        checks = []

        def handler(request):
            assert request.url.host == "api.github.com"
            path = request.url.path
            if path == "/user":
                return httpx.Response(200, json={"login": "tester"})
            if path == "/search/issues":
                return httpx.Response(
                    200, json={"items": [copy.deepcopy(r) for r in remote.values() if r["state"] == "open"]}
                )
            if path == "/notifications":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "repository": {"full_name": "example/project"},
                            "reason": "mention",
                            "unread": True,
                            "updated_at": remote[75]["updated_at"],
                            "subject": {
                                "url": "https://api.github.com/repos/example/project/pulls/75",
                                "title": remote[75]["title"],
                            },
                        }
                    ],
                )
            if path.endswith("/comments"):
                return httpx.Response(
                    200, json=[{"user": {"login": "tester"}, "body": "Reviewed on GitHub."}]
                )
            number = int(path.split("/")[-1])
            checks.append(number)
            if unavailable and number == 75:
                return httpx.Response(503, json={"message": "temporary provider failure"})
            return httpx.Response(200, json=copy.deepcopy(remote[number]))

        original_client = integrations.client
        integrations.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def runtime(method, path, **kwargs):
            if path == "/v1/providers":
                return [
                    {
                        "id": "test",
                        "name": "Test",
                        "configured": True,
                        "models": [{"id": "test", "name": "Test model"}],
                    }
                ]
            if path == "/v1/health":
                return {"held": False}
            return {}

        core.coordinator.request = runtime
        asyncio.run(core.inbox.sync())
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def open_inbox():
                    page.locator('[data-view="projects"]').click()
                    page.get_by_role("tab", name="GitHub inbox", exact=True).click()

                def row(number):
                    return page.locator(".inbox-item").filter(
                        has=page.locator(f'[data-issue="example/project#{number}"]')
                    )

                page.goto(origin)
                open_inbox()
                expect(page.locator(".inbox-item")).to_have_count(3)
                remote[75].update(
                    state="closed",
                    merged=True,
                    merged_at="2026-10-27T09:39:39Z",
                    closed_at="2026-10-27T09:39:39Z",
                    updated_at="2026-10-27T09:39:39Z",
                )
                remote[76].update(state="closed", updated_at="2026-10-27T09:40:00Z")
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(page.locator(".inbox-item")).to_have_count(1)
                expect(row(73)).to_be_visible()
                page.locator('[data-inbox-view="all"]').click()
                expect(row(75)).to_contain_text("Pull request · Merged")
                expect(row(76)).to_contain_text("Issue · Closed")
                page.screenshot(path=str(OUT / "merged-light.png"), full_page=True)
                page.evaluate("window.ShinBotTheme.set('dark')")
                page.evaluate("Promise.all(document.getAnimations().filter(a=>a.effect.getComputedTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})))")
                page.screenshot(path=str(OUT / "merged-dark.png"), full_page=True)
                page.reload()
                open_inbox()
                page.locator('[data-inbox-view="all"]').click()
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(row(75)).to_contain_text("Pull request · Merged")
                assert "unknown" not in page.locator("#inbox-list").inner_text().lower()
                row(75).locator("[data-issue]").click()
                expect(page.get_by_role("dialog")).to_contain_text("Merged")
                page.get_by_role("button", name="Close dialog", exact=True).click()
                expect(row(75)).to_contain_text("Merged")
                unavailable = True
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(row(75)).to_contain_text("Merged · Last known activity")
                expect(page.locator("#page")).to_contain_text("Some activity may be missing")
                unavailable = False
                remote[76].update(
                    state="open",
                    closed_at=None,
                    updated_at="2026-10-27T10:00:00Z",
                )
                row(76).locator("[data-issue]").click()
                expect(page.get_by_role("dialog")).to_contain_text("Open")
                page.get_by_role("button", name="Close dialog", exact=True).click()
                expect(row(76)).to_contain_text("Issue · Open")
                expect(row(75)).to_contain_text("Pull request · Merged")
                page.locator('[data-inbox-view="attention"]').click()
                expect(row(76)).to_be_visible()
                expect(row(75)).to_have_count(0)
                page.set_viewport_size({"width": 390, "height": 844})
                expect(page.locator("#navigation-panel")).not_to_be_visible()
                page.screenshot(path=str(OUT / "reopened-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert checks.count(75) >= 4 and not errors, (checks, errors)
                browser.close()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "verified": [
                            "manual merge",
                            "issue closure without notification",
                            "repeat refresh and reload",
                            "fresh detail updates list",
                            "upstream failure retains verified state",
                            "reopening returns to attention",
                            "light/dark/mobile",
                        ],
                        "screenshots": str(OUT),
                    }
                )
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            integrations.client = original_client


if __name__ == "__main__":
    main()
