"""Exercise optional UI plugins in an isolated authenticated installation."""

import json
import socket
import tempfile
import threading
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

from shinbot.app import create_app
from shinbot.auth import hash_password
from shinbot.config import Config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-results/plugin-ui"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-plugin-ui-") as directory:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(directory), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-plugin-test-password"))
        session = core.auth.session()
        for name in ("paint", "ocean-theme"):
            folder = ROOT / "examples/plugins" / name
            core.plugins.stage({p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()})
        # A UI-only version of the toolbar example avoids unrelated service setup in this UI test.
        folder = ROOT / "examples/plugins/workflow-hooks"
        files = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        manifest = json.loads(files["shinbot-plugin.json"])
        manifest["hooks"] = []
        manifest["permissions"] = ["ui.patch"]
        manifest["files"].remove("hooks.py")
        files.pop("hooks.py")
        files["shinbot-plugin.json"] = json.dumps(manifest).encode()
        core.plugins.stage(files)

        async def runtime(method, path, **kwargs):
            return [] if path == "/v1/providers" else {"held": False}

        core.coordinator.request = runtime
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(origin)
                expect(page.get_by_role("button", name="Paint", exact=True)).to_have_count(0)
                page.get_by_role("button", name="Plugins", exact=True).click()

                def activate(name):
                    tile = page.locator(".plugin-tile").filter(
                        has=page.get_by_role("heading", name=name, exact=True)
                    )
                    tile.get_by_role("button", name="Finish setup").click()
                    expect(
                        page.get_by_text("This plugin can change ShinBot’s interface", exact=False)
                    ).to_be_visible()
                    page.get_by_role("button", name="Enable plugin", exact=True).click()

                activate("Paint")
                page.get_by_role("button", name="Paint", exact=True).click()
                canvas = page.get_by_label("Drawing canvas")
                expect(canvas).to_be_visible()
                box = canvas.bounding_box()
                page.mouse.move(box["x"] + 100, box["y"] + 90)
                page.mouse.down()
                page.mouse.move(box["x"] + 360, box["y"] + 200, steps=20)
                page.mouse.up()
                before = canvas.evaluate("(c)=>c.toDataURL()")
                page.get_by_role("button", name="Save drawing").click()
                expect(page.locator("#toast")).to_have_text("Drawing saved.")
                page.screenshot(path=str(OUT / "paint-light.png"))
                page.reload()
                page.get_by_role("button", name="Paint", exact=True).click()
                page.wait_for_function(
                    '(saved)=>document.querySelector("canvas")?.toDataURL()===saved', arg=before
                )
                page.get_by_role("button", name="Plugins", exact=True).click()
                activate("Ocean theme")
                page.wait_for_function(
                    '() => getComputedStyle(document.documentElement).getPropertyValue("--accent").trim()==="#006d77"'
                )
                activate("Workflow hooks")
                page.get_by_role("button", name="Chat", exact=True).click()
                expect(page.locator('[data-plugin-slot="composer-actions"] button')).to_have_text("Review")
                page.get_by_role("button", name="Review", exact=True).click()
                expect(page.get_by_role("textbox", name="Message ShinBot", exact=True)).to_have_value(
                    "Review the current changes. Check correctness, risks and missing tests."
                )
                expect(page.locator('[data-plugin-slot="composer-actions"] button')).to_have_count(1)
                page.get_by_role("button", name="Paint", exact=True).click()
                expect(canvas).to_be_visible()
                page.wait_for_function(
                    '(saved)=>document.querySelector("canvas")?.toDataURL()===saved', arg=before
                )
                page.evaluate('window.ShinBotTheme.set("dark")')
                page.wait_for_function(
                    '() => getComputedStyle(document.documentElement).getPropertyValue("--accent").trim()==="#80cbd0"'
                )
                page.wait_for_function(
                    "() => getComputedStyle(document.querySelector('.profile-button')).color === getComputedStyle(document.body).color"
                )
                page.screenshot(path=str(OUT / "paint-dark-theme.png"))
                page.get_by_role("button", name="Plugins", exact=True).click()
                for name in ("Paint", "Ocean theme", "Workflow hooks"):
                    tile = page.locator(".plugin-tile").filter(
                        has=page.get_by_role("heading", name=name, exact=True)
                    )
                    tile.get_by_role("button", name="Disable", exact=True).click()
                    expect(tile.get_by_text("Disabled", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Paint", exact=True)).to_have_count(0)
                expect(page.locator("link[data-plugin]")).to_have_count(0)
                page.get_by_role("button", name="Chat", exact=True).click()
                expect(page.locator('[data-plugin-slot="composer-actions"] button')).to_have_count(0)
                core.plugins.activate("paint", "1.0.0")
                core.plugins.activate("ocean-theme", "1.0.0")
                core.plugins.activate("workflow-hooks", "1.0.0")
                page.goto(origin + "/?extensions=off")
                expect(page.get_by_role("button", name="Paint", exact=True)).to_have_count(0)
                expect(page.locator("link[data-plugin]")).to_have_count(0)
                assert not errors, errors
                browser.close()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "verified": [
                            "UI activation and trust copy",
                            "sidebar module loading",
                            "drawing and persistent state after reload",
                            "light and dark custom styles",
                            "composer patch and cleanup",
                            "disable removes UI assets",
                            "recovery skips all UI plugins",
                        ],
                    }
                )
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
