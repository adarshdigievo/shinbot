"""Check prompt drafting through Pi's real provider API and the browser UI.

The local model fixture replaces paid providers. Personal data is never used.
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from shinbot.app import create_app
from shinbot.auth import hash_password
from shinbot.config import Config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results/prompt-suggestions"
OUT.mkdir(parents=True, exist_ok=True)
SUGGESTION = "Compare the remaining options and recommend the best next step."


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


with tempfile.TemporaryDirectory(prefix="shinbot-prompts-") as temporary:
    data = Path(temporary)
    ports = {name: free_port() for name in ("control", "runtime", "fixture")}
    urls = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}
    token = secrets.token_urlsafe(32)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SHINBOT_", "OPENAI_", "ANTHROPIC_"))}
    env.update(
        SHINBOT_RUNTIME_DATA=str(data / "runtime"),
        SHINBOT_SERVICE_TOKEN=token,
        SHINBOT_CONTROL_URL=urls["control"],
        SHINBOT_FIXTURE_URL=urls["fixture"] + "/v1",
        HOST="127.0.0.1",
        PORT=str(ports["runtime"]),
    )
    app = create_app(
        Config(
            data=data / "control",
            origin=urls["control"],
            runtime_url=urls["runtime"],
            computer_url="http://127.0.0.1:1",
            browser_url="http://127.0.0.1:1",
            service_token=token,
            coordinate=False,
        )
    )
    core = app.state.core
    core.db.set("password", hash_password("isolated-prompt-test-password"))
    core.db.set(
        "preferences", {**core.db.setting("preferences"), "provider": "custom-fixture", "model": "fixture"}
    )
    session = core.auth.session()
    task = core.tasks.create("Compare a train and bus for a weekend trip.")["id"]
    core.db.execute("UPDATE tasks SET status='completed',title='Weekend trip' WHERE id=?", (task,))
    core.db.execute(
        "UPDATE messages SET status='completed',answer=?,finished=? WHERE task_id=?",
        (
            "The train is faster; the bus costs less. I can compare the remaining options and recommend a route.",
            time.time(),
            task,
        ),
    )
    core.coordinator.ready = True
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=ports["control"], log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    processes, logs = [], []
    try:
        for name, command in (
            (
                "fixture",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "fixture_model:app",
                    "--app-dir",
                    str(ROOT / "tests"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(ports["fixture"]),
                    "--no-access-log",
                ],
            ),
            (
                "runtime",
                [
                    shutil.which("node") or str(Path.home() / ".local/bin/node"),
                    str(ROOT / "runtime/dist/server.js"),
                ],
            ),
        ):
            log = (OUT / f"{name}.log").open("w")
            logs.append(log)
            processes.append(subprocess.Popen(command, env=env, stdout=log, stderr=log))
        thread.start()
        with httpx.Client(trust_env=False, timeout=20) as client:
            for _ in range(100):
                try:
                    if (
                        client.get(urls["control"] + "/health").is_success
                        and client.get(
                            urls["runtime"] + "/v1/health", headers={"Authorization": "Bearer " + token}
                        ).is_success
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Local services did not start")
            response = client.post(
                urls["runtime"] + "/v1/reconcile",
                headers={"Authorization": "Bearer " + token},
                json=core.runtime_bootstrap(),
            )
            response.raise_for_status()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_cookies([{"name": "shinbot_session", "value": session, "url": urls["control"]}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.emulate_media(reduced_motion="reduce")
            page.goto(urls["control"])
            page.locator("#new-task").click()
            prompt = page.locator("#prompt")
            suggestion = page.locator("#prompt-suggestion")
            send = page.get_by_role("button", name="Send", exact=True)
            expect(prompt).to_have_attribute("placeholder", SUGGESTION, timeout=15000)
            expect(send).to_be_disabled()
            assert core.db.one("SELECT COUNT(*) AS n FROM usage_ledger")["n"] == 1
            prompt.focus()
            prompt.press("Shift+Tab")
            expect(prompt).to_have_value("")
            prompt.focus()
            prompt.press("Tab")
            expect(prompt).to_have_value(SUGGESTION)
            expect(prompt).to_be_focused()
            expect(send).to_be_enabled()
            expect(suggestion).to_be_hidden()
            assert core.db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == 1
            prompt.fill("My own draft")
            prompt.press("Tab")
            expect(prompt).to_have_value("My own draft")
            expect(prompt).not_to_be_focused()
            prompt.fill("")
            prompt.press("Escape")
            expect(suggestion).to_be_hidden()
            prompt.press("Tab")
            expect(prompt).not_to_be_focused()
            page.locator("#new-task").click()
            expect(prompt).to_have_attribute("placeholder", SUGGESTION)
            # Suggestions are a placeholder and hint, not another button.
            expect(suggestion).to_have_text("Tab to complete")
            assert suggestion.evaluate("el => el.tagName") == "SPAN"
            prompt.click()
            expect(prompt).to_have_value("")
            prompt.press("Tab")
            expect(prompt).to_have_value(SUGGESTION)
            expect(prompt).to_be_focused()
            prompt.fill("")
            for theme, width in (("light", 1440), ("dark", 390), ("light", 320)):
                page.evaluate("theme => window.ShinBotTheme.set(theme)", theme)
                page.set_viewport_size({"width": width, "height": 900})
                expect(suggestion).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator(".send-hint,#composer-shortcuts").count() == 0
                page.screenshot(path=str(OUT / f"starter-{theme}-{width}.png"))
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator(f'[data-task="{task}"]').click()
            expect(prompt).to_have_attribute("placeholder", SUGGESTION, timeout=15000)
            assert core.db.one("SELECT COUNT(*) AS n FROM usage_ledger")["n"] == 2
            page.evaluate("window.ShinBotTheme.set('dark')")
            page.screenshot(path=str(OUT / "follow-up-dark.png"))
            prompt.press("Tab")
            expect(prompt).to_have_value(SUGGESTION)
            assert core.db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 1
            prompt.fill("")
            # A slow suggestion arriving after typing must not replace the draft.
            pending = []
            page.route("**/api/prompt-suggestion", lambda route: pending.append(route))
            page.locator("#new-task").click()
            page.wait_for_timeout(550)
            assert pending
            prompt.fill("Keep these words")
            pending.pop().fulfill(json={"suggestion": "A late suggestion"})
            expect(prompt).to_have_value("Keep these words")
            expect(suggestion).to_be_hidden()
            # Switching conversations invalidates an outstanding suggestion.
            prompt.fill("")
            page.locator("#new-task").click()
            page.wait_for_timeout(550)
            stale = pending.pop()
            page.locator(f'[data-task="{task}"]').click()
            page.wait_for_timeout(550)
            pending.pop().fulfill(json={"suggestion": "Compare the train fares."})
            stale.fulfill(json={"suggestion": "Wrong conversation"})
            expect(prompt).to_have_attribute("placeholder", "Compare the train fares.")
            page.unroute("**/api/prompt-suggestion")
            for status, message in (
                ("running", "Preparing the next step"),
                ("waiting", "Waiting for your approval"),
                ("failed", "The task could not finish."),
            ):
                core.db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
                expect(page.locator("#chat-status")).to_have_text(message, timeout=5000)
                expect(suggestion).to_be_hidden(timeout=5000)
            core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task,))
            expect(suggestion).to_be_visible(timeout=5000)
            touch = browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            touch.add_cookies([{"name": "shinbot_session", "value": session, "url": urls["control"]}])
            mobile = touch.new_page()
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.emulate_media(reduced_motion="reduce")
            mobile.goto(urls["control"])
            mobile.get_by_role("button", name="Open chat", exact=True).first.click()
            expect(mobile.locator("#prompt-suggestion")).to_be_visible()
            expect(mobile.locator("#prompt-suggestion")).to_have_text("Tab to complete")
            mobile.locator("#prompt").tap()
            expect(mobile.locator("#prompt")).to_be_empty()
            mobile.locator("#prompt").press("Tab")
            expect(mobile.locator("#prompt")).not_to_be_empty()
            expect(mobile.get_by_role("button", name="Send", exact=True)).to_be_enabled()
            assert mobile.locator("#prompt").evaluate(
                "el => el.scrollHeight <= el.clientHeight && el.scrollTop === 0"
            )
            mobile.screenshot(path=str(OUT / "mobile-draft.png"))
            assert not errors, errors
            browser.close()
        print(
            "PASS: real provider suggestions, cached usage, Tab-only completion, draft safety, stale replies, inactive states, light/dark mobile layouts"
        )
    finally:
        server.should_exit = True
        if thread.is_alive():
            thread.join(timeout=10)
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
