"""Check that long chats and expanded tool output stay inside the conversation pane."""

import json
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

OUT = Path(__file__).resolve().parents[1] / "test-results/chat-scroll"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-scroll-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-scroll-test-password"))
        core.db.set("preferences", {**core.db.setting("preferences"), "provider": "test", "model": "test"})
        session = core.auth.session()
        key = core.tasks.create("Review the repository and explain the results")["id"]
        core.db.execute("UPDATE tasks SET status='running' WHERE id=?", (key,))
        core.db.execute("UPDATE messages SET status='running' WHERE task_id=?", (key,))
        stamp = time.time()
        for index in range(35):
            core.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"check-{index}",
                    key,
                    core.db.setting("epoch"),
                    "run_command",
                    "{}",
                    "fixture",
                    "completed",
                    json.dumps({"exit_code": 0, "output": "Repository check passed. " * 500}),
                    None,
                    stamp + index,
                    stamp + 600,
                ),
            )

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
            return {"held": False}

        async def remote(*args, **kwargs):
            return {"ok": True, "available": True, "lease": None, "tabs": []}

        core.coordinator.request = runtime
        core.remote = remote
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                page = context.new_page()
                page.emulate_media(reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                page.locator(f'[data-task="{key}"]').click()
                expect(page.locator(".tool-item")).to_have_count(35)
                scroller = page.locator("#conversation-scroll")

                def bounds():
                    return page.evaluate("""() => {
                      const pane=document.querySelector('#conversation-scroll');
                      const rect=s=>document.querySelector(s).getBoundingClientRect();
                      return {height:innerHeight, document:document.documentElement.scrollHeight,
                        body:document.body.scrollHeight, y:scrollY, header:rect('.topbar').top,
                        layout:rect('.layout').bottom, composer:rect('.composer-dock').bottom,
                        paneHeight:pane.clientHeight, paneScroll:pane.scrollHeight,
                        overflowX:document.documentElement.scrollWidth>innerWidth};
                    }""")

                def contained():
                    measured = bounds()
                    assert measured["document"] <= measured["height"] + 1, measured
                    # The main sheet is inset from the window edge, so the pinned header starts a few pixels down.
                    assert measured["y"] == 0 and 0 <= measured["header"] <= 12, measured
                    assert abs(measured["layout"] - measured["height"]) <= 1, measured
                    assert measured["composer"] <= measured["height"] + 1, measured
                    assert measured["paneScroll"] > measured["paneHeight"], measured
                    assert not measured["overflowX"], measured

                last = page.locator('[data-entry="action-check-34"]')
                last.locator("summary").click()
                expect(last).to_have_attribute("open", "")
                print(json.dumps({"expanded_chat": bounds()}), flush=True)
                page.screenshot(path=str(OUT / "expanded.png"))
                contained()
                for theme, width, height in (
                    ("light", 1440, 900),
                    ("dark", 2048, 1024),
                    ("dark", 390, 844),
                    ("light", 320, 568),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("theme => window.ShinBotTheme.set(theme)", theme)
                    scroller.evaluate("node => {node.scrollTop=node.scrollHeight}")
                    contained()
                    scroller.hover(position={"x": 8, "y": 8})
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(200)
                    contained()
                    page.screenshot(path=str(OUT / f"chat-{theme}-{width}.png"))
                page.set_viewport_size({"width": 1440, "height": 900})
                scroller.evaluate("node => {node.scrollTop=0}")
                top = scroller.evaluate("node => node.scrollTop")
                core.db.execute("UPDATE actions SET tool='read_file' WHERE id='check-0'")
                expect(page.locator('[data-entry="action-check-0"] summary')).to_contain_text(
                    "Reading a file", timeout=15000
                )
                assert scroller.evaluate("node => node.scrollTop") == top
                contained()
                scroller.evaluate("node => {node.scrollTop=node.scrollHeight}")
                core.db.execute(
                    "UPDATE messages SET status='completed',answer=?,finished=? WHERE task_id=?",
                    ("The checks passed.\n\n" * 30, time.time() + 60, key),
                )
                core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (key,))
                expect(page.locator(".message.assistant")).to_be_visible(timeout=15000)
                assert scroller.evaluate("node => node.scrollHeight-node.scrollTop-node.clientHeight") < 2
                contained()
                page.reload()
                page.locator(f'[data-task="{key}"]').click()
                expect(page.locator(".message.assistant")).to_be_visible()
                contained()
                page.locator('[data-view="settings"]').click()
                expect(page.locator("#workspace-panel")).to_be_visible()
                assert page.evaluate("scrollY===0 && document.documentElement.scrollHeight<=innerHeight")
                assert not errors, errors
                browser.close()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "verified": [
                            "expanded tool output",
                            "scroll containment",
                            "wheel at bottom",
                            "polling preserves reading position",
                            "new reply follows bottom",
                            "reload",
                            "settings navigation",
                            "desktop/mobile/light/dark",
                        ],
                    }
                )
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
