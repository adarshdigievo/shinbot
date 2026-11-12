"""Exercise a real Apple Linux desktop, takeover, persistence and the web viewer."""

import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright
from run_attachments import checked, free_port, wait

from shinbot.desktop_host import container_name, create_command, endpoints, offline_copy

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results/desktop"


def main():
    runtime = os.environ["SHINBOT_TEST_CONTAINER"]
    image = os.getenv("SHINBOT_TEST_DESKTOP_IMAGE", "shinbot-desktop:local")
    OUT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="shinbot-desktop-test-"))
    ports = {name: free_port() for name in ("control", "computer", "browser", "runtime")}
    tokens = {name: secrets.token_urlsafe(32) for name in ("runtime", "computer", "browser", "channel")}
    config = {"desktop": {"runtime": runtime, "image": image}, "ports": ports}
    for directory in ("workspace", "desktop-data/computer"):
        (root / directory).mkdir(parents=True)
    environment = root / "desktop.env"
    environment.write_text(
        "\n".join(
            [f"SHINBOT_{k.upper()}_TOKEN={tokens[k]}" for k in ("computer", "browser")]
            + [
                f"SHINBOT_WORKSPACE_UID={os.getuid()}",
                f"SHINBOT_WORKSPACE_GID={os.getgid()}",
                "SHINBOT_SHARED_WORKSPACE=1",
            ]
        )
    )
    environment.chmod(0o600)
    name = container_name(root)

    def commands(*args):
        return subprocess.run([runtime, *args], check=True, capture_output=True, text=True, timeout=120)

    process = runtime_process = None
    try:
        commands("volume", "create", "--opt", "size=20g", name + "-data")
        subprocess.run(create_command(root, config, environment), check=True, capture_output=True)
        commands("start", name)
        urls = endpoints(json.loads(commands("inspect", name).stdout)[0])
        browser = httpx.Client(
            base_url=urls["browser"],
            headers={"Authorization": "Bearer " + tokens["browser"]},
            trust_env=False,
            timeout=60,
        )
        computer = httpx.Client(
            base_url=urls["computer"],
            headers={"Authorization": "Bearer " + tokens["computer"]},
            trust_env=False,
            timeout=60,
        )

        def ready():
            status = json.loads(commands("inspect", name).stdout)[0]["status"]["state"]
            if status != "running":
                raise AssertionError(commands("logs", "-n", "30", name).stdout)
            return browser.get("/health").is_success

        wait(ready, timeout=120)
        wait(lambda: computer.get("/health").is_success)
        assert checked(browser.get("/lease"))["desktop"] is True
        assert checked(browser.get("/health"))["browser_running"] is False
        print("Desktop services healthy; browser stays closed.", flush=True)

        def shortcut(*arguments):
            return subprocess.run(
                [
                    runtime,
                    "exec",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    name,
                    "env",
                    "HOME=/workspace",
                    "DISPLAY=:99",
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )

        denied = shortcut("shinbot-open-browser")
        assert denied.returncode != 0 and "Take control" in denied.stderr, denied.stderr
        assert checked(browser.get("/health"))["browser_running"] is False
        checked(browser.post("/select", json={"browser_id": "shared", "backend": "camoufox"}))

        def effect(client, tool, args, task="desktop-test", endpoint="/execute", key=None):
            data = {
                "id": key or uuid.uuid4().hex,
                "task_id": task,
                "epoch": "test",
                "tool": tool,
                "args": args,
                "approval_mode": "full",
                "backend": "camoufox",
                "browser_id": "shared",
            }
            response = client.post(endpoint, json=data)
            if not response.is_success:
                print(response.text[:1500], flush=True)
            return checked(response)

        result = effect(
            computer, "run_command", {"command": "id -u; printf persistent > /workspace/desktop-proof.txt"}
        )
        assert str(os.getuid()) in result["output"], result
        probe = effect(
            computer,
            "run_command",
            {
                "command": "python -c \"import os; from pathlib import Path; assert not any(k.endswith('_TOKEN') for k in os.environ); assert not os.access('/run/shinbot-desktop', os.W_OK); assert not os.access('/data/computer', os.W_OK); print('service boundaries intact')\""
            },
        )
        assert "service boundaries intact" in probe["output"], probe
        effect(
            browser, "desktop", {"operation": "launch", "application": "files"}, endpoint="/desktop/execute"
        )
        observe = effect(browser, "desktop", {"operation": "observe"}, endpoint="/desktop/execute")
        pixels = base64.b64decode(observe["content"][1]["data"])
        assert pixels.startswith(b"\x89PNG")
        (OUT / "desktop.png").write_bytes(pixels)
        # The owner takes control without forcing a browser to launch.
        checked(browser.post("/takeover", json={"owner": "tester"}))
        assert checked(browser.get("/health"))["browser_running"] is False
        blocked = computer.post(
            "/execute",
            json={
                "id": uuid.uuid4().hex,
                "task_id": "desktop-test",
                "epoch": "test",
                "tool": "write_file",
                "args": {"path": "blocked.txt", "content": "bad"},
            },
        )
        assert blocked.status_code == 423, blocked.text
        assert not (root / "workspace/blocked.txt").exists()
        opened = shortcut("exo-open", "--launch", "WebBrowser")
        assert opened.returncode == 0, opened.stderr
        wait(lambda: checked(browser.get("/health"))["browser_running"] is True, timeout=60)
        opened = shortcut("exo-open", "https://example.com/?shinbot=desktop-launcher")
        assert opened.returncode == 0, opened.stderr
        wait(
            lambda: checked(browser.get("/state"))["url"] == "https://example.com/?shinbot=desktop-launcher",
            timeout=30,
        )
        assert checked(browser.get("/lease"))["lease"] == {"kind": "human", "owner": "tester"}
        print("XFCE default browser and desktop URL shortcuts use the managed Camoufox session.", flush=True)
        checked(browser.post("/release", json={"owner": "tester"}))
        print("Workspace and takeover fence passed.", flush=True)
        effect(
            browser, "desktop", {"operation": "launch", "application": "browser"}, endpoint="/desktop/execute"
        )
        assert checked(browser.get("/health"))["browser_running"] is True
        page = effect(browser, "browser", {"operation": "goto", "url": "https://example.com"})
        assert not page.get("isError"), page
        capture_id = uuid.uuid4().hex
        shot = effect(
            browser, "desktop", {"operation": "screenshot"}, endpoint="/desktop/execute", key=capture_id
        )
        content = checked(browser.get("/files/" + shot["file"]["digest"]))
        (OUT / "desktop-browser.png").write_bytes(base64.b64decode(content["data"]))
        checked(browser.post("/agent/release", json={"task_id": "desktop-test", "close": True}))
        assert checked(browser.get("/health"))["browser_running"] is False
        print("Camoufox, screenshots and browser close passed.", flush=True)
        # A guest restart preserves workspace, browser profile and screenshot receipts.
        commands("stop", "--time", "20", name)
        commands("start", name)
        urls = endpoints(json.loads(commands("inspect", name).stdout)[0])
        browser.base_url = urls["browser"]
        computer.base_url = urls["computer"]
        wait(lambda: browser.get("/health").is_success, timeout=120)
        assert (root / "workspace/desktop-proof.txt").read_text() == "persistent"
        replay = effect(
            browser, "desktop", {"operation": "screenshot"}, endpoint="/desktop/execute", key=capture_id
        )
        assert replay == shot
        assert checked(browser.get("/files/" + shot["file"]["digest"]))["data"] == content["data"]
        print("Guest restart and persistent receipts passed.", flush=True)
        origin = f"http://127.0.0.1:{ports['control']}"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SHINBOT_", "OPENAI_", "ANTHROPIC_"))}
        env.update(
            SHINBOT_DATA=str(root / "control"),
            SHINBOT_RUNTIME_DATA=str(root / "runtime"),
            SHINBOT_ORIGIN=origin,
            SHINBOT_CONTROL_URL=origin,
            SHINBOT_DESKTOP="1",
            SHINBOT_COMPUTER_URL=str(computer.base_url).rstrip("/"),
            SHINBOT_BROWSER_URL=str(browser.base_url).rstrip("/"),
            SHINBOT_RUNTIME_URL=f"http://127.0.0.1:{ports['runtime']}",
            SHINBOT_COMPUTER_TOKEN=tokens["computer"],
            SHINBOT_BROWSER_TOKEN=tokens["browser"],
            SHINBOT_SERVICE_TOKEN=tokens["runtime"],
            SHINBOT_CHANNEL_TOKEN=tokens["channel"],
        )
        runtime_log = (OUT / "runtime.log").open("w")
        runtime_process = subprocess.Popen(
            ["node", str(ROOT / "runtime/dist/server.js")],
            cwd=ROOT,
            env={**env, "HOST": "127.0.0.1", "PORT": str(ports["runtime"])},
            stdout=runtime_log,
            stderr=runtime_log,
        )
        log = (OUT / "gateway.log").open("w")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "shinbot.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports["control"]),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        with httpx.Client(base_url=origin, timeout=30, trust_env=False) as client:
            wait(lambda: client.get("/health").is_success)
            password = secrets.token_urlsafe(24)
            checked(
                client.post(
                    "/auth/login",
                    json={
                        "bootstrap": (root / "control/bootstrap-token").read_text().strip(),
                        "password": password,
                        "cli": True,
                    },
                )
            )
            with sync_playwright() as p:
                web = p.chromium.launch(headless=True)
                context = web.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                page.goto(origin)
                page.get_by_label("Password", exact=True).fill(password)
                page.get_by_role("button", name="Sign in", exact=True).click()
                page.locator('[data-view="computer"]').click()
                expect(page.locator("#browser-screen canvas")).to_be_visible(timeout=20000)
                expect(page.locator("#browser-address")).to_have_text("Linux desktop · /workspace")
                page.screenshot(path=str(OUT / "computer-view.png"), full_page=True)
                page.get_by_role("button", name="Take control", exact=True).click()
                expect(page.get_by_role("button", name="Hand back", exact=True)).to_be_visible()
                page.locator('[data-desktop-app="terminal"]').click()
                # Use the viewer's actual keyboard channel to write a file in the guest.
                canvas = page.locator("#browser-screen canvas")
                time.sleep(2)
                canvas.focus()
                page.keyboard.type("printf viewer ", delay=25)
                page.keyboard.press("Shift+Period")
                page.keyboard.type(" /workspace/viewer-proof.txt", delay=25)
                page.keyboard.press("Enter")
                page.screenshot(path=str(OUT / "computer-takeover.png"), full_page=True)
                wait(lambda: (root / "workspace/viewer-proof.txt").exists())
                page.get_by_role("button", name="Hand back", exact=True).click()
                context.close()
                web.close()
        process.terminate()
        process.wait(15)
        process = None
        commands("stop", "--time", "20", name)
        backup = root / "backup"
        backup.mkdir()
        offline_copy(root, config, backup)
        assert (backup / "desktop-data/computer").is_dir()
        import shutil

        shutil.copytree(backup / "desktop-data/computer", root / "computer-data")
        offline_copy(root, config, root, restore_computer=True)
        print("Offline desktop backup and Computer data transfer passed.", flush=True)
        print(
            "PASS: Apple desktop VM, Camoufox window, unprivileged shared workspace, takeover fence, screenshot replay, guest restart, authenticated noVNC keyboard control"
        )
    finally:
        logs = subprocess.run([runtime, "logs", "-n", "100", name], capture_output=True, text=True)
        (OUT / "desktop.log").write_text(logs.stdout + logs.stderr)
        if process:
            process.terminate()
            try:
                process.wait(15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if runtime_process:
            runtime_process.terminate()
            runtime_process.wait(15)
        subprocess.run([runtime, "stop", "--time", "10", name], capture_output=True)
        subprocess.run([runtime, "delete", name], capture_output=True)
        subprocess.run([runtime, "volume", "delete", name + "-data"], capture_output=True)
        print("Isolated test data:", root)


if __name__ == "__main__":
    main()
