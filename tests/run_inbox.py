"""Run the integration views against an isolated local gateway and mocked provider APIs."""

import json
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from shinbot import integrations
from shinbot.app import create_app
from shinbot.auth import hash_password
from shinbot.config import Config
from shinbot.db import packed

OUT = Path(__file__).resolve().parents[1] / "test-results/inbox"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-inbox-ui-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-inbox-test-password"))
        session = core.auth.session()
        for plugin, version in [("projects", "1.1.0"), ("calendar", "1.0.0")]:
            core.plugins.stage_from_catalog("official", plugin, version)
            core.plugins.activate(plugin, version)
        for key, name, created in [("engine-demo", "Engine", 2), ("docs-demo", "Docs", 1)]:
            core.db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?)",
                (key, name, "projects/" + name.lower(), "main", created),
            )
        core.inbox.configuration(
            {"repositories": ["example/engine"], "organizations": ["example-org"], "token": "fixture-token"}
        )
        now = datetime.now(timezone.utc)
        for n, title, reasons in [
            (21, "Review retry behavior before the release", ["review_requested"]),
            (19, "Fix a missing timezone in scheduled jobs", ["assign"]),
            (17, "Clarify setup instructions for new contributors", ["mention", "subscribed"]),
        ]:
            raw = {
                "number": n,
                "title": title,
                "body": "Please check the linked change and summarize your findings.\n\nA fixture issue for this isolated UI test.",
                "state": "open",
                "updated_at": now.isoformat(),
                "user": {"login": "maintainer"},
                "labels": [{"name": "needs-review"}],
                **({"pull_request": {}} if n == 21 else {}),
            }
            item = core.inbox.normalize(raw, "example/engine", "tester", reasons)
            core.db.execute(
                "INSERT INTO github_items VALUES (?,?,?,?,?)",
                (item["id"], item["repo"], item["number"], packed(item), time.time()),
            )
        core.db.set("github-inbox-sync", {"synced_at": time.time(), "login": "tester", "warnings": []})
        core.calendar.vault.update(
            "google-calendar",
            {
                "client_id": "fixture.apps.googleusercontent.com",
                "client_secret": "fixture-secret",
                "refresh_token": "fixture-refresh",
                "access_token": "fixture-access",
                "expires": str(time.time() + 3600),
            },
        )

        def handler(req):
            if req.url.host == "www.googleapis.com":
                if req.url.path.endswith("calendarList"):
                    return httpx.Response(
                        200,
                        json={
                            "items": [
                                {"id": "primary", "summary": "Personal calendar", "primary": True},
                                {"id": "work@example.com", "summary": "Work"},
                            ]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "event",
                                "summary": "Weekly engineering review",
                                "location": "Meeting room 2",
                                "start": {"dateTime": (now + timedelta(hours=1)).isoformat()},
                                "end": {"dateTime": (now + timedelta(hours=2)).isoformat()},
                                "htmlLink": "https://calendar.google.com/calendar/event?eid=fixture",
                            },
                            {
                                "id": "day",
                                "summary": "Release day",
                                "start": {"date": now.date().isoformat()},
                                "end": {"date": (now + timedelta(days=1)).date().isoformat()},
                            },
                        ]
                    },
                )
            if req.url.path.endswith("comments"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "user": {"login": "reviewer"},
                            "body": "Please check recovery after a connection failure.",
                        }
                    ],
                )
            n = int(req.url.path.split("/")[-1])
            item = core.inbox.item("example/engine#" + str(n))
            return httpx.Response(
                200,
                json={
                    "number": n,
                    "title": item["title"],
                    "body": item["body"],
                    "state": "open",
                    "updated_at": now.isoformat(),
                    "user": {"login": "maintainer"},
                    **({"pull_request": {}} if item["kind"] == "pr" else {}),
                },
            )

        original_client = integrations.client
        integrations.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def runtime(method, path, **kwargs):
            if path == "/v1/providers":
                return [
                    {
                        "id": "openai",
                        "name": "OpenAI",
                        "configured": True,
                        "models": [{"id": "gpt-6-sol", "name": "Test model"}],
                    }
                ]
            if path == "/v1/health":
                return {"held": False}
            return {}

        core.coordinator.request = runtime
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(origin)
                page.locator('[data-view="plugins"]').click()
                expect(page.locator('[data-install="projects"]')).to_have_count(1)
                expect(page.locator('[data-install="projects"]')).to_have_attribute("data-version", "1.1.0")
                page.locator('[data-view="projects"]').click()
                expect(page.get_by_role("heading", name="Projects", exact=True)).to_be_visible()
                workspaces = page.get_by_role("tab", name="Workspaces", exact=True)
                inbox = page.get_by_role("tab", name="GitHub inbox", exact=True)
                expect(workspaces).to_have_attribute("aria-selected", "true")
                expect(inbox).to_be_visible()
                expect(page.locator("[data-project]")).to_have_count(2)
                page.locator('[data-project="docs-demo"]').click()
                expect(page.locator(".project-heading")).to_contain_text("Docs")
                page.screenshot(path=str(OUT / "workspaces-default.png"), full_page=True)
                workspaces.focus()
                workspaces.press("ArrowRight")
                expect(inbox).to_have_attribute("aria-selected", "true")
                expect(inbox).to_be_focused()
                inbox.press("ArrowLeft")
                expect(page.locator(".project-heading")).to_contain_text("Docs")
                expect(workspaces).to_be_focused()
                inbox.click()
                expect(page.locator(".inbox-item")).to_have_count(3)
                page.screenshot(path=str(OUT / "github-inbox-desktop.png"), full_page=True)
                page.evaluate("window.ShinBotTheme.set('dark')")
                page.screenshot(path=str(OUT / "github-inbox-dark.png"), full_page=True)
                page.evaluate("window.ShinBotTheme.set('light')")
                page.locator('[data-inbox-view="review"]').click()
                expect(page.locator(".inbox-item")).to_have_count(1)
                page.locator("[data-issue]").click()
                expect(page.get_by_text("Please check recovery after a connection failure.")).to_be_visible()
                page.locator("#issue-remind").click()
                page.locator("dialog").last.get_by_label("When (your local time)").fill(
                    (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
                )
                page.get_by_role("button", name="Set reminder", exact=True).click()
                expect(page.get_by_text("Reminder saved in Jobs and Calendar.")).to_be_visible()
                page.locator("dialog").get_by_role("button", name="Close dialog").click()
                page.locator('[data-view="calendar"]').click()
                expect(page.locator(".agenda-event")).to_have_count(3)
                page.screenshot(path=str(OUT / "calendar-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(OUT / "calendar-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.get_by_role("button", name="Choose calendars", exact=True).click()
                expect(page.get_by_label("Personal calendar")).to_be_checked()
                page.get_by_label("Work", exact=True).check()
                page.get_by_role("button", name="Save selection", exact=True).click()
                page.locator('[data-view="projects"]').click()
                page.get_by_role("button", name="Follow repositories", exact=True).click()
                token = page.get_by_label("GitHub token", exact=True)
                expect(token).to_have_attribute("type", "password")
                expect(token).to_have_value("")
                page.screenshot(path=str(OUT / "secure-github-entry.png"), full_page=True)
                page.get_by_role("button", name="Close dialog").click()
                page.get_by_role("tab", name="Workspaces", exact=True).click()
                expect(page.locator(".project-heading")).to_contain_text("Docs")
                page.get_by_role("tab", name="GitHub inbox", exact=True).click()
                expect(page.locator(".inbox-item")).to_have_count(1)
                page.locator('[data-inbox-view="attention"]').click()
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(OUT / "github-inbox-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    "Mobile inbox overflow"
                )
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator('[data-view="settings"]').click()
                page.get_by_role("button", name="Credentials", exact=True).click()
                expect(page.get_by_role("heading", name="Credentials", exact=True)).to_be_visible()
                assert "fixture-token" not in page.locator("body").inner_text()
                page.screenshot(path=str(OUT / "secrets-manager.png"), full_page=True)
                page.get_by_role("button", name="Close dialog").click()
                page.locator('[data-view="projects"]').click()
                page.locator("[data-ask]").first.click()
                page.get_by_label("What should the bot check?").fill(
                    "Check the current issue and explain what needs review."
                )
                page.get_by_role("button", name="Start conversation", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Ask a bot about this issue", exact=True)
                ).to_have_count(0)
                assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1
                assert "connection failure" in core.db.one("SELECT payload FROM messages")["payload"]
                page.reload()
                page.locator('[data-view="projects"]').click()
                expect(page.get_by_role("tab", name="Workspaces", exact=True)).to_have_attribute(
                    "aria-selected", "true"
                )
                expect(page.locator("[data-project]")).to_have_count(2)
                assert not errors, errors
                browser.close()
            print(
                json.dumps(
                    {
                        "passed": "Inbox filters, issue comments, reminder scheduling, calendar selection, secure entry, coding view, mobile overflow and secrets manager",
                        "screenshots": str(OUT),
                    }
                )
            )
        finally:
            integrations.client = original_client
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    main()
