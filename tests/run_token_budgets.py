"""Exercise token caps through HTTP, the web UI, and an actual Pi Durable runtime.

Uses the deterministic model fixture for exact usage counts. No paid model or
personal installation is used. Run after building runtime/dist/server.js.
"""

import json
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
OUT = ROOT / "test-results/token-budgets"
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


with tempfile.TemporaryDirectory(prefix="shinbot-budgets-") as directory:
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

        def card(key):
            return next(c for c in api("GET", "/api/board") if c["id"] == key)

        def settled(key):
            return wait_for(
                lambda: row if (row := card(key))["status"] in {"blocked", "review"} else None,
                "card completion",
            )

        def move(row):
            return api(
                "POST", f"/api/board/{row['id']}/move", json={"status": "ready", "revision": row["revision"]}
            )

        def new_card(label, **limits):
            row = api(
                "POST",
                "/api/board",
                json={"title": label, "goal": "fixture-budget: verify both steps", **limits},
            )
            move(row)
            return settled(row["id"])

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
            api(
                "POST",
                "/api/plugins/stage",
                json={"marketplace": "official", "id": "kanban", "version": "1.1.0"},
            )
            api("POST", "/api/plugins/kanban/1.1.0/activate")
            report = {}

            before = metrics()
            limited = new_card("Small card cap", max_tokens=1000)
            task = api("GET", "/api/tasks/" + limited["task_id"])
            assert limited["status"] == "blocked" and limited["runs"] == 1 and limited["tokens"] == 1200, (
                limited
            )
            assert task["status"] == "failed" and "token budget" in task["error"], task
            assert "board card reached its total token budget" in task["error"]
            assert metrics() - before == 2
            report["card_cap"] = {
                "cap": 1000,
                "used": limited["tokens"],
                "model_calls": 2,
                "third_call_blocked": True,
            }
            print("PASS: 1,000-token card stops before the third model call", flush=True)

            # Restart both services with the same databases: completed work must not replay.
            stop("control")
            stop("runtime")
            launch("runtime")
            launch("control")
            wait_for(lambda: client.get("/auth/status").is_success, "restarted gateway")
            wait_for(lambda: api("GET", "/api/status")["runtime_ready"], "restarted runtime")
            assert card(limited["id"])["tokens"] == 1200 and metrics() - before == 2
            rejected = client.post(
                f"/api/board/{limited['id']}/move",
                json={"status": "ready", "revision": card(limited["id"])["revision"]},
            )
            assert rejected.status_code == 400 and "budget" in rejected.text
            report["restart"] = {"budget_preserved": True, "no_replayed_calls": True}

            # Clear the exhausted cap through the actual card UI, preserving its history.
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": urls["control"]}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(urls["control"])
                page.locator('[data-view="board"]').click()
                page.get_by_role("button", name="Small card cap", exact=True).click()
                budget = page.get_by_label("Total token budget (optional)", exact=True)
                expect(budget).to_have_value("1000")
                budget.fill("")
                page.get_by_role("button", name="Save card", exact=True).click()
                expect(page.get_by_role("dialog")).to_have_count(0)
                cleared = card(limited["id"])
                assert cleared["max_tokens"] == 0 and cleared["tokens"] == 1200
                before = metrics()
                move(cleared)
                cleared = settled(cleared["id"])
                assert cleared["status"] == "review" and cleared["tokens"] == 3000 and cleared["runs"] == 2
                assert metrics() - before == 3
                report["clear_cap"] = {"used_after_resume": 3000, "runs": 2, "history_preserved": True}

                page.locator("#refresh-board").click()
                page.get_by_role("button", name="New card", exact=True).click()
                expect(budget).to_have_value("")
                expect(budget).to_have_attribute("placeholder", "No limit")
                page.get_by_label("Title", exact=True).fill("Unlimited by default")
                page.get_by_label("Goal and acceptance checks").fill("fixture-budget: verify both steps")
                for theme, width in (("light", 1440), ("dark", 390)):
                    page.set_viewport_size({"width": width, "height": 1000})
                    page.evaluate("theme => window.ShinBotTheme.set(theme)", theme)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(OUT / f"card-budget-{theme}.png"), full_page=True)
                page.get_by_role("button", name="Save card", exact=True).click()
                expect(page.get_by_role("dialog")).to_have_count(0)
                unlimited = next(c for c in api("GET", "/api/board") if c["title"] == "Unlimited by default")
                assert unlimited["max_tokens"] == 0
                before = metrics()
                move(unlimited)
                unlimited = settled(unlimited["id"])
                assert (
                    unlimited["status"] == "review" and unlimited["runs"] == 2 and unlimited["tokens"] == 3600
                )
                assert metrics() - before == 6
                report["unlimited_default"] = {"used": 3600, "runs": 2, "model_calls": 6}
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator('[data-view="settings"]').click()
                task_cap = page.get_by_label("Token cap per message (optional)", exact=True)
                daily_cap = page.get_by_label("Token cap per 24 hours (optional)", exact=True)
                expect(task_cap).to_have_value("")
                expect(daily_cap).to_have_value("")
                task_cap.fill("1000")
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.locator("#toast")).to_have_text("Settings saved.")
                page.reload()
                page.locator('[data-view="settings"]').click()
                expect(task_cap).to_have_value("1000")
                expect(daily_cap).to_have_value("")
                assert not errors, errors
                browser.close()
            print("PASS: removing a cap resumes work; new UI cards are unlimited", flush=True)

            before = metrics()
            assert api("GET", "/api/settings")["task_tokens"] == 1000
            workspace = new_card("Workspace cap on unlimited card")
            assert (
                workspace["max_tokens"] == 0
                and workspace["status"] == "blocked"
                and workspace["tokens"] == 1200
            )
            assert metrics() - before == 2
            report["workspace_cap"] = {"cap": 1000, "used": 1200, "card_cap": 0, "third_call_blocked": True}
            workspace_error = api("GET", "/api/tasks/" + workspace["task_id"])["error"]
            assert "saved token budget" in workspace_error and "Settings → Workspace" in workspace_error
            settings(task_tokens=0, daily_tokens=1000)
            before = metrics()
            daily = new_card("Daily cap on unlimited card")
            assert daily["status"] == "blocked" and daily["tokens"] == 0 and metrics() == before
            assert "past 24 hours" in api("GET", "/api/tasks/" + daily["task_id"])["error"]
            report["daily_cap"] = {"cap": 1000, "existing_usage_blocks_new_work": True, "model_calls": 0}
            settings(daily_tokens=0)
            before = metrics()
            chat = api("POST", "/api/tasks", json={"content": "fixture-budget: ordinary chat"})
            task = wait_for(
                lambda: (
                    t
                    if (t := api("GET", "/api/tasks/" + chat["id"]))["status"] in {"completed", "failed"}
                    else None
                ),
                "unlimited chat",
            )
            assert task["status"] == "completed" and task["tokens"] == 1800 and task["max_tokens"] == 0
            assert metrics() - before == 3
            report["unlimited_chat"] = {"used": 1800, "model_calls": 3}

            # A newly enabled workspace cap and the daily cap also apply to ordinary chats.
            settings(task_tokens=1000)
            before = metrics()
            api(
                "POST",
                f"/api/tasks/{chat['id']}/messages",
                json={"content": "fixture-budget: continue", "request_id": "limited-follow-up"},
            )
            ordinary = wait_for(
                lambda: t if (t := api("GET", "/api/tasks/" + chat["id"]))["status"] == "failed" else None,
                "ordinary chat message cap",
            )
            assert "workspace token budget per message" in ordinary["error"]
            assert "card" not in ordinary["error"].lower() and metrics() - before == 2
            settings(task_tokens=0, daily_tokens=1000)
            before = metrics()
            api(
                "POST",
                f"/api/tasks/{chat['id']}/messages",
                json={"content": "fixture-budget: continue", "request_id": "daily-follow-up"},
            )
            ordinary = wait_for(
                lambda: t if (t := api("GET", "/api/tasks/" + chat["id"]))["status"] == "failed" else None,
                "ordinary chat daily cap",
            )
            assert "past 24 hours" in ordinary["error"]
            assert "card" not in ordinary["error"].lower() and metrics() == before
            settings(daily_tokens=0, task_tokens=1000)
            saved_chat = api("POST", "/api/tasks", json={"content": "fixture-budget: saved chat cap"})
            ordinary = wait_for(
                lambda: (
                    t if (t := api("GET", "/api/tasks/" + saved_chat["id"]))["status"] == "failed" else None
                ),
                "saved chat cap",
            )
            assert "saved when this chat started" in ordinary["error"]
            assert "card" not in ordinary["error"].lower()
            report["budget_messages"] = {
                "card": True,
                "workspace_message": True,
                "daily": True,
                "saved_chat": True,
            }
            settings(task_tokens=0)
            final = api("GET", "/api/settings")
            assert final["task_tokens"] == final["daily_tokens"] == 0
            print(
                "PASS: workspace and daily caps block work; removing caps restores ordinary chat", flush=True
            )
            (OUT / "results.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
        finally:
            for name in list(processes):
                stop(name)
            for log in logs:
                log.close()
