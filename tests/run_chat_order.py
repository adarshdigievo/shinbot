"""Check chat chronology through the gateway, polling, approval UI, and reload."""

import asyncio
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

OUT = Path(__file__).resolve().parents[1] / "test-results/chat-order"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-chat-order-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-chat-test-password"))
        core.db.set(
            "preferences",
            {
                **core.db.setting("preferences"),
                "provider": "test",
                "model": "test",
                "timezone": "Asia/Kolkata",
            },
        )
        session = core.auth.session()
        task = core.tasks.create("Check the repository")
        key = task["id"]
        first = core.tasks.detail(key)["messages"][0]["id"]
        second = core.tasks.message(key, "Check the website too", "follow-up")["id"]
        stamp = time.time() - 60
        core.db.execute(
            "UPDATE messages SET created=?,finished=?,status='completed',answer='The repository checks passed.' WHERE id=?",
            (stamp, stamp + 3, first),
        )
        core.db.execute("UPDATE messages SET created=?,status='running' WHERE id=?", (stamp + 4, second))
        core.db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (key,))

        def activity(action_id, when, status, tool="browser"):
            core.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    key,
                    core.db.setting("epoch"),
                    tool,
                    '{"operation":"inspect"}',
                    "fixture",
                    status,
                    '{"text":"The check passed."}' if status == "completed" else None,
                    None,
                    when,
                    time.time() + 600,
                ),
            )

        activity("repository-check", stamp + 1, "completed", "inspect_repository")
        activity("website-check", stamp + 5, "pending")
        progress = {}
        original_detail = core.tasks.detail

        def detail(task_id):
            result = original_detail(task_id)
            if task_id == key:
                result["progress"] = dict(progress)
            return result

        core.tasks.detail = detail
        refresh = {"delay_next": False}
        captured, release, delivered = threading.Event(), threading.Event(), threading.Event()

        @app.middleware("http")
        async def delay_old_snapshot(request, call_next):
            delay = request.url.path == f"/api/tasks/{key}" and refresh["delay_next"]
            if delay:
                refresh["delay_next"] = False
            response = await call_next(request)
            if delay:
                captured.set()
                while not release.is_set():
                    await asyncio.sleep(0.02)
                delivered.set()
            return response

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

        core.coordinator.request = runtime
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
                page.emulate_media(color_scheme="dark")
                page.goto(origin)
                page.locator(f'[data-task="{key}"]').click()
                expected = [
                    f"user-{first}",
                    "action-repository-check",
                    f"answer-{first}",
                    f"user-{second}",
                    "action-website-check",
                    "status",
                ]

                def ordered(keys):
                    expect(page.locator("#messages > [data-entry]")).to_have_count(len(keys))
                    assert (
                        page.locator("#messages > [data-entry]").evaluate_all(
                            "nodes => nodes.map(n=>n.dataset.entry)"
                        )
                        == keys
                    )

                expect(page.get_by_role("button", name="Decline", exact=True)).to_be_visible()
                ordered(expected)
                for entry, instant in [
                    (f"user-{first}", stamp),
                    (f"answer-{first}", stamp + 3),
                    (f"user-{second}", stamp + 4),
                ]:
                    clock = page.locator(f'[data-entry="{entry}"] time')
                    expect(clock).to_be_visible()
                    assert clock.get_attribute("datetime") == page.evaluate(
                        "stamp=>new Date(stamp*1000).toISOString()", instant
                    )
                    assert clock.inner_text() == page.evaluate(
                        "stamp=>new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Kolkata'}).format(new Date(stamp*1000))",
                        instant,
                    )
                page.locator("#conversation-scroll").evaluate("el=>el.scrollTop=0")
                page.screenshot(path=str(OUT / "message-times-dark.png"), animations="disabled")
                page.emulate_media(color_scheme="light")
                page.set_viewport_size({"width": 390, "height": 844})
                expect(page.locator(f'[data-entry="user-{first}"] time')).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.locator("#conversation-scroll").evaluate("el=>el.scrollTop=0")
                page.screenshot(path=str(OUT / "message-times-mobile.png"), animations="disabled")
                page.set_viewport_size({"width": 1440, "height": 1000})
                trace = page.locator('[data-entry="action-repository-check"]')
                trace.locator("summary").click()
                page.get_by_role("button", name="Decline", exact=True).click()
                expect(page.locator('[data-entry="action-website-check"]')).to_contain_text("denied")
                ordered(expected)
                expect(trace).to_have_attribute("open", "")
                progress.update(text="The website needs one more check.", created=stamp + 6)
                expect(page.locator('[data-entry="progress"]')).to_be_visible(timeout=15000)
                ordered(expected[:-1] + ["progress", "status"])
                activity("follow-up-check", stamp + 7, "pending")
                expect(page.locator('[data-entry="action-follow-up-check"]')).to_be_visible(timeout=15000)
                ordered(expected[:-1] + ["progress", "action-follow-up-check", "status"])
                expect(trace).to_have_attribute("open", "")
                core.db.execute(
                    "UPDATE actions SET status='completed',result='{}' WHERE id='follow-up-check'"
                )
                progress.update(text="The website checks passed.")
                expect(page.locator('[data-entry="progress"]')).to_contain_text(
                    "The website checks passed.", timeout=15000
                )
                page.evaluate("""() => {
                  window.replyDisappearances=0;
                  window.replyObserver=new MutationObserver(() => {
                    if(![...document.querySelectorAll('.message.assistant')].some(node=>node.textContent.includes('The website checks passed.')))window.replyDisappearances++;
                  });
                  window.replyObserver.observe(document.querySelector('#messages'),{childList:true,subtree:true,characterData:true});
                }""")
                progress.clear()
                # Two polls with no live text reproduce the runtime-to-database handoff gap.
                for _ in range(2):
                    with page.expect_response(lambda response: response.url == f"{origin}/api/tasks/{key}"):
                        page.wait_for_timeout(1600)
                expect(page.locator('[data-entry="progress"]')).to_contain_text("The website checks passed.")
                assert page.evaluate("window.replyDisappearances") == 0
                refresh["delay_next"] = True
                deadline = time.monotonic() + 10
                while not captured.is_set() and time.monotonic() < deadline:
                    page.wait_for_timeout(50)
                assert captured.is_set(), "Expected an older refresh to be held in flight"
                core.db.execute(
                    "UPDATE messages SET status='completed',answer='The website checks passed.',finished=? WHERE id=?",
                    (stamp + 8, second),
                )
                core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (key,))
                expect(page.locator(f'[data-entry="answer-{second}"]')).to_be_visible(timeout=15000)
                final = expected[:-1] + ["action-follow-up-check", f"answer-{second}", "status"]
                ordered(final)
                expect(trace).to_have_attribute("open", "")
                release.set()
                deadline = time.monotonic() + 5
                while not delivered.is_set() and time.monotonic() < deadline:
                    page.wait_for_timeout(50)
                assert delivered.is_set()
                page.wait_for_timeout(350)
                assert page.evaluate("window.replyDisappearances") == 0
                expect(page.locator(f'[data-entry="answer-{second}"]')).to_contain_text(
                    "The website checks passed."
                )
                expect(page.locator('[data-entry="progress"]')).to_have_count(0)
                ordered(final)
                page.evaluate("window.replyObserver.disconnect()")
                page.reload()
                page.locator(f'[data-task="{key}"]').click()
                expect(page.locator(f'[data-entry="answer-{second}"]')).to_be_visible()
                ordered(final)
                page.screenshot(path=str(OUT / "chronological-light.png"), full_page=True)
                page.evaluate("window.ShinBotTheme.set('dark')")
                page.evaluate(
                    "Promise.all(document.getAnimations().filter(a=>a.effect.getComputedTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})))"
                )
                ordered(final)
                page.screenshot(path=str(OUT / "chronological-dark.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                ordered(final)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors, errors
                browser.close()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "verified": [
                            "two-turn chronology",
                            "approval decision",
                            "expanded trace survives polling",
                            "streaming position",
                            "completion without duplicates",
                            "streamed reply stays visible until saved",
                            "older delayed refresh cannot remove the completed reply",
                            "reload",
                            "light/dark/mobile",
                        ],
                    }
                )
            )
        finally:
            release.set()
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
