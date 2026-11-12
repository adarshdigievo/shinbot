"""Check live notifications, project chat destinations and avatar spacing in Chromium."""

import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

from shinbot.app import create_app
from shinbot.auth import hash_password
from shinbot.config import Config
from shinbot.db import packed

OUT = Path(__file__).resolve().parents[1] / "test-results/notifications"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-notifications-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-notification-test-password"))
        session = core.auth.session()
        core.plugins.stage_from_catalog("official", "projects", "1.1.0")
        core.plugins.activate("projects", "1.1.0")
        core.db.execute("DELETE FROM events")
        builder = core.bots.save(
            {"name": "Builder", "color": "blue", "description": "Implements features and fixes"}
        )
        reviewer = core.bots.save(
            {"name": "Reviewer", "color": "violet", "description": "Reviews code and tests"}
        )
        core.db.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?)", ("shinbot", "shinbot", "projects/shinbot", "main", 1)
        )
        tasks = []
        for i, (title, bot, status) in enumerate(
            [
                ("Fix concurrent token refresh", builder, "running"),
                ("Review OAuth callback validation", reviewer, "waiting"),
                ("Add retry coverage", builder, "completed"),
            ]
        ):
            task = core.tasks.create(
                "Demo instruction: " + title,
                title=title,
                bot_id=bot["id"],
                provider="fixture",
                model="fixture-model",
            )
            worktree = f"worktree-{i}"
            path = f".shinbot-worktrees/shinbot/{worktree}"
            core.db.execute(
                "UPDATE tasks SET status=?,workspace_id=?,workspace_path=? WHERE id=?",
                (status, worktree, path, task["id"]),
            )
            core.db.execute(
                "UPDATE messages SET status=?,answer=? WHERE task_id=?",
                (status, "Demo reply for " + title if status == "completed" else None, task["id"]),
            )
            core.db.execute(
                "INSERT INTO project_workspaces(id,project_id,request_id,request_hash,base_commit,branch,path,state,created,task_id,prompt,bot_id,title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    worktree,
                    "shinbot",
                    "demo:" + worktree,
                    "hash",
                    "f" * 40,
                    "shinbot/" + worktree,
                    path,
                    "ready",
                    i,
                    task["id"],
                    title,
                    bot["id"],
                    title,
                ),
            )
            tasks.append({**task, "bot_id": bot["id"], "status": status})
        core.db.execute(
            "INSERT INTO actions(id,task_id,epoch,tool,args,hash,status,created,expires) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "approval",
                tasks[1]["id"],
                "epoch",
                "run_command",
                packed({"command": "pytest tests/test_oauth.py"}),
                "hash",
                "pending",
                time.time(),
                time.time() + 3600,
            ),
        )
        core.db.event("completed", "Tests are ready", tasks[2]["id"])
        core.inbox.configuration({"repositories": ["demo/shinbot"], "token": "fixture-token"})
        item = core.inbox.normalize(
            {
                "number": 84,
                "title": "Review private repository permissions",
                "updated_at": "2026-10-26T10:00:00Z",
                "body": "A demo issue for the notification test.",
            },
            "demo/shinbot",
            "tester",
            ["review_requested"],
        )
        core.db.execute(
            "INSERT INTO github_items VALUES (?,?,?,?,?)",
            (item["id"], item["repo"], item["number"], packed(item), time.time()),
        )
        core.db.set("github-inbox-sync", {"synced_at": time.time(), "login": "tester", "warnings": []})

        async def runtime(method, path, **kwargs):
            return [] if path == "/v1/providers" else {}

        async def issue_detail(key):
            return {**core.inbox.item(key), "links": [], "conversation": [], "more_comments": False}

        core.coordinator.request = runtime
        core.coordinator.ready = True
        core.inbox.detail = issue_detail
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            deadline = time.time() + 10
            while not server.started and time.time() < deadline:
                time.sleep(0.05)
            assert server.started
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                page.locator('[data-view="projects"]').click()
                expect(page.locator(".project-task")).to_have_count(3)
                expect(page.locator("#notification-count")).to_have_text("3")
                page.screenshot(path=str(OUT / "projects-notification-badge.png"))
                page.locator("#notifications-toggle").click()
                expect(page.locator(".notification-item")).to_have_count(3)
                expect(page.locator("#read-notifications")).to_be_enabled()
                page.screenshot(path=str(OUT / "notifications-open.png"))
                page.keyboard.press("Escape")
                expect(page.locator("#notifications-toggle")).to_be_focused()
                expect(page.locator("#notifications-menu")).not_to_have_attribute("open", "")
                # Opening a worktree chat selects that exact thread and its bot.
                page.locator(f'[data-project-chat="{tasks[1]["id"]}"]').click()
                expect(page.locator("#task-title")).to_have_text(tasks[1]["title"])
                expect(page.locator("#header-bot-name")).to_have_text("Reviewer")
                expect(page.locator(f'[data-task="{tasks[1]["id"]}"]')).to_have_attribute(
                    "aria-current", "true"
                )
                expect(page.locator("#messages")).to_contain_text("Demo instruction: " + tasks[1]["title"])
                expect(page.locator("#messages")).to_contain_text(".shinbot-worktrees/shinbot/worktree-1")
                expect(page.locator("#messages .approval")).to_have_count(1)
                expect(page.locator("#notification-count")).to_have_text("2")
                assert core.db.one("SELECT status FROM actions WHERE id='approval'")["status"] == "pending"
                page.screenshot(path=str(OUT / "open-chat-worktree-conversation.png"))
                # Avatar artwork has a smaller crop in every bot identity size.
                for selector in (".roster-avatar img", ".header-avatar img"):
                    assert page.locator(selector).first.evaluate(
                        "el => el.clientWidth / el.parentElement.clientWidth < 1.3"
                    )
                page.locator('[data-view="projects"]').click()
                page.locator("#notifications-toggle").click()
                page.locator('[data-notification="github:' + item["id"] + '"]').click()
                expect(page.get_by_role("dialog")).to_contain_text(item["title"])
                expect(page.get_by_role("tab", name="GitHub inbox", exact=True)).to_have_attribute(
                    "aria-selected", "true"
                )
                page.get_by_role("button", name="Close dialog").click()
                expect(page.locator("#notification-count")).to_have_text("1")
                # Another tab receives read receipts through the existing live stream.
                peer = context.new_page()
                peer.goto(origin)
                expect(peer.locator("#notification-count")).to_have_text("1")
                page.locator("#notifications-toggle").click()
                page.get_by_role("button", name="Mark all read").click()
                expect(page.locator("#notification-count")).to_be_hidden()
                expect(page.locator("#read-notifications")).to_be_disabled()
                expect(peer.locator("#notification-count")).to_be_hidden()
                # A new event appears while Projects stays open.
                page.keyboard.press("Escape")
                core.db.event("failed", "A demo failure", tasks[0]["id"])
                expect(page.locator("#notification-count")).to_have_text("1")
                page.reload()
                expect(page.locator("#notification-count")).to_have_text("1")
                page.locator("#notifications-toggle").click()
                page.locator(".notification-item.unread").click()
                expect(page.locator("#task-title")).to_have_text(tasks[0]["title"])
                expect(page.locator("#header-bot-name")).to_have_text("Builder")
                expect(page.locator("#notification-count")).to_be_hidden()
                page.locator('[data-view="projects"]').click()
                page.evaluate("window.ShinBotTheme.set('dark')")
                page.locator("#notifications-toggle").click()
                page.screenshot(path=str(OUT / "notifications-dark.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                expect(page.locator("#navigation-panel")).to_be_hidden()
                page.screenshot(path=str(OUT / "notifications-mobile.png"))
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                box = page.locator(".notifications-panel").bounding_box()
                assert box["x"] >= 0 and box["x"] + box["width"] <= 390
                page.keyboard.press("Escape")
                core.db.execute("DELETE FROM events")
                core.db.execute("UPDATE actions SET status='completed'")
                page.locator("#notifications-toggle").click()
                expect(page.get_by_role("heading", name="You’re all caught up")).to_be_visible()
                page.screenshot(path=str(OUT / "notifications-empty-mobile.png"))
                assert not errors, errors
                peer.close()
                browser.close()
            print(
                "PASS live notification counts, exact worktree chats and bots, read persistence, approvals unchanged, GitHub destination, light/dark/mobile and avatar spacing",
                flush=True,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    main()
