"""Exercise first-run Home against a fresh gateway, with an explicit fake credential endpoint."""

import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = Path("tmp/reference/implementation/screenshots")
OUT.mkdir(parents=True, exist_ok=True)

runtime_providers = None


class FixtureRuntime(BaseHTTPRequestHandler):
    def do_GET(self):
        available = self.path == "/v1/providers" and runtime_providers is not None
        body = json.dumps(runtime_providers if available else {"detail": "Fixture unavailable"}).encode()
        self.send_response(200 if available else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


with tempfile.TemporaryDirectory(prefix="shinbot-onboarding-") as directory:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://localhost:{port}"
    env = {key: value for key, value in os.environ.items() if not key.startswith("SHINBOT_")}
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), FixtureRuntime)
    threading.Thread(target=runtime.serve_forever, daemon=True).start()
    env.update(
        SHINBOT_DATA=directory,
        SHINBOT_ORIGIN=url,
        SHINBOT_RUNTIME_URL=f"http://127.0.0.1:{runtime.server_port}",
        SHINBOT_SERVICE_TOKEN=secrets.token_urlsafe(32),
    )
    code = f"import uvicorn; from shinbot.app import create_app; from shinbot.config import Config; uvicorn.run(create_app(Config(coordinate=False)),host='127.0.0.1',port={port},access_log=False)"
    with open(Path(directory) / "server.log", "w") as log:
        process = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if httpx.get(url + "/auth/status", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Isolated onboarding gateway did not start")
            token = (Path(directory) / "bootstrap-token").read_text().strip()
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, timezone_id="Asia/Kolkata")
                page = context.new_page()
                errors, urls = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: urls.append(request.url))
                page.emulate_media(color_scheme="dark")
                page.goto(url + "/#setup=" + token)
                expect(page.get_by_role("heading", name="Set up ShinBot")).to_be_visible()
                assert page.get_by_label("Timezone", exact=True).input_value() in {"Asia/Kolkata", "Asia/Calcutta"}
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                expect(page.get_by_label("Appearance", exact=True)).to_have_value("system")
                page.emulate_media(color_scheme="light")
                expect(page.locator("html")).to_have_attribute("data-theme", "light")
                page.get_by_label("Appearance", exact=True).select_option("dark")
                page.emulate_media(color_scheme="dark")
                page.emulate_media(color_scheme="light")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                # Reloading restores the explicit choice before the app renders.
                page.reload()
                expect(page.get_by_label("Appearance", exact=True)).to_have_value("dark")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                peer = page.context.new_page()
                peer.emulate_media(color_scheme="light")
                peer.goto(url)
                expect(peer.get_by_label("Appearance", exact=True)).to_have_value("dark")
                page.get_by_label("Appearance", exact=True).select_option("light")
                expect(peer.locator("html")).to_have_attribute("data-theme", "light")
                peer.close()
                blocked_context = browser.new_context(color_scheme="light")
                blocked_context.add_init_script(
                    "Object.defineProperty(window, 'localStorage', {get() {"
                    "throw new DOMException('Storage blocked', 'SecurityError'); }});"
                )
                blocked = blocked_context.new_page()
                blocked.on("pageerror", lambda error: errors.append(str(error)))
                blocked.goto(url)
                blocked.get_by_label("Appearance", exact=True).select_option("dark")
                expect(blocked.locator("html")).to_have_attribute("data-theme", "dark")
                expect(blocked.locator("#toast")).to_contain_text("Browser storage is unavailable")
                blocked_context.close()
                # Restore the bootstrap link after reloading deliberately cleared it.
                page.goto("about:blank")
                page.goto(url + "/#setup=" + token)
                expect(page.get_by_label("Installation token")).to_have_count(0)
                expect(
                    page.get_by_role("heading", name="Research & writing team", exact=True)
                ).to_be_visible()
                assert not page.evaluate("location.hash") and all(token not in url for url in urls)
                page.screenshot(path=str(OUT / "first-run.png"), full_page=True)
                page.locator(".template-card").filter(
                    has=page.get_by_role("heading", name="Research & writing team", exact=True)
                ).get_by_role("button").click()
                page.get_by_role("dialog", name="Research & writing team").get_by_role(
                    "button", name="Start with this template"
                ).click()
                expect(page.locator("#pending-template")).to_contain_text("Research & writing team")
                page.get_by_label("Choose a password", exact=True).fill(secrets.token_urlsafe(24))
                page.get_by_role("button", name="Show password", exact=True).click()
                expect(page.get_by_label("Choose a password", exact=True)).to_have_attribute("type", "text")
                page.get_by_role("button", name="Hide password", exact=True).click()
                expect(page.get_by_label("Choose a password", exact=True)).to_have_attribute("type", "password")
                # Wait for the real template installation, which includes its security scan.
                with page.expect_response(
                    lambda response: response.url.endswith("/api/templates/install")
                    and response.request.method == "POST", timeout=30000
                ) as installed_template:
                    page.get_by_role("button", name="Create workspace", exact=True).click()
                assert installed_template.value.ok
                expect(page.get_by_role("heading", name="Your bots")).to_be_visible()
                expect(page.get_by_role("heading", name="Connect your model.")).to_be_visible()
                defaults=page.request.get(url+"/api/settings").json()
                assert defaults["timezone"] in {"Asia/Kolkata", "Asia/Calcutta"}
                assert defaults["learn_preferences"] and defaults["task_tokens"] == defaults["daily_tokens"] == 0
                assert defaults["provider"] == defaults["model"] == ""
                assert page.request.get(url+"/api/plugins").json()["installed"] == []
                for name in ("Researcher", "Writing partner"):
                    card = page.locator(".bot-card").filter(
                        has=page.get_by_role("heading", name=name, exact=True)
                    )
                    expect(card).to_be_visible()
                    expect(card).to_contain_text("This group")
                # Defaults are assigned by the gateway; a saved colour follows the
                # bot across its card, sidebar, chat and a fresh browser session.
                colors_out = Path("tmp/reference/bot-colors")
                colors_out.mkdir(parents=True, exist_ok=True)
                initial_bots = page.request.get(url + "/api/bots").json()["bots"]
                initial_colors = {b["id"]: b["profile"]["color"] for b in initial_bots}
                assert len(set(initial_colors.values())) == len(initial_bots)
                page.get_by_role("button", name="Create bot", exact=True).click()
                bot_dialog = page.get_by_role("dialog", name="Create a bot", exact=True)
                expect(bot_dialog.get_by_role("radio", name="Automatic", exact=True)).to_be_checked()
                bot_dialog.get_by_label("Bot name", exact=True).fill("Calendar helper")
                with page.expect_response(
                    lambda response: response.url.endswith("/api/bots") and response.request.method == "POST"
                ) as created_bot:
                    bot_dialog.get_by_role("button", name="Save profile", exact=True).click()
                added = created_bot.value.json()
                assert added["profile"]["color"] not in initial_colors.values()
                expect(page.get_by_role("heading", name="Calendar helper", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="Your bots")).to_be_visible()
                page.get_by_role("button", name="Edit Calendar helper profile", exact=True).click()
                bot_dialog = page.get_by_role("dialog", name="Edit bot profile", exact=True)
                violet = bot_dialog.get_by_role("radio", name="Violet", exact=True)
                violet.focus()
                violet.press("Space")
                violet.press("ArrowRight")
                expect(bot_dialog.get_by_role("radio", name="Slate", exact=True)).to_be_checked()
                expect(bot_dialog.locator("#bot-color-label")).to_have_text("Slate")
                bot_dialog.get_by_role("radio", name="Violet", exact=True).press("Space")
                page.screenshot(path=str(colors_out / "picker-light.png"), full_page=True)
                bot_dialog.get_by_role("button", name="Save profile", exact=True).click()
                expect(bot_dialog).not_to_be_visible()
                expect(page.get_by_role("heading", name="Your bots")).to_be_visible()
                roster_avatar = page.locator(f'[data-sidebar-bot="{added["id"]}"] .mascot')
                card = page.locator(".bot-card").filter(has=page.get_by_role("heading", name="Calendar helper", exact=True))
                expect(card.locator(".mascot")).to_have_attribute("data-bot-color", "violet")
                expect(roster_avatar).to_have_attribute("data-bot-color", "violet")
                expect(page.locator("#header-profile .mascot")).to_have_attribute("data-bot-color", "violet")
                page.screenshot(path=str(colors_out / "home-light.png"), full_page=True)
                card.get_by_role("button", name="Open chat", exact=False).click()
                expect(page.locator(".chat-emblem")).to_have_attribute("data-bot-color", "violet")
                page.reload()
                page.locator(f'[data-sidebar-bot="{added["id"]}"]').click()
                expect(page.locator("#header-profile .mascot")).to_have_attribute("data-bot-color", "violet")
                # A separate authenticated device receives the same saved profile.
                saved_bots = page.request.get(url + "/api/bots").json()["bots"]
                assert {b["id"]: b["profile"]["color"] for b in saved_bots if b["id"] in initial_colors} == initial_colors
                peer_context = browser.new_context(storage_state=context.storage_state())
                peer = peer_context.new_page()
                peer.goto(url)
                expect(peer.locator(f'[data-sidebar-bot="{added["id"]}"] .mascot')).to_have_attribute("data-bot-color", "violet")
                peer_context.close()
                page.locator('[data-view="settings"]').click()
                page.get_by_role("tab", name="Appearance", exact=True).click()
                page.get_by_role("radio", name="System", exact=False).check()
                page.emulate_media(color_scheme="dark")
                page.locator('[data-view="home"]').click()
                expect(page.get_by_role("heading", name="Your bots")).to_be_visible()
                page.screenshot(path=str(colors_out / "home-dark.png"), full_page=True)
                page.get_by_role("button", name="Edit Calendar helper profile", exact=True).click()
                expect(page.get_by_role("radio", name="Violet", exact=True)).to_be_checked()
                # Wait for the Lucide mask to load before capturing the selected swatch.
                page.evaluate("""async () => {
                    const image = new Image();
                    image.src = '/static/vendor/lucide/check.svg';
                    await image.decode();
                }""")
                page.screenshot(path=str(colors_out / "picker-dark.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.locator("dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
                page.screenshot(path=str(colors_out / "picker-mobile-dark.png"), full_page=True)
                page.get_by_role("button", name="Close dialog", exact=True).click()
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.emulate_media(color_scheme="light")
                print("PASS random bot colours, keyboard selection, persistence and both themes", flush=True)
                page.locator('[data-view="plugins"]').click()
                expect(page.get_by_role("heading", name="Plugins", exact=True)).to_be_visible()
                expect(page.locator('[data-install="telegram"]')).to_be_visible()
                # GitHub has its own account picker; no real account is used in this test.
                page.locator('[data-install="github"]').click()
                setup_dialog = page.get_by_role("dialog", name="Set up GitHub", exact=True)
                setup_dialog.get_by_role("button", name="Connect account", exact=True).click()
                github_dialog = page.get_by_role("dialog", name="Connect GitHub", exact=True)
                expect(github_dialog.get_by_role("button", name="Connect with GitHub CLI")).to_be_visible()
                github_dialog.get_by_text("Use a personal access token instead", exact=True).click()
                expect(github_dialog.get_by_label("GitHub token", exact=True)).to_have_attribute("type", "password")
                github_dialog.get_by_role("button", name="Close dialog", exact=True).click()
                setup_dialog.get_by_role("button", name="Done for now", exact=True).click()
                # A catalog plugin exercises the shared encrypted-settings form.
                page.locator('[data-install="serpapi"]').click()
                setup_dialog = page.get_by_role("dialog", name="Set up SerpApi", exact=True)
                setup_dialog.get_by_role("button", name="Connect account", exact=True).click()
                plugin_dialog = page.get_by_role("dialog", name="Configure SerpApi", exact=True)
                expect(plugin_dialog.get_by_text("Source: adarshdigievo/shinbot-official-plugins", exact=True)).to_be_visible()
                expect(plugin_dialog.get_by_label("SerpApi API key", exact=True)).to_have_attribute("type", "password")
                plugin_dialog.get_by_label("SerpApi API key", exact=True).fill("fixture-serpapi-key")
                plugin_dialog.get_by_role("button", name="Save settings", exact=True).click()
                expect(plugin_dialog).not_to_be_visible()
                setup_dialog.get_by_role("button", name="Done for now", exact=True).click()
                page.locator('[data-configure^="serpapi@"]').click()
                expect(plugin_dialog.get_by_label("SerpApi API key", exact=True)).to_have_value("")
                expect(plugin_dialog.get_by_text("A value is saved.", exact=False)).to_be_visible()
                plugin_dialog.get_by_role("button", name="Close dialog", exact=True).click()
                plugin_out = Path("tmp/reference/plugin-review")
                plugin_out.mkdir(parents=True, exist_ok=True)
                with page.expect_response(
                    lambda response: response.url.endswith("/api/plugins/stage")
                    and response.request.method == "POST", timeout=30000
                ) as staged_telegram:
                    page.locator('[data-install="telegram"]').click()
                assert staged_telegram.value.ok
                # Telegram is connected in the app like any other plugin; nothing runs on the host.
                telegram_dialog = page.get_by_role("dialog", name="Set up Telegram", exact=True)
                expect(telegram_dialog.get_by_role("button", name="Connect account", exact=True)).to_be_visible()
                expect(page.get_by_text("shinbot telegram-setup")).to_have_count(0)
                page.screenshot(path=str(plugin_out / "plugins-light.png"), full_page=True)
                page.get_by_role("button", name="Close dialog", exact=True).click()
                page.emulate_media(color_scheme="dark")
                page.locator('[data-configure^="serpapi@"]').click()
                expect(plugin_dialog).to_be_visible()
                page.screenshot(path=str(plugin_out / "plugin-settings-dark.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert plugin_dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
                page.screenshot(path=str(plugin_out / "plugin-settings-mobile.png"), full_page=True)
                plugin_dialog.get_by_label("Remove saved SerpApi API key", exact=True).check()
                plugin_dialog.get_by_role("button", name="Save settings", exact=True).click()
                expect(plugin_dialog).not_to_be_visible()
                page.locator('[data-configure^="serpapi@"]').click()
                expect(plugin_dialog.get_by_label("SerpApi API key", exact=True)).to_have_attribute("required", "")
                plugin_dialog.get_by_role("button", name="Close dialog", exact=True).click()
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.emulate_media(color_scheme="light")
                print("PASS bundled plugin setup, masked credential persistence and removal", flush=True)
                extension_out = Path('tmp/reference/extensions/screenshots')
                extension_out.mkdir(parents=True, exist_ok=True)
                page.locator('.plugin-tools-menu > summary').click()
                page.get_by_role('button', name='Connect MCP server', exact=True).click()
                mcp_dialog = page.get_by_role('dialog', name='Connect an MCP server', exact=True)
                mcp_dialog.get_by_label('Name', exact=True).fill('Research tools')
                mcp_dialog.get_by_label('Connection ID', exact=True).fill('research-tools')
                mcp_dialog.get_by_label('Arguments (JSON array)', exact=True).fill('invalid-json')
                mcp_dialog.get_by_role('button', name='Connect and inspect tools').click()
                expect(mcp_dialog.get_by_role('alert')).to_contain_text('Use valid JSON')
                mcp_dialog.get_by_label('Transport', exact=True).select_option('http')
                mcp_dialog.get_by_label('Server URL', exact=True).fill('https://example.com/mcp')
                expect(mcp_dialog.get_by_label('Headers (JSON object)', exact=True)).to_have_attribute('type', 'password')
                mcp_dialog.evaluate('el => el.scrollTop = 0')
                page.screenshot(path=str(extension_out / 'mcp-setup-light.png'), full_page=True)
                mcp_dialog.get_by_role('button', name='Close dialog', exact=True).click()
                serpapi_card = page.locator('[data-plugin-card^="serpapi@"]')
                serpapi_card.get_by_text('Details and permissions', exact=True).click()
                serpapi_card.locator('[data-plugin-access]').click()
                access_dialog = page.get_by_role('dialog', name='Bot access for SerpApi', exact=True)
                access_dialog.get_by_role('radio', name='Selected bots', exact=True).check()
                access_dialog.get_by_role('checkbox', name='Calendar helper', exact=True).check()
                access_dialog.get_by_role('button', name='Save access', exact=True).click()
                expect(access_dialog).not_to_be_visible()
                expect(serpapi_card).to_contain_text('1 selected bot')
                page.reload()
                page.locator('[data-view="plugins"]').click()
                expect(serpapi_card).to_contain_text('1 selected bot')
                def install_skill_fixture(route):
                    body = route.request.post_data_json
                    assert body['command'] == 'npx skills add example/skills --skill helpful-notes'
                    response = page.request.post(url + '/api/plugins/skills/import', headers={'Origin': url, 'X-Shinbot-CSRF': '1'}, data={
                        'files': {'SKILL.md': '---\nname: helpful-notes\ndescription: Write useful notes.\n---\nWrite clearly.', 'LICENSE': (ROOT / 'LICENSE').read_text()},
                        'license': body['license'], 'access': body['access'],
                    })
                    assert response.ok, response.text()
                    route.fulfill(json=response.json())
                page.route('**/api/plugins/skills/install', install_skill_fixture)
                page.locator('.plugin-tools-menu > summary').click()
                page.get_by_role('button', name='Install skills', exact=True).click()
                skill_dialog = page.get_by_role('dialog', name='Install skills', exact=True)
                skill_dialog.get_by_label('Skills command', exact=True).fill('npx skills add example/skills --skill helpful-notes')
                skill_dialog.get_by_label('Upstream license', exact=True).select_option('MIT')
                skill_dialog.get_by_role('radio', name='All bots', exact=True).check()
                page.emulate_media(color_scheme='dark')
                page.screenshot(path=str(extension_out / 'skills-setup-dark.png'), full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert skill_dialog.evaluate('el => el.scrollWidth <= el.clientWidth')
                page.screenshot(path=str(extension_out / 'skills-setup-mobile.png'), full_page=True)
                with page.expect_response("**/api/plugins/skills/install", timeout=30000) as installed_skill:
                    skill_dialog.get_by_role('button', name='Install and review', exact=True).click()
                assert installed_skill.value.ok
                expect(skill_dialog).not_to_be_visible()
                expect(page.get_by_role('heading', name='helpful-notes', exact=False)).to_be_visible()
                page.unroute('**/api/plugins/skills/install', install_skill_fixture)
                page.set_viewport_size({'width':1440,'height':1000})
                page.emulate_media(color_scheme='light')
                page.screenshot(path=str(extension_out / 'plugins-overview.png'), full_page=True)
                print('PASS MCP setup validation, persisted bot access and skill installation UI', flush=True)
                # A model outage must leave owner controls available. Settings still
                # saves ordinary preferences without erasing the disabled model fields.
                before = page.request.get(url + "/api/settings").json()
                page.locator('[data-view="settings"]').click()
                expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
                expect(
                    page.get_by_role("status").filter(has_text="Model connections are unavailable")
                ).to_be_visible()
                expect(page.get_by_role("button", name="Retry model connection")).to_be_visible()
                expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_enabled()
                page.get_by_role("tab", name="Accounts", exact=True).click()
                expect(page.get_by_role("button", name="Sign out all devices", exact=True)).to_be_enabled()
                page.get_by_role("tab", name="Workspace", exact=True).click()
                page.get_by_label("Workspace name", exact=True).fill("My test workspace")
                page.get_by_role("tab", name="Appearance", exact=True).click()
                page.get_by_role("radio", name="Dark", exact=False).check()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                expect(page.get_by_role("button", name="Save settings", exact=True)).not_to_be_visible()
                page.get_by_role("radio", name="System", exact=False).check()
                page.emulate_media(color_scheme="dark")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                expect(page.get_by_role("radio", name="System", exact=False)).to_be_checked()
                page.emulate_media(color_scheme="light")
                expect(page.locator("html")).to_have_attribute("data-theme", "light")
                page.get_by_role("tab", name="Workspace", exact=True).click()
                expect(page.get_by_label("Workspace name", exact=True)).to_have_value("My test workspace")
                with page.expect_response(
                    lambda response: (
                        response.url.endswith("/api/settings") and response.request.method == "PUT"
                    )
                ) as saved:
                    page.get_by_role("button", name="Save settings", exact=True).click()
                assert saved.value.ok
                settings = saved.value.json()
                assert settings["name"] == "My test workspace"
                expect(page.locator("#workspace-name")).to_have_text("My test workspace")
                expect(page).to_have_title("My test workspace · ShinBot")
                assert (settings["provider"], settings["model"]) == (before["provider"], before["model"])
                page.locator('[data-view="computer"]').click()
                expect(page.get_by_role("heading", name="Browser unavailable", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Try again", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Take control", exact=False)).to_have_count(0)
                pending = []
                page.route("**/api/browser/activity", lambda route: route.fulfill(json={
                    "lease": None, "browser_id": "shared", "url": "", "headless": True, "idle": True
                }))
                page.route("**/api/browser/select", lambda route: pending.append(route))
                with page.expect_request("**/api/browser/select"):
                    page.get_by_role("button", name="Try again", exact=True).click()
                page.locator('[data-view="notebook"]').click()
                expect(page.get_by_role("heading", name="Notebook", exact=True)).to_be_visible()
                assert len(pending) == 1
                with page.expect_response("**/api/browser/select") as stale:
                    pending[0].fulfill(status=503, json={"detail": "Browser service unavailable"})
                stale.value.body()
                # Wait for the paint after delivery, so a stale async render has
                # had a chance to overwrite the current page before the assertion.
                page.evaluate(
                    "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
                expect(page.get_by_role("heading", name="Notebook", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="Browser unavailable", exact=True)).to_have_count(0)
                page.unroute("**/api/browser/select")
                page.unroute("**/api/browser/activity")
                page.locator('[data-view="home"]').click()
                expect(page.get_by_role("heading", name="Your bots", exact=True)).to_be_visible()
                print(
                    "PASS first-run template selection survives signup; unavailable services preserve settings and show useful browser recovery",
                    flush=True,
                )
                runtime_providers = [
                    {
                        "id": "test",
                        "name": "Test model",
                        "configured": False,
                        "oauth": True,
                        "models": [{"id": "test", "name": "Test model"}],
                    },
                    {
                        "id": "other",
                        "name": "Unrelated provider",
                        "configured": True,
                        "models": [{"id": "other", "name": "Other model"}],
                    },
                ]

                def save_key(route):
                    assert json.loads(route.request.post_data)["key"] == "fixture-key"
                    runtime_providers[0]["configured"] = True
                    route.fulfill(json={"ok": True})

                page.route("**/api/providers/key", save_key)
                page.reload()
                expect(page.get_by_role("button", name="Choose a model")).to_be_visible()
                posts = []
                page.on(
                    "request",
                    lambda request: (
                        posts.append(request.url)
                        if request.method == "POST" and request.url.endswith("/api/tasks")
                        else None
                    ),
                )
                page.get_by_role("navigation", name="Main navigation").get_by_role(
                    "button", name="Chat", exact=True
                ).click()
                draft = "Keep this draft while I connect the selected provider"
                page.get_by_role("textbox", name="Message ShinBot", exact=True).fill(draft)
                page.get_by_role("button", name="Send", exact=True).click()
                expect(page.locator("#toast")).to_contain_text("Your message is still here")
                expect(page.get_by_role("textbox", name="Message ShinBot", exact=True)).to_have_value(draft)
                assert posts == [], "An unrelated connected provider must not admit a task"
                page.locator('[data-view="home"]').click()
                expect(page.get_by_role("heading", name="Your bots", exact=True)).to_be_visible()
                page.get_by_role("button", name="Choose a model").click()
                page.locator("#first-provider").select_option("test")
                page.locator("#first-model-id").select_option("test")
                page.get_by_text("Use an API key instead", exact=True).click()
                page.get_by_label("API key", exact=True).fill("fixture-key")
                page.get_by_role("button", name="Use this model").click()
                expect(page.get_by_role("button", name="Help me get started")).to_be_visible()
                # A draft belongs to its bot, and moving through the desktop shell must not lose it.
                page.locator('[data-sidebar-bot]').filter(has_text="Researcher").click()
                page.get_by_role("textbox", name="Message Researcher", exact=True).fill("Draft for my research bot")
                page.locator('[data-sidebar-bot="default"]').click()
                expect(page.get_by_role("textbox", name="Message ShinBot", exact=True)).to_have_value(draft)
                page.locator('[data-view="notebook"]').click()
                page.locator('[data-view="chat"]').click()
                expect(page.get_by_role("textbox", name="Message ShinBot", exact=True)).to_have_value(draft)
                page.keyboard.press("ControlOrMeta+k")
                search = page.get_by_role("dialog", name="Search chats", exact=True)
                search.get_by_role("searchbox", name="Search chats", exact=True).fill("No matching fixture chat")
                expect(search).to_contain_text("No matching chats")
                search.get_by_role("searchbox").press("Escape")
                expect(search).to_have_count(0)
                expect(page.get_by_role("textbox", name="Message ShinBot", exact=True)).to_have_value(draft)
                page.locator('[data-sidebar-bot]').filter(has_text="Researcher").click()
                expect(page.get_by_role("textbox", name="Message Researcher", exact=True)).to_have_value("Draft for my research bot")
                page.locator('[data-view="settings"]').click()
                page.get_by_role("tab", name="Workspace", exact=True).click()
                workspace_name = page.get_by_label("Workspace name", exact=True)
                original_name = workspace_name.input_value()
                workspace_name.fill("")
                page.get_by_role("tab", name="Permissions", exact=True).click()
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.get_by_role("tab", name="Workspace", exact=True)).to_have_attribute("aria-selected", "true")
                expect(workspace_name).to_be_focused()
                workspace_name.fill(original_name)
                page.get_by_role("tab", name="Permissions", exact=True).click()
                page.get_by_role("tab", name="Permissions", exact=True).press("ArrowLeft")
                expect(page.get_by_role("tab", name="Browser", exact=True)).to_be_focused()
                page.get_by_role("tab", name="Workspace", exact=True).click()
                expect(workspace_name).to_have_value(original_name)
                print("PASS per-bot drafts, keyboard search, settings tab state and hidden-field validation", flush=True)

                # Delay real gateway responses to reproduce navigation races without script injection.
                fixture_tasks = []
                for title in ("Draft race A", "Draft race B"):
                    created = page.request.post(
                        url + "/api/tasks",
                        headers={"Origin": url, "X-Shinbot-CSRF": "1"},
                        data={"content": title, "request_id": secrets.token_hex(16), "bot_id": "default"},
                    )
                    assert created.ok
                    fixture_tasks.append(created.json()["id"])
                with sqlite3.connect(Path(directory) / "shinbot.sqlite") as db:
                    for task_id in fixture_tasks:
                        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                        db.execute("UPDATE messages SET status='completed',answer='Fixture reply' WHERE task_id=?", (task_id,))
                task_a, task_b = fixture_tasks
                page.locator('[data-sidebar-bot="default"]').click()
                page.locator('[data-task="' + task_a + '"]').click()
                expect(page.locator("#task-title")).to_have_text("Draft race A")
                expect(page.locator(".reply-avatar").first).to_have_attribute("data-bot-color", "blue")
                # Compaction is optional and disclosed inside a keyboard-accessible menu.
                compact = page.get_by_role("button", name="Compact now", exact=True)
                expect(compact).not_to_be_visible()
                menu = page.get_by_label("Conversation options", exact=True)
                menu.focus()
                menu.press("Enter")
                expect(compact).to_be_visible()
                expect(page.locator(".conversation-actions")).to_contain_text("automatically")
                menu.press("Escape")
                expect(compact).not_to_be_visible()
                expect(menu).to_be_focused()
                menu.click()
                page.locator("#prompt").click()
                expect(compact).not_to_be_visible()
                menu.click()
                page.route("**/api/tasks/" + task_a + "/compact", lambda route: route.fulfill(json={"taskId": 1}))
                with page.expect_request("**/api/tasks/" + task_a + "/compact"):
                    compact.click()
                expect(compact).not_to_be_visible()
                expect(page.locator("#toast")).to_contain_text("Summarizing earlier messages")
                page.unroute("**/api/tasks/" + task_a + "/compact")
                compacting_task = page.request.get(url + "/api/tasks/" + task_a).json()
                compacting_task["progress"] = {"compacting": True}
                page.route("**/api/tasks/" + task_a, lambda route: route.fulfill(json=compacting_task))
                expect(page.locator("#context-status")).to_be_visible()
                menu.click()
                expect(compact).to_be_disabled()
                expect(page.locator("#compact-help")).to_have_text("Context is being summarized.")
                menu.press("Escape")
                page.unroute("**/api/tasks/" + task_a)
                expect(page.locator("#context-status")).not_to_be_visible()
                page.locator("#prompt").fill("Keep draft A")
                delayed = []
                page.route("**/api/tasks/" + task_b, lambda route: delayed.append(route))
                with page.expect_request("**/api/tasks/" + task_b):
                    page.locator('[data-task="' + task_b + '"]').click()
                page.locator("#new-task").click()
                delayed.pop().continue_()
                page.unroute("**/api/tasks/" + task_b)
                page.locator('[data-task="' + task_a + '"]').click()
                expect(page.locator("#prompt")).to_have_value("Keep draft A")
                # A delayed send belongs to A even if B is selected before it completes.
                pending_send = []
                page.route("**/api/tasks/" + task_a + "/messages", lambda route: pending_send.append(route))
                with page.expect_request("**/api/tasks/" + task_a + "/messages"):
                    page.get_by_role("button", name="Send", exact=True).click()
                page.locator('[data-task="' + task_b + '"]').click()
                expect(page.locator("#task-title")).to_have_text("Draft race B")
                page.locator("#prompt").fill("Keep draft B")
                with page.expect_response("**/api/tasks/" + task_a + "/messages"):
                    pending_send.pop().continue_()
                expect(page.locator("#toast")).to_contain_text("Message sent")
                expect(page.locator("#task-title")).to_have_text("Draft race B")
                expect(page.locator("#prompt")).to_have_value("Keep draft B")
                page.unroute("**/api/tasks/" + task_a + "/messages")
                # A real snapshot changing the busy state must preserve keyboard focus.
                roster_button = page.locator('[data-sidebar-bot="default"]')
                expect(roster_button).to_contain_text("Task in progress")
                roster_button.focus()
                with sqlite3.connect(Path(directory) / "shinbot.sqlite") as db:
                    db.execute("UPDATE tasks SET status='completed',updated=? WHERE id=?", (time.time(), task_a))
                expect(roster_button).not_to_contain_text("Task in progress")
                expect(roster_button).to_be_focused()
                # An unchanged roster must keep its DOM nodes, not merely restore focus after replacing them.
                roster_node = roster_button.element_handle()
                page.locator('[data-view="jobs"]').click()
                page.locator('[data-view="home"]').click()
                assert roster_node.evaluate("node => node.isConnected")
                # Returning to A before its send completes must clear the sent draft in the new composer.
                page.locator('[data-task="' + task_a + '"]').click()
                page.locator("#prompt").fill("Send while I return")
                page.route("**/api/tasks/" + task_a + "/messages", lambda route: pending_send.append(route))
                with page.expect_request("**/api/tasks/" + task_a + "/messages"):
                    page.get_by_role("button", name="Send", exact=True).click()
                page.locator('[data-task="' + task_b + '"]').click()
                expect(page.locator("#task-title")).to_have_text("Draft race B")
                page.locator('[data-task="' + task_a + '"]').click()
                expect(page.locator("#task-title")).to_have_text("Draft race A")
                expect(page.locator("#prompt")).to_have_value("Send while I return")
                with page.expect_response("**/api/tasks/" + task_a + "/messages"):
                    pending_send.pop().continue_()
                expect(page.locator("#prompt")).to_have_value("")
                page.unroute("**/api/tasks/" + task_a + "/messages")
                page.locator('[data-task="' + task_b + '"]').click()
                expect(page.locator("#prompt")).to_have_value("Keep draft B")
                page.locator('[data-task="' + task_a + '"]').click()
                expect(page.locator("#prompt")).to_have_value("")
                # The same return path for a new conversation must open the created task once.
                page.locator("#new-task").click()
                page.locator("#prompt").fill("Start while I return")
                pending_create = []

                def hold_create(route):
                    if route.request.method == "POST":
                        pending_create.append(route)
                    else:
                        route.continue_()

                page.route("**/api/tasks", hold_create)
                with page.expect_request(lambda request: request.url.endswith("/api/tasks") and request.method == "POST"):
                    page.get_by_role("button", name="Send", exact=True).click()
                page.locator('[data-view="home"]').click()
                page.locator("#new-task").click()
                expect(page.locator("#prompt")).to_have_value("Start while I return")
                with page.expect_response(lambda response: response.url.endswith("/api/tasks") and response.request.method == "POST"):
                    pending_create.pop().continue_()
                expect(page.locator("#task-title")).to_have_text("Start while I return")
                expect(page.locator("#prompt")).to_have_value("")
                page.unroute("**/api/tasks", hold_create)
                print("PASS delayed loads, sends and re-entry retain the right drafts; unchanged snapshots preserve roster nodes", flush=True)

                page.locator('[data-view="jobs"]').click()
                page.get_by_role("button", name="New job", exact=True).click()
                job_dialog = page.get_by_role("dialog", name="Create a job")
                job_dialog.get_by_label("Name", exact=True).fill("Invalid schedule")
                job_dialog.get_by_label("What should the bot do?", exact=True).fill("Send a reminder")
                job_dialog.locator('[name="mode"]').select_option("custom")
                job_dialog.get_by_label("Cron expression", exact=True).fill("not a cron expression")
                job_dialog.get_by_role("button", name="Save job", exact=True).click()
                expect(job_dialog.get_by_role("alert")).to_contain_text("cron")
                job_dialog.locator('[name="mode"]').select_option("interval")
                expect(job_dialog.get_by_label("Every", exact=True)).to_have_attribute("min", "1")
                job_dialog.locator('[name="unit"]').select_option("60")
                expect(job_dialog.locator('[data-preview]')).to_contain_text("Next run:")
                job_dialog.locator('[name="mode"]').select_option("once")
                expect(job_dialog.get_by_label("Date and time", exact=True)).to_have_attribute("type", "datetime-local")
                job_dialog.get_by_role("button", name="Close dialog", exact=True).click()
                # A form with unsaved input asks before it closes.
                page.get_by_role("button", name="Discard changes", exact=True).click()
                expect(job_dialog).to_have_count(0)
                page.locator('[data-view="settings"]').click()
                login_posts = []

                def login_reply(route):
                    if route.request.method == "POST":
                        login_posts.append(route.request.post_data)
                    route.fulfill(
                        json={
                            "id": "ui-login",
                            "provider": "test",
                            "status": "pending",
                            "events": [
                                {
                                    "type": "auth_url",
                                    "url": "https://example.com/auth",
                                    "instructions": "Paste the callback URL here.",
                                },
                                {
                                    "type": "info",
                                    "links": [{"url": "https://example.com/help", "label": "Provider help"}],
                                },
                            ],
                            "prompt": {"type": "manual_code", "message": "Callback URL"},
                        }
                    )

                page.route("**/api/login", login_reply)
                page.route("**/api/login/respond", lambda route: route.fulfill(json={"ok": True}))
                page.get_by_role("tab", name="Connections", exact=True).click()
                page.get_by_role("button", name="Sign in with Test model", exact=True).click()
                auth_dialog = page.get_by_role("dialog", name="Connect your account", exact=True)
                expect(auth_dialog.get_by_label("Callback URL", exact=True)).not_to_be_visible()
                auth_dialog.get_by_text("Having trouble returning from sign-in?", exact=True).click()
                expect(auth_dialog.get_by_label("Callback URL", exact=True)).to_be_visible()
                expect(auth_dialog.get_by_role("link", name="Provider help", exact=True)).to_have_attribute(
                    "href", "https://example.com/help"
                )
                assert login_posts == [], "A pending sign-in should resume without starting another attempt"
                auth_dialog.get_by_role("button", name="Close dialog", exact=True).click()

                # Exercise real owner approval endpoints with pending actions in an isolated conversation.
                permissions_out = ROOT / "tmp/reference/security-defaults/screenshots"
                permissions_out.mkdir(parents=True, exist_ok=True)
                reply = page.request.post(url + "/api/tasks", headers={"Origin": url, "X-Shinbot-CSRF": "1"}, data={"content": "Review approval choices", "request_id": secrets.token_hex(16), "bot_id": "default"})
                assert reply.ok, reply.text()
                permission_task = reply.json()["id"]
                with sqlite3.connect(Path(directory) / "shinbot.sqlite") as db:
                    for scope in ("once", "chat", "always"):
                        db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("review-"+scope, permission_task, "fixture", "run_command", json.dumps({"command":"echo "+scope}), scope, "pending", None, None, time.time(), time.time()+300))
                    db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (permission_task,))
                page.reload()
                page.locator('[data-sidebar-bot="default"]').click()
                page.locator('[data-task="'+permission_task+'"]').click()
                expect(page.get_by_role("button", name="Always allow", exact=True)).to_have_count(3)
                page.screenshot(path=str(permissions_out / "chat-approvals-light.png"), full_page=True)
                for scope,label in (("once","Allow once"),("chat","Allow for this chat"),("always","Always allow")):
                    # Wait for each decision to be stored; the rules are read right after the loop.
                    with page.expect_response(lambda r, s=scope: r.url.endswith("/api/actions/review-"+s+"/decision")) as decided:
                        page.locator('[data-entry="action-review-'+scope+'"]').get_by_role("button", name=label, exact=True).click()
                    assert decided.value.ok
                rules=page.request.get(url+"/api/approvals").json()
                assert sorted(rule["scope"] for rule in rules)==["always","chat"]
                page.locator('[data-view="settings"]').click()
                page.get_by_role("tab", name="Permissions", exact=True).click()
                expect(page.get_by_text("Saved approvals", exact=True)).to_be_visible()
                expect(page.locator('[name="learn_preferences"]')).to_be_checked()
                page.screenshot(path=str(permissions_out / "permissions-light.png"), full_page=True)
                page.locator('[name="learn_preferences"]').uncheck()
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.locator('#toast')).to_contain_text('Settings saved')
                assert not page.request.get(url+"/api/settings").json()["learn_preferences"]
                page.locator('[data-revoke-approval]').first.click()
                expect(page.locator('[data-revoke-approval]')).to_have_count(1)
                page.get_by_role("tab", name="Workspace", exact=True).click()
                expect(page.locator('[name="task_tokens"]')).to_have_value('')
                expect(page.locator('[name="daily_tokens"]')).to_have_value('')
                page.get_by_label("Timezone", exact=True).fill("America/New_York")
                page.get_by_role("button", name="Save settings", exact=True).click()
                expect(page.locator('#toast')).to_contain_text('Settings saved')
                page.reload()
                page.locator('[data-view="settings"]').click()
                page.get_by_role("tab", name="Workspace", exact=True).click()
                expect(page.get_by_label("Timezone", exact=True)).to_have_value("America/New_York")
                page.screenshot(path=str(permissions_out / "workspace-light.png"), full_page=True)
                page.get_by_role("tab", name="Appearance", exact=True).click()
                page.get_by_role("radio", name="Dark", exact=False).check()
                page.get_by_role("tab", name="Permissions", exact=True).click()
                page.screenshot(path=str(permissions_out / "permissions-dark.png"), full_page=True)
                page.locator('[data-view="plugins"]').click()
                serpapi_card.get_by_text('Details and permissions', exact=True).click()
                serpapi_card.locator('[data-review]').click()
                expect(page.get_by_role("dialog").get_by_text("Scan completed", exact=True)).to_be_visible()
                page.screenshot(path=str(permissions_out / "plugin-scan-dark.png"), full_page=True)
                page.get_by_role("button", name="Close dialog", exact=True).click()
                page.locator('[data-view="chat"]').click()
                print("PASS real chat approval scopes, revocation, memory opt-out, timezone persistence and scanner reports", flush=True)

                polls = []

                def task_reply(route):
                    response = route.fetch()
                    task = response.json()
                    polls.append(task["id"])
                    task["messages"][-1]["status"] = "running"
                    task.update(
                        status="running",
                        progress={"text": f"Streaming segment {len(polls)}"},
                        actions=[
                            {
                                "id": "pending-ui",
                                "tool": "computer",
                                "args": "{}",
                                "status": "pending",
                                "expires": time.time() + 300,
                            },
                            {
                                "id": "completed-ui",
                                "tool": "notes",
                                "args": "{}",
                                "result": "Saved",
                                "status": "completed",
                            },
                        ],
                    )
                    route.fulfill(response=response, json=task)

                # Only presentation is stubbed; the conversation is created in the real gateway.
                page.route("**/api/tasks/*", task_reply)
                page.locator("#new-task").click()
                page.get_by_role("textbox", name="Message ShinBot", exact=True).fill("Streaming UI fixture")
                page.get_by_role("button", name="Send", exact=True).click()
                approval = page.get_by_role("button", name="Allow once", exact=True)
                expect(approval).to_be_visible()
                page.locator(".tool-item summary").click()
                progress = page.locator('[data-entry="progress"]')
                before_poll = progress.inner_text()
                approval.focus()
                expect(progress).not_to_have_text(before_poll)
                expect(approval).to_be_focused()
                assert page.locator(".tool-item").get_attribute("open") is not None
                assert page.locator("#messages").get_attribute("aria-live") in {None, "off"}
                expect(page.locator("#messages")).to_have_attribute("aria-busy", "true")
                page.unroute("**/api/tasks/*")
                page.locator('[data-view="home"]').click()
                page.set_viewport_size({"width": 390, "height": 844})
                navigation = page.get_by_role("button", name="Open navigation", exact=True)
                expect(navigation).to_have_attribute("aria-expanded", "false")
                navigation.click()
                expect(navigation).to_have_attribute("aria-expanded", "true")
                home_button = page.get_by_role("navigation", name="Main navigation").get_by_role(
                    "button", name="Home", exact=True
                )
                expect(home_button).to_have_attribute("aria-current", "page")
                home_button.click()
                expect(navigation).to_have_attribute("aria-expanded", "false")
                page.get_by_role("button", name="Open navigation", exact=True).click()
                expect(page.locator("main")).to_have_attribute("inert", "")
                expect(page.get_by_role("complementary", name="Workspace navigation").get_by_role("button", name="Close navigation", exact=True)).to_be_focused()
                page.keyboard.press("ControlOrMeta+k")
                expect(page.get_by_role("dialog", name="Search chats", exact=True)).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.get_by_role("dialog")).to_have_count(0)
                expect(page.locator(".sidebar")).to_have_class("sidebar open")
                expect(page.locator("#close-navigation")).to_be_focused()
                page.keyboard.press("Escape")
                expect(page.get_by_role("button", name="Open navigation", exact=True)).to_be_focused()
                expect(page.locator("main")).not_to_have_attribute("inert", "")
                page.screenshot(path=str(OUT / "home-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors, errors
                browser.close()
                print(
                    "PASS selected model readiness, retained draft, modal errors, OAuth resume, streaming focus, and mobile navigation",
                    flush=True,
                )
        finally:
            process.terminate()
            process.wait(timeout=10)
            runtime.shutdown()
            runtime.server_close()
