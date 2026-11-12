"""Real runtime, Computer, Camoufox and chat delivery with a deterministic model."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results/attachments"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait(check, timeout=60):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            if result := check():
                return result
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("The attachment flow did not finish in time.")


def checked(response):
    response.raise_for_status()
    return response.json()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-files-") as temporary:
        root = Path(temporary)
        ports = {name: free_port() for name in ("control", "runtime", "computer", "browser", "fixture")}
        urls = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}
        tokens = {name: secrets.token_urlsafe(32) for name in ("runtime", "computer", "browser", "channel")}
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SHINBOT_", "OPENAI_", "ANTHROPIC_", "SERPAPI_"))
        }
        env.update(
            SHINBOT_DATA=str(root / "control"),
            SHINBOT_RUNTIME_DATA=str(root / "runtime"),
            SHINBOT_EXECUTOR_DATA=str(root / "computer"),
            SHINBOT_BROWSER_DATA=str(root / "browser"),
            SHINBOT_WORKSPACE=str(root / "workspace"),
            SHINBOT_DEV_EXECUTION="1",
            SHINBOT_BROWSER_VIEW="native",
            SHINBOT_CHILD_PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
            SHINBOT_ORIGIN=urls["control"],
            SHINBOT_CONTROL_URL=urls["control"],
            SHINBOT_RUNTIME_URL=urls["runtime"],
            SHINBOT_COMPUTER_URL=urls["computer"],
            SHINBOT_BROWSER_URL=urls["browser"],
            SHINBOT_FIXTURE_URL=urls["fixture"] + "/v1",
            SHINBOT_FIXTURE_ORIGIN=urls["fixture"],
            SHINBOT_SERVICE_TOKEN=tokens["runtime"],
            SHINBOT_COMPUTER_TOKEN=tokens["computer"],
            SHINBOT_BROWSER_TOKEN=tokens["browser"],
            SHINBOT_CHANNEL_TOKEN=tokens["channel"],
        )
        processes, logs = {}, []

        def start(name):
            child = dict(env)
            log = (OUT / f"{name}.log").open("a")
            logs.append(log)
            if name == "runtime":
                child.update(PORT=str(ports[name]), HOST="127.0.0.1")
                command = ["node", str(ROOT / "runtime/dist/server.js")]
            else:
                app = {"control": "shinbot.app:create_app", "fixture": "fixture_model:app"}.get(
                    name, f"shinbot.services.{name}:app"
                )
                command = [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    app,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(ports[name]),
                    "--no-access-log",
                ]
                if name == "control":
                    command.append("--factory")
                if name == "fixture":
                    command.extend(["--app-dir", str(ROOT / "tests")])
                if name in {"computer", "browser"}:
                    child["SHINBOT_EXECUTOR_TOKEN"] = tokens[name]
            processes[name] = subprocess.Popen(command, cwd=ROOT, env=child, stdout=log, stderr=log)

        def stop(name):
            process = processes[name]
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        try:
            for name in ports:
                start(name)
            with httpx.Client(base_url=urls["control"], trust_env=False, timeout=30) as client:
                wait(lambda: client.get("/health").is_success)
                password = secrets.token_urlsafe(24)
                login = checked(
                    client.post(
                        "/auth/login",
                        json={
                            "bootstrap": (root / "control/bootstrap-token").read_text().strip(),
                            "password": password,
                            "cli": True,
                        },
                    )
                )
                client.headers["Authorization"] = "Bearer " + login["token"]
                wait(lambda: checked(client.get("/api/status"))["runtime_ready"])
                preferences = checked(client.get("/api/settings"))
                assert preferences["browser_backend"] == "camoufox"
                checked(
                    client.put(
                        "/api/settings",
                        json={
                            **preferences,
                            "provider": "custom-fixture",
                            "model": "fixture",
                            "timezone": "Asia/Kolkata",
                        },
                    )
                )
                task = checked(client.post("/api/tasks", json={"content": "fixture-chat-files"}))

                def complete():
                    detail = checked(client.get("/api/tasks/" + task["id"]))
                    for action in detail["actions"]:
                        if action["status"] == "pending":
                            assert action["tool"] == "write_file", action
                            checked(
                                client.post(
                                    f"/api/actions/{action['id']}/decision",
                                    json={"approved": True, "scope": "once"},
                                )
                            )
                    if detail["status"] in {"failed", "uncertain"}:
                        raise AssertionError(detail["error"] or detail["actions"])
                    return detail if detail["status"] == "completed" else None

                detail = wait(complete)
                assert len(detail["attachments"]) == 2, detail
                csv, screenshot = detail["attachments"]
                original = b"day,task\nTuesday,Project check-in\n"
                assert client.get(csv["url"]).content == original
                assert client.get(screenshot["url"]).content.startswith(b"\x89PNG\r\n\x1a\n")
                with httpx.Client(
                    base_url=urls["browser"],
                    headers={"Authorization": "Bearer " + tokens["browser"]},
                    trust_env=False,
                ) as browser_client:
                    wait(lambda: checked(browser_client.get("/health"))["browser_running"] is False)
                    assert checked(browser_client.get("/state"))["backend"] == "camoufox"
                (root / "workspace/reports/week.csv").write_text("Original changed after sharing")
                for name in ("control", "runtime", "computer", "browser"):
                    stop(name)
                for name in ("runtime", "computer", "browser", "control"):
                    start(name)
                wait(lambda: checked(client.get("/api/status"))["runtime_ready"])
                restored = checked(client.get("/api/tasks/" + task["id"]))
                assert restored["attachments"] == detail["attachments"]
                assert client.get(csv["url"]).content == original
                for action in restored["actions"]:
                    if (
                        action["tool"] == "attach_file"
                        or json.loads(action["args"]).get("operation") == "screenshot"
                    ):
                        payload = {k: action[k] for k in ("id", "task_id", "epoch", "tool")}
                        payload["args"] = json.loads(action["args"])
                        replay = checked(
                            client.post(
                                "/internal/actions",
                                headers={"Authorization": "Bearer " + tokens["runtime"]},
                                json=payload,
                            )
                        )
                        assert replay["status"] == "completed"
                assert len(checked(client.get("/api/tasks/" + task["id"]))["attachments"]) == 2
                assert httpx.get(urls["control"] + csv["url"], trust_env=False).status_code == 401
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000}, color_scheme="dark"
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(urls["control"])
                    page.get_by_label("Password", exact=True).fill(password)
                    page.get_by_role("button", name="Sign in", exact=True).click()
                    page.locator(f'[data-task="{task["id"]}"]').click()
                    expect(page.locator(".chat-attachment")).to_have_count(2)
                    image = page.get_by_alt_text("appointments.png", exact=True)
                    expect(image).to_be_visible()
                    page.wait_for_function(
                        "document.querySelector('.attachment-preview img')?.naturalWidth === 1280"
                    )
                    page.locator(".chat-attachment").last.scroll_into_view_if_needed()
                    page.screenshot(path=str(OUT / "chat-files-dark.png"), animations="disabled")
                    with page.expect_download() as download:
                        page.locator(".chat-attachment").first.get_by_role(
                            "link", name="Download", exact=True
                        ).click()
                    assert download.value.suggested_filename == "weekly-plan.csv"
                    assert Path(download.value.path()).read_bytes() == original
                    page.reload()
                    page.locator(f'[data-task="{task["id"]}"]').click()
                    expect(page.locator(".chat-attachment")).to_have_count(2)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.emulate_media(color_scheme="light")
                    page.locator(".chat-attachment").last.scroll_into_view_if_needed()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(OUT / "chat-files-mobile.png"), animations="disabled")
                    assert not errors, errors
                    browser.close()
                print(
                    "PASS: Camoufox default, runtime file/screenshot tools, authenticated chat delivery, download, auto-close, service restarts, immutable replay, desktop/mobile UI"
                )
        finally:
            for name in reversed(list(processes)):
                if processes[name].poll() is None:
                    stop(name)
            for log in logs:
                log.close()


if __name__ == "__main__":
    main()
