"""Exercise the native Computer panel with an isolated, real browser service."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from playwright.sync_api import expect, sync_playwright

from shinbot.app import create_app
from shinbot.auth import hash_password
from shinbot.config import Config

OUT = Path(__file__).resolve().parents[1] / "test-results/native-browser"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait(check):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("Native browser did not reach the expected state")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-browser-ui-") as temporary:
        root = Path(temporary)
        token = secrets.token_urlsafe(32)
        port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHINBOT_")}
        env.update(
            SHINBOT_EXECUTOR_TOKEN=token,
            SHINBOT_BROWSER_DATA=str(root / "browser"),
            SHINBOT_BROWSER_VIEW="native",
            SHINBOT_HEADLESS="0",
            # The agent navigation below targets the test origin on loopback, which Auto would ask about.
            SHINBOT_BROWSER_TRUSTED_HOSTS="127.0.0.1",
        )
        log = (OUT / "browser.log").open("w")
        service = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "shinbot.services.browser:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        direct = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + token},
            timeout=25,
            trust_env=False,
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(
            Config(
                data=root / "control",
                origin=origin,
                coordinate=False,
                browser_url=f"http://127.0.0.1:{port}",
                browser_token=token,
            )
        )
        core = app.state.core
        core.db.set("password", hash_password("isolated-browser-test-password"))
        session = core.auth.session()
        record = {"clicks": 0, "text": "", "scroll": 0}
        fail_frame = {"once": True}

        @app.get("/fixture", response_class=HTMLResponse)
        def fixture():
            return '<html><head><link rel="stylesheet" href="/fixture.css"></head><body><h1>Browser control test</h1><input id="text" aria-label="Test text"><button id="save">Save once</button><output id="result">Ready</output><script src="/fixture.js"></script></body></html>'

        @app.get("/fixture.css")
        def fixture_css():
            return Response(
                "body{margin:0;background:#e9f2ff;color:#102b50;font:24px sans-serif;height:2400px}h1{position:absolute;left:100px;top:60px}input{position:absolute;left:100px;top:150px;width:400px;height:44px;font-size:24px}button{position:absolute;left:100px;top:220px;width:200px;height:44px}output{position:absolute;left:100px;top:300px}",
                media_type="text/css",
            )

        @app.get("/fixture.js")
        def fixture_js():
            return Response(
                'let clicks=0;const text=document.querySelector("#text");function record(){fetch("/fixture-record",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({clicks,text:text.value,scroll:scrollY})})}text.oninput=record;onscroll=record;document.querySelector("#save").onclick=()=>{clicks++;document.querySelector("#result").textContent="Saved "+clicks;record()};',
                media_type="application/javascript",
            )

        @app.post("/fixture-record")
        async def fixture_record(request: Request):
            record.update(await request.json())
            return {"ok": True}

        original_remote = core.remote

        async def remote(name, path, *args, **kwargs):
            if name != "browser":
                return {"ok": True}
            if path == "/view" and fail_frame["once"]:
                fail_frame["once"] = False
                raise httpx.ConnectError("Isolated preview interruption")
            return await original_remote(name, path, *args, **kwargs)

        async def runtime(method, path, **kwargs):
            return (
                [
                    {
                        "id": "test",
                        "name": "Test",
                        "configured": True,
                        "models": [{"id": "test", "name": "Test"}],
                    }
                ]
                if path == "/v1/providers"
                else {"held": False}
            )

        core.remote, core.coordinator.request = remote, runtime
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            wait(lambda: direct.get("/health").is_success)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1050})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                ui = context.new_page()
                errors = []
                ui.on("pageerror", lambda error: errors.append(str(error)))
                ui.goto(origin)
                ui.get_by_role("button", name="Computer", exact=True).click()
                expect(ui.get_by_role("heading", name="Browser ready")).to_be_visible(timeout=15000)
                assert direct.get("/health").json()["browser_running"] is False
                ui.screenshot(path=str(OUT / "ready-light.png"))
                with ui.expect_response(
                    lambda response: response.url.endswith("/api/browser/takeover"), timeout=45000
                ) as takeover:
                    ui.get_by_role("button", name="Take control", exact=True).click()
                assert takeover.value.ok, takeover.value.text()
                expect(ui.get_by_label("Website address")).to_be_visible()
                ui.get_by_label("Website address").fill(origin + "/fixture")
                ui.get_by_role("button", name="Go", exact=True).click()
                image = ui.get_by_alt_text("Current browser page")
                expect(image).to_be_visible(timeout=15000)
                ui.wait_for_function("document.querySelector('#browser-screen img')?.naturalWidth === 1280")

                def click_remote(x, y, double=False):
                    box = image.bounding_box()
                    scale = min(box["width"] / 1280, box["height"] / 800)
                    (ui.mouse.dblclick if double else ui.mouse.click)(
                        box["x"] + (box["width"] - 1280 * scale) / 2 + x * scale,
                        box["y"] + (box["height"] - 800 * scale) / 2 + y * scale,
                    )

                click_remote(180, 172)
                ui.keyboard.type("Hello")
                wait(lambda: record["text"] == "Hello")
                # Screenshot polling must preserve keyboard focus.
                with ui.expect_response(lambda response: response.url.endswith("/api/browser/view")):
                    pass
                expect(ui.get_by_label("Type in the remote browser")).to_be_focused()
                ui.keyboard.insert_text(" world")
                wait(lambda: record["text"] == "Hello world")
                click_remote(180, 240)
                try:
                    wait(lambda: record["clicks"] >= 1)
                    assert record["clicks"] == 1, record
                except BaseException:
                    ui.screenshot(path=str(OUT / "failed-click.png"))
                    print("fixture", record, flush=True)
                    print("ui error", ui.locator("#toast").text_content(), flush=True)
                    raise
                click_remote(180, 240, double=True)
                wait(lambda: record["clicks"] >= 3)
                assert record["clicks"] == 3, record
                ui.evaluate("window.ShinBotTheme.set('dark')")
                ui.screenshot(path=str(OUT / "control-dark.png"))
                box = image.bounding_box()
                ui.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                ui.mouse.wheel(0, 500)
                wait(lambda: record["scroll"] > 0)
                state = direct.get("/lease").json()
                assert (
                    direct.post(
                        "/input",
                        json={
                            "owner": "different-device",
                            "generation": state["generation"],
                            "event": {"type": "text", "text": "no"},
                        },
                    ).status_code
                    == 423
                )
                blocked = direct.post(
                    "/execute",
                    json={
                        "id": "agent-blocked",
                        "task_id": "agent",
                        "epoch": "test",
                        "args": {"operation": "observe"},
                    },
                )
                assert blocked.status_code == 423
                ui.get_by_role("button", name="Hand back", exact=True).click()
                expect(ui.get_by_role("button", name="Take control", exact=True)).to_be_visible()
                assert (
                    direct.post(
                        "/input",
                        json={
                            "owner": state["lease"]["owner"],
                            "generation": state["generation"],
                            "event": {"type": "text", "text": "no"},
                        },
                    ).status_code
                    == 423
                )
                # Read-only previews must not switch the profile leased by an agent.
                result = direct.post(
                    "/execute",
                    json={
                        "id": "agent-nav",
                        "task_id": "agent",
                        "epoch": "test",
                        "browser_id": state["browser_id"],
                        "args": {"operation": "goto", "url": origin + "/fixture"},
                    },
                )
                result.raise_for_status()
                assert not result.json().get("isError")
                expect(ui.locator("#browser-mode")).to_have_text("View only")
                ui.get_by_role("button", name="Home", exact=True).click()
                ui.get_by_role("button", name="Computer", exact=True).click()
                expect(image).to_be_visible()
                assert direct.get("/lease").json()["lease"] == {"kind": "agent", "owner": "agent"}
                ui.get_by_role("button", name="Settings", exact=True).click()
                ui.get_by_role("tab", name="Browser", exact=True).click()
                expect(ui.locator("#browser-availability")).to_contain_text("Ready")
                expect(ui.locator('[name="browser_backend"]')).to_have_value("camoufox")
                ui.locator('[name="browser_close_on_finish"]').uncheck()
                with ui.expect_response(
                    lambda r: r.url.endswith("/api/settings") and r.request.method == "PUT"
                ) as saved:
                    ui.get_by_role("button", name="Save settings", exact=True).click()
                assert saved.value.status == 200, saved.value.text()
                expect(ui.locator('[name="browser_close_on_finish"]')).not_to_be_checked()
                assert core.db.setting("preferences")["browser_close_on_finish"] is False
                expect(ui.get_by_role("button", name="Save settings", exact=True)).to_be_enabled()
                ui.screenshot(
                    path=str(OUT / "browser-settings-dark.png"), full_page=True, animations="disabled"
                )
                ui.evaluate("window.ShinBotTheme.set('light')")
                ui.screenshot(
                    path=str(OUT / "browser-settings-light.png"), full_page=True, animations="disabled"
                )
                ui.set_viewport_size({"width": 390, "height": 844})
                expect(ui.locator('[name="browser_backend"]')).to_be_visible()
                assert ui.evaluate("document.documentElement.scrollWidth <= innerWidth")
                ui.screenshot(
                    path=str(OUT / "browser-settings-mobile.png"), full_page=True, animations="disabled"
                )
                # A new page starts a fresh fixture counter; the first page's click was already checked.
                assert not errors, errors
                browser.close()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "verified": [
                            "lazy background startup",
                            "initial preview retry",
                            "native in-panel navigation",
                            "click, keyboard, text and scrolling",
                            "stable input focus",
                            "owner and generation checks",
                            "human lease blocks agent",
                            "hand-back blocks old inputs",
                            "read-only preview preserves agent lease",
                            "one click produces one effect",
                        ],
                    }
                )
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            service.terminate()
            service.wait(timeout=20)
            direct.close()
            log.close()


if __name__ == "__main__":
    main()
