"""Exercise real services on localhost with an isolated workspace and a deterministic model.

This explicitly enables the development Computer runner. It does not test Linux
container isolation and never loads the owner's credentials or persistent data.
"""

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
OUT = ROOT / "tmp/reference/production/local-services"
OUT.mkdir(parents=True, exist_ok=True)


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(fn, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except (httpx.HTTPError, FileNotFoundError):
            pass
        time.sleep(0.15)
    raise AssertionError("Timed out waiting for local service state")


def checked(response):
    response.raise_for_status()
    return response.json()


def process_metrics(pids):
    rows = []
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,time="], text=True)
    for line in output.splitlines():
        pid, parent, rss, cpu = line.split()
        parts = cpu.split(":")
        seconds = sum(float(p) * 60**i for i, p in enumerate(reversed(parts)))
        rows.append((int(pid), int(parent), int(rss), seconds))
    included = set(pids)
    while True:
        children = {pid for pid, parent, _, _ in rows if parent in included}
        if children <= included:
            break
        included |= children
    return {
        "rss_mib": round(sum(rss for pid, _, rss, _ in rows if pid in included) / 1024, 2),
        "cpu_seconds": round(sum(cpu for pid, _, _, cpu in rows if pid in included), 3),
        "processes": len(included),
    }


with tempfile.TemporaryDirectory(prefix="shinbot-local-") as temporary:
    directory = Path(temporary)
    ports = {name: port() for name in ["control", "runtime", "computer", "browser", "fixture"]}
    urls = {name: f"http://localhost:{value}" for name, value in ports.items()}
    tokens = {name: secrets.token_urlsafe(32) for name in ["runtime", "computer", "browser", "channel"]}
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SHINBOT_", "OPENAI_", "ANTHROPIC_", "SERPAPI_"))
    }
    env.update(
        SHINBOT_DATA=str(directory / "control"),
        SHINBOT_RUNTIME_DATA=str(directory / "runtime"),
        SHINBOT_BROWSER_DATA=str(directory / "browser"),
        SHINBOT_EXECUTOR_DATA=str(directory / "computer"),
        SHINBOT_WORKSPACE=str(directory / "workspace"),
        SHINBOT_HEADLESS="1",
        SHINBOT_DEV_EXECUTION="1",
        SHINBOT_CHILD_PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
        SHINBOT_SKILLS_CLI=str(ROOT / "computer/node_modules/skills/bin/cli.mjs"),
        SHINBOT_ORIGIN=urls["control"],
        SHINBOT_CONTROL_URL=urls["control"],
        SHINBOT_RUNTIME_URL=urls["runtime"],
        SHINBOT_COMPUTER_URL=urls["computer"],
        SHINBOT_BROWSER_URL=urls["browser"],
        SHINBOT_FIXTURE_URL=urls["fixture"] + "/v1",
        SHINBOT_FIXTURE_ORIGIN=urls["fixture"],
        # Auto mode opens only public addresses unprompted; the fixture site is on this machine.
        SHINBOT_BROWSER_TRUSTED_HOSTS=urls["fixture"].removeprefix("http://"),
        SHINBOT_SERVICE_TOKEN=tokens["runtime"],
        SHINBOT_COMPUTER_TOKEN=tokens["computer"],
        SHINBOT_BROWSER_TOKEN=tokens["browser"],
        PYTHONDONTWRITEBYTECODE="1",
    )
    processes = {}
    logs = []

    def launch(name):
        log = (OUT / (name + ".log")).open("w")
        logs.append(log)
        child = env.copy()
        if name == "runtime":
            child.update(PORT=str(ports[name]), HOST="127.0.0.1")
            command = ["node", str(ROOT / "runtime/dist/server.js")]
        else:
            app = {
                "control": "shinbot.app:create_app",
                "computer": "shinbot.services.computer:app",
                "browser": "shinbot.services.browser:app",
                "fixture": "fixture_model:app",
            }[name]
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
                command += ["--factory"]
            if name == "fixture":
                command += ["--app-dir", str(ROOT / "tests")]
            if name in {"browser", "computer"}:
                child["SHINBOT_EXECUTOR_TOKEN"] = tokens[name]
        processes[name] = subprocess.Popen(command, env=child, cwd=ROOT, stdout=log, stderr=log)

    try:
        for name in ports:
            launch(name)
        wait(lambda: httpx.get(urls["control"] + "/health").status_code == 200)
        c = httpx.Client(base_url=urls["control"], timeout=20)
        password = secrets.token_urlsafe(24)
        login = checked(
            c.post(
                "/auth/login",
                json={
                    "bootstrap": (directory / "control/bootstrap-token").read_text().strip(),
                    "password": password,
                    "cli": True,
                },
            )
        )
        c.headers["Authorization"] = "Bearer " + login["token"]
        wait(lambda: checked(c.get("/api/status"))["runtime_ready"])
        b = httpx.Client(
            base_url=urls["browser"], headers={"Authorization": "Bearer " + tokens["browser"]}, timeout=30
        )
        wait(lambda: checked(b.get("/health"))["ok"])
        assert checked(b.get("/health"))["browser_running"] is False
        before = process_metrics([p.pid for name, p in processes.items() if name != "fixture"])
        model_before = checked(httpx.get(urls["fixture"] + "/fixture-metrics"))["model_requests"]
        time.sleep(8)
        after = process_metrics([p.pid for name, p in processes.items() if name != "fixture"])
        assert checked(httpx.get(urls["fixture"] + "/fixture-metrics"))["model_requests"] == model_before
        report = {
            "idle_before_browser": after,
            "idle_cpu_seconds_over_8s": round(after["cpu_seconds"] - before["cpu_seconds"], 3),
            "idle_model_requests": 0,
            "mode": "host subprocesses, deterministic model, development Computer, headless Chromium",
        }
        settings = checked(c.get("/api/settings"))
        settings.update(provider="custom-fixture", model="fixture", computer_auto=True, browser_auto=True)
        # Exercise the existing Chromium setup; run_attachments covers fresh Camoufox installations.
        settings.update(browser_backend="chromium", browser_close_on_finish=False, approval_mode="full")
        checked(c.put("/api/settings", json=settings))
        assert checked(c.get("/api/plugins"))["installed"] == []
        assert c.get("/api/board").status_code == 409

        def complete(prompt):
            task = checked(c.post("/api/tasks", json={"content": prompt}))
            result = wait(
                lambda: (
                    r
                    if (r := checked(c.get("/api/tasks/" + task["id"])))["status"]
                    in {"completed", "failed", "uncertain"}
                    else None
                )
            )
            assert result["status"] == "completed", str(result)[:1000]
            return result

        complete("fixture-learn")
        learned = next(
            p for p in checked(c.get("/api/plugins"))["installed"] if p["id"].startswith("skill-learned-")
        )
        assert learned["status"] == "staged" and learned["access"]["bots"] == ["default"]
        memory_task = complete("fixture-memory-check")
        assert "Memory missing" in memory_task["messages"][-1]["answer"]
        checked(c.post("/api/memories", json={"text": "Use metric units", "bot_id": "default"}))
        checked(
            c.post(
                "/api/tasks/" + memory_task["id"] + "/messages",
                json={"content": "fixture-memory-check", "request_id": secrets.token_hex(16)},
            )
        )
        refreshed = wait(
            lambda: (
                r
                if (r := checked(c.get("/api/tasks/" + memory_task["id"])))["status"] == "completed"
                else None
            )
        )
        assert "Memory refreshed" in refreshed["messages"][-1]["answer"]
        complete("fixture-write")
        assert (directory / "workspace/fixture-result.txt").read_text() == "Persistent fixture result"
        complete("fixture-command")
        complete("fixture-browser")
        assert checked(b.get("/health"))["browser_running"] is True

        # Real selectors, browser lease and exact-once replay through the browser service.
        def browser_action(operation, **args):
            return {
                "id": secrets.token_hex(16),
                "epoch": "test",
                "task_id": "browser-routine",
                "browser_id": "shared",
                "approval_mode": "full",
                "tool": "browser",
                "args": {"operation": operation, **args},
            }

        observed = checked(b.post("/execute", json=browser_action("observe")))
        assert "Available appointments" in observed["content"][0]["text"]
        checked(b.post("/execute", json=browser_action("fill", selector="#note", text="Reminder saved")))
        payload = browser_action("click", selector="button")
        clicked = checked(b.post("/execute", json=payload))
        assert clicked == checked(b.post("/execute", json=payload))
        assert "Reminder saved" in clicked["content"][0]["text"]
        checked(b.post("/takeover", json={"owner": "test-owner", "browser_id": "shared"}))
        assert b.post("/execute", json=browser_action("click", selector="button")).status_code == 423
        checked(b.post("/release", json={"owner": "test-owner"}))
        assert b.post("/execute", json=browser_action("click", selector="button")).status_code == 400
        checked(b.post("/execute", json=browser_action("observe")))
        checked(b.post("/agent/release", json={"task_id": "browser-routine"}))
        report["after_browser_use"] = process_metrics(
            [p.pid for name, p in processes.items() if name != "fixture"]
        )
        checked(
            c.post("/api/plugins/stage", json={"marketplace": "official", "id": "kanban", "version": "1.0.0"})
        )
        checked(c.post("/api/plugins/kanban/1.0.0/activate", json={}))
        card = checked(
            c.post(
                "/api/board",
                json={
                    "title": "Verify a two-run goal",
                    "goal": "fixture-board: verify two acceptance criteria.",
                    "max_runs": 3,
                },
            )
        )
        checked(
            c.post(
                "/api/board/" + card["id"] + "/move", json={"status": "ready", "revision": card["revision"]}
            )
        )
        card = wait(
            lambda: next(
                (
                    r
                    for r in checked(c.get("/api/board"))
                    if r["id"] == card["id"] and r["status"] in {"review", "blocked"}
                ),
                None,
            ),
            timeout=60,
        )
        assert card["status"] == "review" and card["runs"] == 2, card
        assert len(checked(c.get("/api/board/" + card["id"] + "/runs"))) == 2
        # Restart gateway and runtime against the same private data.
        for name in ["control", "runtime"]:
            processes[name].terminate()
            processes[name].wait(timeout=15)
        for name in ["runtime", "control"]:
            launch(name)
        wait(lambda: checked(c.get("/api/status"))["runtime_ready"])
        recovered = next(r for r in checked(c.get("/api/board")) if r["id"] == card["id"])
        assert recovered["runs"] == 2 and recovered["status"] == "review"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1512, "height": 1000}, color_scheme="light")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(urls["control"])
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.locator('[data-view="plugins"]').click()
            expect(page.get_by_role("heading", name="Plugins", exact=True)).to_be_visible()
            page.screenshot(path=str(OUT / "plugins-light.png"), full_page=True)
            learned_card = page.locator(f'[data-plugin-card="{learned["id"]}@{learned["version"]}"]')
            learned_card.get_by_text("Details and permissions", exact=True).click()
            learned_card.get_by_role("button", name="Review files and scan", exact=True).click()
            expect(page.locator("#plugin-source")).to_contain_text("Write the file with the workspace tool")
            page.screenshot(path=str(OUT / "learned-skill-review.png"), full_page=True)
            page.keyboard.press("Escape")
            page.get_by_text("Add from another source", exact=True).click()
            page.get_by_role("button", name="Import from a link", exact=True).click()
            expect(page.get_by_label("Source URL")).to_be_visible()
            page.screenshot(path=str(OUT / "source-import.png"), full_page=True)
            page.keyboard.press("Escape")
            page.emulate_media(color_scheme="dark")
            page.screenshot(path=str(OUT / "plugins-dark.png"), full_page=True)
            page.emulate_media(color_scheme="light")
            page.locator('[data-view="board"]').click()
            expect(page.get_by_role("heading", name="Board", exact=True)).to_be_visible()
            page.get_by_role("button", name="New card", exact=True).click()
            page.get_by_label("Title", exact=True).fill("Plan the week")
            page.get_by_label("Goal and acceptance checks").fill(
                "Create a schedule with time for focused work and exercise."
            )
            page.get_by_role("button", name="Save card", exact=True).click()
            expect(page.get_by_role("button", name="Plan the week", exact=True)).to_be_visible()
            page.screenshot(path=str(OUT / "board-light.png"), full_page=True)
            page.emulate_media(color_scheme="dark")
            page.screenshot(path=str(OUT / "board-dark.png"), full_page=True)
            page.get_by_role("button", name="Plan the week", exact=True).click()
            page.get_by_label("Title", exact=True).fill("Plan next week")
            page.get_by_role("button", name="Save card", exact=True).click()
            expect(page.get_by_role("button", name="Plan next week", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(OUT / "board-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert not errors, errors
            context.close()
            browser.close()
        report["checks"] = [
            "real Pi Durable tool dispatch",
            "learned skill proposal with tool evidence",
            "memory refresh across turns",
            "Plugins review and source import UI",
            "workspace file and shell command",
            "browser navigation, form, replay and human takeover",
            "two-run Kanban continuation",
            "gateway/runtime restart persistence",
            "Kanban create and edit UI",
            "light/dark/mobile rendering",
        ]
        (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        c.close()
        b.close()
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
