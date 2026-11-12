"""Check chat history search, related activity, and operation status in Chromium."""

import base64
import json
import re
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

OUT = Path(__file__).resolve().parents[1] / "test-results/chat-workspace"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-chat-workspace-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-chat-workspace-password"))
        core.db.set("preferences", {**core.db.setting("preferences"), "provider": "test", "model": "test"})
        session = core.auth.session()
        research = core.bots.save({"name": "Research partner", "color": "violet"})
        old = core.tasks.create(
            "Find a quiet place for our weekend trip.", title="Weekend plans", bot_id=research["id"]
        )
        core.db.execute("UPDATE tasks SET updated=1,status='completed' WHERE id=?", (old["id"],))
        core.db.execute(
            "UPDATE messages SET status='completed',answer=? WHERE task_id=?",
            ("The riverside cafe has a terrace and opens at eight.", old["id"]),
        )
        for index in range(205):
            core.tasks.create("A recent conversation", title=f"Recent chat {index}")
        working = core.tasks.create(
            "Compare three places for the weekend.", title="Plan a weekend in Kyoto", bot_id=research["id"]
        )
        key = working["id"]
        core.db.execute("UPDATE tasks SET status='running' WHERE id=?", (key,))
        core.db.execute("UPDATE messages SET status='running',started=? WHERE task_id=?", (time.time(), key))
        for action_id, tool, args, status in (
            ("read", "read_file", {"path": "travel/preferences.md"}, "completed"),
            ("browse", "browser", {"operation": "goto", "url": "https://example.com/kyoto"}, "executing"),
        ):
            core.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    key,
                    "fixture",
                    tool,
                    json.dumps(args),
                    "fixture",
                    status,
                    json.dumps({"output": "Read the saved travel preferences."})
                    if status == "completed"
                    else None,
                    None,
                    time.time(),
                    time.time() + 600,
                ),
            )
        core.plugins.stage_from_catalog("official", "kanban", "1.0.0")
        core.plugins.activate("kanban", "1.0.0")
        card = core.board.save(
            {
                "title": "Weekend trip research",
                "goal": "Compare travel options and opening hours",
                "bot_id": research["id"],
            }
        )
        core.db.execute("UPDATE work_cards SET task_id=?,status='running' WHERE id=?", (key, card["id"]))
        progress = {"phase": "tool", "tools": [{"name": "browser", "status": "running"}]}
        detail = core.tasks.detail

        def task_detail(task_id):
            value = detail(task_id)
            if task_id == key:
                value["progress"] = dict(progress)
            return value

        core.tasks.detail = task_detail
        browser_state = {
            "lease": {"kind": "agent", "owner": key},
            "browser_id": detail(key)["browser_id"],
            "headless": True,
            "native_view": True,
            "url": "https://example.com/kyoto",
        }
        browser_frame = {"empty": True}

        async def runtime(method, path, **kwargs):
            return (
                [
                    {
                        "id": "test",
                        "name": "Test",
                        "configured": True,
                        "models": [{"id": "test", "name": "Test model"}],
                    }
                ]
                if path == "/v1/providers"
                else {}
            )

        async def remote(service, path, *args, **kwargs):
            return dict(browser_frame) if path == "/view" else dict(browser_state)

        core.coordinator.request = runtime
        core.remote = remote
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            # Begin fixture execution after startup recovery has finished.
            core.db.execute("UPDATE tasks SET status='running',wait_reason=NULL WHERE id=?", (key,))
            core.db.execute("UPDATE actions SET status='executing',error=NULL WHERE id='browse'")
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                website = context.new_page()
                website.set_content(
                    '<html><body style="font:24px system-ui;background:#f4f7f6;padding:50px;color:#23443a"><small>City guide</small><h1>Weekend in Kyoto</h1><p>Gardens, quiet cafes and a walk by the river.</p><hr><h2>The riverside cafe</h2><p>Open from 8 am. Outdoor seating available.</p></body></html>'
                )
                browser_frame.update(
                    empty=False, screenshot=base64.b64encode(website.screenshot(type="jpeg")).decode()
                )
                website.close()
                page = context.new_page()
                page.emulate_media(reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                expect(page.locator("#new-bot")).to_be_visible()
                assert page.locator(f'[data-task="{old["id"]}"]').count() == 0
                page.locator("#search-workspace").click()
                query = page.locator("#chat-search-query")
                query.fill("riverside cafe")
                expect(page.locator("[data-chat-result]")).to_have_count(1)
                expect(page.locator("#chat-search-results")).to_contain_text("Research partner")
                expect(page.locator("#chat-search-results mark")).to_have_text("riverside cafe")
                page.screenshot(path=str(OUT / "history-search-light.png"))
                page.locator("#chat-search-bot").select_option("default")
                expect(page.locator("#chat-search-results")).to_contain_text("No matching chats")
                page.locator("#chat-search-bot").select_option("")
                expect(page.locator("[data-chat-result]")).to_have_count(1)
                page.locator("[data-chat-result]").click()
                expect(page.locator("#task-title")).to_have_text("Weekend plans")
                expect(page.locator("#header-bot-name")).to_have_text("Research partner")
                expect(page.locator(".message.search-match")).to_contain_text("riverside cafe")
                # Search also supports paging and discards stale requests.
                page.keyboard.press("Control+k")
                expect(page.locator("[data-chat-result]")).to_have_count(30)
                page.locator("#chat-search-more").click()
                expect(page.locator("[data-chat-result]")).to_have_count(60)
                delayed = []
                page.route("**/api/tasks/search?*", lambda route: delayed.append(route))
                query.fill("old request")
                page.wait_for_timeout(300)
                first = delayed.pop()
                query.fill("new request")
                page.wait_for_timeout(300)
                delayed.pop().fulfill(
                    json={
                        "items": [
                            {
                                "id": key,
                                "title": "Newest match",
                                "bot_id": research["id"],
                                "bot_name": "Research partner",
                                "updated": time.time(),
                                "snippet": "Correct result",
                            }
                        ],
                        "has_more": False,
                    }
                )
                first.fulfill(json={"items": [], "has_more": False})
                expect(page.locator("#chat-search-results")).to_contain_text("Newest match")
                page.unroute("**/api/tasks/search?*")
                page.keyboard.press("Escape")
                page.locator(f'[data-task="{key}"]').click()
                expect(page.locator("#chat-inspector")).to_be_hidden()
                # A missing mask renders the icon's background as a solid square.
                expect(page.locator("[data-toggle-inspector] .icon")).to_have_css(
                    "mask-image", re.compile(r"panel-right\.svg")
                )
                assert page.evaluate("""async () => {
                    const mask = getComputedStyle(document.querySelector('[data-toggle-inspector] .icon')).maskImage;
                    const image = new Image();
                    image.src = mask.slice(4, -1).replace(/^['\"]|['\"]$/g, '');
                    await image.decode();
                    return image.naturalWidth > 0;
                }""")
                expect(page.locator(".working")).to_contain_text("Opening a webpage")
                page.locator("#prompt").fill("Keep this draft while I inspect the task.")
                page.get_by_role("button", name="Chat details", exact=True).click()
                expect(page.locator("#chat-inspector")).to_be_visible()
                expect(page.locator("#chat-browser-dock .computer-preview img")).to_be_visible(timeout=10000)
                expect(page.locator("[data-inspector-cards]")).to_contain_text("Weekend trip research")
                expect(page.locator("[data-inspector-log]")).to_contain_text("Reading a file")
                page.locator("[data-inspector-card]").click()
                expect(page.get_by_role("dialog")).to_contain_text("Work card")
                expect(page.get_by_role("dialog").locator('[name="title"]')).to_have_value(
                    "Weekend trip research"
                )
                page.get_by_role("button", name="Close dialog").click()
                for theme, width in (("light", 1440), ("dark", 1440), ("light", 390), ("dark", 320)):
                    page.evaluate("value => window.ShinBotTheme.set(value)", theme)
                    page.set_viewport_size({"width": width, "height": 900})
                    expect(page.locator("#chat-inspector")).to_be_visible()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight"
                    )
                    page.screenshot(path=str(OUT / f"details-{theme}-{width}.png"), animations="disabled")
                    page.keyboard.press("Escape")
                    expect(page.locator("#chat-inspector")).to_be_hidden()
                    expect(page.locator("#prompt")).to_have_value("Keep this draft while I inspect the task.")
                    expect(page.get_by_role("button", name="Chat details", exact=True)).to_be_focused()
                    page.screenshot(path=str(OUT / f"activity-{theme}-{width}.png"), animations="disabled")
                    page.get_by_role("button", name="Chat details", exact=True).click()
                # Status follows the current action; it never treats approval as execution.
                core.db.execute("UPDATE actions SET status='pending' WHERE id='browse'")
                expect(page.locator(".working")).to_contain_text("Waiting for your approval")
                core.db.execute("UPDATE actions SET status='completed' WHERE id='browse'")
                progress.clear()
                progress.update(phase="thinking")
                expect(page.locator(".working")).to_contain_text("Thinking through your request")
                progress.update(text="The cafe is open tomorrow morning.", created=time.time())
                expect(page.locator(".working")).to_contain_text("Writing a reply")
                core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (key,))
                core.db.execute(
                    "UPDATE messages SET status='completed',answer=?,finished=? WHERE task_id=?",
                    ("The cafe is open tomorrow morning.", time.time(), key),
                )
                expect(page.locator(".working")).to_have_count(0)
                expect(page.locator("[data-inspector-current]")).to_have_text("Reply ready")
                page.reload()
                expect(page.locator("#new-bot")).to_be_visible()
                page.get_by_role("button", name="Open navigation").click()
                page.locator(f'[data-task="{key}"]').click()
                expect(page.locator("#chat-inspector")).to_be_hidden()
                assert not errors, errors
                browser.close()
            print(
                "PASS old chat contents, bot filter, paging, stale search responses, exact chat ownership, sidebar default, browser docking, board card, operation history, live phases, drafts, light/dark/mobile and scroll containment"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    main()
