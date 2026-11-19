"""Verify compact chat controls and approval decisions through the real web UI."""

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
from shinbot.db import uid

OUT = Path(__file__).resolve().parents[1] / "test-results/chat-controls"
PROVIDERS = [
    {
        "id": "test",
        "name": "Connected provider",
        "configured": True,
        "models": [
            {"id": "quick", "name": "Quick model", "thinkingLevels": ["off"]},
            {"id": "deep", "name": "Deep model", "thinkingLevels": ["off", "low", "medium", "high", "xhigh"]},
        ],
    }
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-controls-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-controls-test-password"))
        core.db.set("preferences", {**core.db.setting("preferences"), "provider": "test", "model": "quick"})
        session = core.auth.session()
        server_loop = {}

        async def runtime(method, path, **kwargs):
            server_loop["value"] = asyncio.get_running_loop()
            return PROVIDERS if path == "/v1/providers" else {"held": False}

        async def remote(*args, **kwargs):
            return {"ok": True, "available": True, "lease": None, "tabs": []}

        core.remote = remote
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
                page.emulate_media(reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                page.locator("#new-task").click()
                expect(page.get_by_label("Approval mode", exact=True)).to_have_value("auto")
                page.locator("#prompt").fill("Check the repository and explain the results.")
                page.get_by_role("button", name="Choose model", exact=True).click()
                page.get_by_role("searchbox", name="Search models").fill("deep")
                page.get_by_role("button", name="Deep model Connected provider").click()
                expect(page.get_by_role("dialog", name="Choose model")).to_be_visible()
                page.locator('.effort-step[title="High"]').click()
                expect(page.get_by_role("radio", name="High", exact=True)).to_be_checked()
                page.get_by_role("radio", name="High", exact=True).press("ArrowLeft")
                expect(page.get_by_role("radio", name="Medium", exact=True)).to_be_checked()
                page.get_by_role("radio", name="Medium", exact=True).press("ArrowRight")
                expect(page.get_by_role("radio", name="High", exact=True)).to_be_checked()
                expect(page.locator("#composer-effort-summary")).to_have_text("Thinking effort: High.")
                expect(page.locator("#prompt")).to_have_value("Check the repository and explain the results.")
                page.get_by_role("searchbox", name="Search models").press("Escape")
                expect(page.get_by_role("button", name="Choose model", exact=True)).to_be_focused()
                page.get_by_label("Approval mode", exact=True).select_option("full")
                # Full access is confirmed before it applies.
                page.get_by_role("button", name="Use full access", exact=True).click()
                expect(page.get_by_label("Approval mode", exact=True)).to_have_value("full")
                page.get_by_role("button", name="Send", exact=True).click()
                expect(page.locator("#task-title")).to_have_text(
                    "Check the repository and explain the results."
                )
                task = core.db.one("SELECT * FROM tasks")
                assert (task["provider"], task["model"], task["thinking_level"], task["approval_mode"]) == (
                    "test",
                    "deep",
                    "high",
                    "full",
                )
                expect(page.locator('.effort-levels input[value="high"]')).to_be_disabled()
                page.locator("#new-task").click()
                expect(page.get_by_label("Approval mode", exact=True)).to_have_value("auto")
                page.locator(f'[data-task="{task["id"]}"]').click()
                page.get_by_label("Approval mode", exact=True).select_option("ask")
                expect(page.get_by_label("Approval mode", exact=True)).to_have_value("ask")
                page.reload()
                page.locator(f'[data-task="{task["id"]}"]').click()
                expect(page.get_by_label("Approval mode", exact=True)).to_have_value("ask")
                core.db.execute(
                    "UPDATE messages SET status='completed',answer='The repository checks passed.',finished=? WHERE task_id=?",
                    (time.time(), task["id"]),
                )
                core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task["id"],))
                expect(page.locator('.effort-levels input[value="high"]')).to_be_enabled(timeout=15000)
                page.get_by_role("button", name="Choose model", exact=True).click()
                page.get_by_role("searchbox", name="Search models").fill("quick")
                page.get_by_role("button", name="Quick model Connected provider").click()
                expect(page.get_by_role("radio", name="Off", exact=True)).to_be_checked()
                expect(page.get_by_role("radio", name="Off", exact=True)).to_be_disabled()
                page.get_by_role("searchbox", name="Search models").press("Escape")
                page.get_by_label("Approval mode", exact=True).select_option("auto")
                expect(page.get_by_label("Approval mode", exact=True)).to_have_value("auto")
                request = {
                    "id": uid(),
                    "task_id": task["id"],
                    "epoch": core.db.setting("epoch"),
                    "tool": "run_command",
                    "args": {"command": "rm -rf obsolete-project"},
                }
                assert (
                    asyncio.run_coroutine_threadsafe(core.action(request), server_loop["value"]).result(10)[
                        "status"
                    ]
                    == "pending"
                )
                expect(page.get_by_role("button", name="Allow once", exact=True)).to_be_visible(timeout=15000)
                expect(page.get_by_role("button", name="Always allow", exact=True)).to_have_count(0)
                expect(page.locator(".approval")).to_contain_text("This action can change files")
                page.screenshot(path=str(OUT / "auto-approval-light.png"), full_page=True)
                page.get_by_role("button", name="Decline", exact=True).click()
                expect(page.locator(f'[data-entry="action-{request["id"]}"]')).to_contain_text("Declined")
                page.get_by_role("button", name="Choose model", exact=True).click()
                page.get_by_role("searchbox", name="Search models").fill("deep")
                page.get_by_role("button", name="Deep model Connected provider").click()
                page.locator('.effort-step[title="High"]').click()
                expect(page.get_by_role("radio", name="High", exact=True)).to_be_checked()
                page.get_by_role("radio", name="High", exact=True).press("Escape")
                for theme in ("light", "dark"):
                    page.evaluate("theme => window.ShinBotTheme.set(theme)", theme)
                    for width in (320, 390, 768, 1440):
                        page.set_viewport_size({"width": width, "height": 900})
                        layout = page.evaluate('''() => {
                          const r=s=>document.querySelector(s).getBoundingClientRect();
                          const model=r('#composer-model'), approval=r('#composer-approval'), icon=r('.composer-approval>.icon'), send=r('.send-button');
                          return {model:model.y+model.height/2,approval:approval.y+approval.height/2,icon:icon.y+icon.height/2,send:send.y+send.height/2,overflow:document.documentElement.scrollWidth>innerWidth};
                        }''')
                        assert not layout['overflow'], (theme,width,layout)
                        assert abs(layout['model']-layout['approval'])<1, (theme,width,layout)
                        assert abs(layout['icon']-layout['approval'])<1, (theme,width,layout)
                        assert abs(layout['send']-layout['approval'])<1, (theme,width,layout)
                        page.get_by_role("button", name="Choose model", exact=True).click()
                        expect(page.get_by_role("radio", name="High", exact=True)).to_be_checked()
                        expect(page.get_by_role("radiogroup", name="Thinking effort")).to_be_in_viewport()
                        page.screenshot(path=str(OUT / f"combined-picker-{theme}-{width}.png"))
                        page.get_by_role("radio", name="High", exact=True).press("Escape")
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.evaluate("window.ShinBotTheme.set('dark')")
                page.get_by_role("button", name="Choose model", exact=True).click()
                page.screenshot(path=str(OUT / "model-picker-dark.png"), full_page=True)
                page.get_by_role("searchbox", name="Search models").press("Escape")
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("#prompt").fill("Keep this unsent draft.")
                page.get_by_role("button", name="Choose model", exact=True).click()
                expect(page.get_by_role("searchbox", name="Search models")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(OUT / "model-picker-mobile.png"), full_page=True)
                page.locator("[data-model-settings]").click()
                expect(page.locator("#connections-panel")).to_be_visible()
                page.get_by_role("tab", name="Permissions", exact=True).click()
                page.locator('[name="approval_mode"]').select_option("auto")
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.locator("#toast")).to_have_text("Settings saved.")
                page.get_by_role("tab", name="Workspace", exact=True).click()
                expect(page.get_by_role("combobox", name="Default model", exact=True)).to_have_value("quick")
                effort = page.get_by_role("combobox", name="Default thinking effort", exact=True)
                expect(effort).to_have_value("off")
                expect(effort).to_be_disabled()
                page.get_by_role("combobox", name="Default model", exact=True).select_option("deep")
                expect(effort).to_be_enabled()
                effort.select_option("high")
                page.get_by_role("combobox", name="Default model", exact=True).select_option("quick")
                expect(effort).to_have_value("off")
                expect(effort).to_be_disabled()
                page.get_by_role("combobox", name="Default model", exact=True).select_option("deep")
                effort.select_option("high")
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.locator("#toast")).to_have_text("Settings saved.")
                expect(effort).to_have_value("high")
                for theme, width in (("light", 1440), ("dark", 390)):
                    page.set_viewport_size({"width": width, "height": 1000})
                    page.evaluate("theme => window.ShinBotTheme.set(theme)", theme)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(OUT / f"workspace-defaults-{theme}.png"), full_page=True)
                page.reload()
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator("#new-task").click()
                expect(page.locator(".composer-model-name")).to_have_text("Deep model")
                expect(page.locator("#composer-effort-summary")).to_have_text("Thinking effort: High.")
                page.locator("#prompt").fill("Use the workspace default model and effort.")
                page.get_by_role("button", name="Send", exact=True).click()
                expect(page.locator("#task-title")).to_have_text("Use the workspace default model and effort.")
                inherited = core.db.one("SELECT * FROM tasks WHERE title=?", ("Use the workspace default model and effort.",))
                assert (inherited["model"], inherited["thinking_level"]) == ("deep", "high")
                # Saving unrelated settings during a provider outage preserves these defaults.
                page.route("**/api/providers", lambda route: route.fulfill(status=503, json={"detail": "Model service is reconnecting."}))
                page.get_by_role("button", name="Choose model", exact=True).click()
                page.locator("[data-model-settings]").click()
                page.get_by_role("tab", name="Workspace", exact=True).click()
                expect(effort).to_have_value("high")
                expect(effort).to_be_disabled()
                page.get_by_label("Workspace name", exact=True).fill("My workspace")
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.locator("#toast")).to_have_text("Settings saved.")
                saved = core.db.setting("preferences")
                assert (saved["name"], saved["model"], saved["thinking_level"]) == ("My workspace", "deep", "high")
                assert not errors, errors
                browser.close()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "verified": [
                            "compact model search",
                            "supported effort choices",
                            "effort bar keyboard control",
                            "aligned model, approval icon and send button at four sizes",
                            "draft preservation",
                            "keyboard dismissal",
                            "per-chat persistence",
                            "active-run model lock",
                            "Auto destructive approval",
                            "decline",
                            "Settings link and save",
                            "workspace model and effort defaults survive reload and apply to new chats",
                            "unsupported effort resets on model change",
                            "provider outage preserves defaults when saving settings",
                            "dark/light/mobile",
                        ],
                    }
                )
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
