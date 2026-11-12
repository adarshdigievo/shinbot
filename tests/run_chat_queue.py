"""Exercise queued messages and steering with real Pi Durable and a local model fixture."""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results/chat-queue"
OUT.mkdir(parents=True, exist_ok=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, label, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    raise AssertionError("Timed out: " + label)


with tempfile.TemporaryDirectory(prefix="shinbot-chat-queue-") as directory:
    data = Path(directory)
    ports = {name: free_port() for name in ("control", "runtime", "fixture")}
    urls = {name: f"http://localhost:{port}" for name, port in ports.items()}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SHINBOT_", "OPENAI_", "ANTHROPIC_"))}
    env.update(
        SHINBOT_DATA=str(data / "control"),
        SHINBOT_RUNTIME_DATA=str(data / "runtime"),
        SHINBOT_ORIGIN=urls["control"],
        SHINBOT_CONTROL_URL=urls["control"],
        SHINBOT_RUNTIME_URL=urls["runtime"],
        SHINBOT_COMPUTER_URL="http://127.0.0.1:1",
        SHINBOT_BROWSER_URL="http://127.0.0.1:1",
        SHINBOT_SERVICE_TOKEN=secrets.token_urlsafe(32),
        SHINBOT_COMPUTER_TOKEN=secrets.token_urlsafe(32),
        SHINBOT_BROWSER_TOKEN=secrets.token_urlsafe(32),
        SHINBOT_CHANNEL_TOKEN=secrets.token_urlsafe(32),
        SHINBOT_FIXTURE_URL=urls["fixture"] + "/v1",
    )
    (data / "runtime").mkdir()
    processes = {}
    logs = []

    def launch(name):
        child = env.copy()
        if name == "runtime":
            cmd = [
                shutil.which("node") or str(Path.home() / ".local/bin/node"),
                str(ROOT / "runtime/dist/server.js"),
            ]
            child.update(HOST="127.0.0.1", PORT=str(ports[name]))
        else:
            module = "shinbot.app:create_app" if name == "control" else "fixture_model:app"
            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                module,
                "--host",
                "127.0.0.1",
                "--port",
                str(ports[name]),
                "--no-access-log",
            ]
            cmd += ["--factory"] if name == "control" else ["--app-dir", str(ROOT / "tests")]
        log = (OUT / f"{name}.log").open("a")
        logs.append(log)
        processes[name] = subprocess.Popen(cmd, env=child, stdout=log, stderr=log)

    def stop(name):
        process = processes[name]
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    with httpx.Client(base_url=urls["control"], trust_env=False, timeout=60) as client:

        def api(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        def settings(**changes):
            return api("PUT", "/api/settings", json={**api("GET", "/api/settings"), **changes})

        def metrics():
            return client.get(urls["fixture"] + "/fixture-metrics").json()["model_requests"]

        try:
            for name in ("fixture", "runtime", "control"):
                launch(name)
            wait_for(lambda: client.get("/auth/status").is_success, "gateway")
            session = api(
                "POST",
                "/auth/login",
                json={
                    "bootstrap": (data / "control/bootstrap-token").read_text().strip(),
                    "password": secrets.token_urlsafe(24),
                    "cli": True,
                },
            )["token"]
            client.headers["Authorization"] = "Bearer " + session
            defaults = api("GET", "/api/settings")
            assert defaults["task_tokens"] == defaults["daily_tokens"] == 0
            settings(provider="custom-fixture", model="fixture")
            wait_for(lambda: api("GET", "/api/status")["runtime_ready"], "runtime reconciliation")
            task = api("POST", "/api/tasks", json={"content": "fixture-queue:first"})["id"]

            def detail():
                return api("GET", "/api/tasks/" + task)

            wait_for(lambda: detail()["messages"][0]["status"] == "running", "initial run")
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": urls["control"]}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.emulate_media(reduced_motion="reduce")
                page.goto(urls["control"])
                page.locator(f'[data-task="{task}"]').click()
                prompt = page.locator("#prompt")
                expect(page.locator("#steer-draft")).to_be_visible()
                expect(page.get_by_role("button", name="Queue", exact=True)).to_have_text("Queue")
                for label in ("second", "third"):
                    prompt.fill("fixture-queue:" + label)
                    page.get_by_role("button", name="Queue", exact=True).click()
                    expect(prompt).to_have_value("")
                expect(page.locator("#message-queue li")).to_have_count(2)
                expect(page.locator("#messages .message.user")).to_have_count(1)
                prompt.fill("fixture-queue:redirect")
                page.locator("#steer-draft").click()
                expect(prompt).to_have_value("")
                waiting = wait_for(
                    lambda: d if (d := detail())["messages"][-1]["submission_id"] else None,
                    "steer admitted while first model call waits",
                )
                assert len(waiting["messages"]) == 4
                original_ids = [m["id"] for m in waiting["messages"]]
                steer_submission = waiting["messages"][-1]["submission_id"]
                assert waiting["messages"][-1]["mode"] == "steer"
                expect(page.locator("#message-queue")).to_contain_text("Steering at next step")
                for theme, width in (("light", 1440), ("dark", 390), ("light", 320)):
                    page.set_viewport_size({"width": width, "height": 1000})
                    page.evaluate("theme=>window.ShinBotTheme.set(theme)", theme)
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                    assert page.locator(".composer").evaluate("el=>el.scrollWidth<=el.clientWidth")
                    page.screenshot(path=str(OUT / f"queue-{theme}-{width}.png"))
                # Both durable stores reopen with the same input and inbox IDs.
                stop("control")
                stop("runtime")
                launch("runtime")
                launch("control")
                wait_for(lambda: client.get("/auth/status").is_success, "gateway restart")
                wait_for(lambda: api("GET", "/api/status")["runtime_ready"], "runtime recovery")
                assert [m["id"] for m in detail()["messages"]] == original_ids
                assert detail()["messages"][-1]["submission_id"] == steer_submission
                page.reload()
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator(f'[data-task="{task}"]').click()
                expect(page.locator("#message-queue li")).to_have_count(3)
                client.post(urls["fixture"] + "/watch-fixture/redirect/reply").raise_for_status()
                client.post(urls["fixture"] + "/watch-fixture/first/reply").raise_for_status()
                done = wait_for(
                    lambda: d if (d := detail())["messages"][-1]["status"] == "completed" else None,
                    "steered response",
                )
                assert done["messages"][0]["answer"] == "Finished redirect"
                assert done["messages"][-1]["answer"] == "Finished redirect"
                assert done["messages"][0]["answer_id"] == done["messages"][-1]["answer_id"]
                assert len(done["actions"]) == 1 and done["actions"][0]["status"] == "completed"
                wait_for(lambda: detail()["messages"][1]["status"] == "running", "second queued prompt")
                expect(page.locator("#messages .message.assistant")).to_have_count(1)
                expect(page.locator("#messages .message.user")).to_have_count(3)
                expect(page.locator("#message-queue li")).to_have_count(1)
                entries = page.locator("#messages [data-entry]").evaluate_all(
                    "els=>els.map(el=>el.dataset.entry)"
                )
                assert (
                    entries.index("user-" + original_ids[3])
                    < entries.index("answer-" + original_ids[3])
                    < entries.index("user-" + original_ids[1])
                )
                # Turn the remaining queued prompt into a steer without adding another message.
                page.locator("#message-queue [data-steer-message]").click()
                wait_for(lambda: detail()["messages"][2]["submission_id"], "promoted message submitted")
                expect(page.locator("#message-queue")).to_contain_text("Steering at next step")
                assert len(detail()["messages"]) == 4
                client.post(urls["fixture"] + "/watch-fixture/second/reply").raise_for_status()
                wait_for(lambda: detail()["messages"][2]["started"], "promoted message starts")
                client.post(urls["fixture"] + "/watch-fixture/third/reply").raise_for_status()
                finished = wait_for(
                    lambda: d if (d := detail())["status"] == "completed" else None, "all queued work done"
                )
                assert [m["answer"] for m in finished["messages"]] == [
                    "Finished redirect",
                    "Finished second",
                    "Finished third",
                    "Finished redirect",
                ]
                assert len(finished["actions"]) == 1
                expect(page.locator("#message-queue")).to_be_hidden()
                expect(page.locator("#messages .message.assistant")).to_have_count(3)
                expect(page.locator("#steer-draft")).to_be_hidden()
                expect(page.get_by_role("button", name="Send", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Queue", exact=True)).to_have_count(0)
                page.screenshot(path=str(OUT / "ordered-chat.png"))
                assert not errors, errors
                browser.close()
            print(
                "PASS: FIFO display, draft steering, queued-message promotion, shared reply deduplication, restart recovery, one tool execution, mobile themes"
            )
        finally:
            for name in list(processes):
                stop(name)
            for log in logs:
                log.close()
