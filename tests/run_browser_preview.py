"""Watch real agent browsing without switching profiles or taking control."""

import json
import subprocess
import time
import uuid

from integration_support import (
    COMPOSE,
    HOME,
    STATE,
    URL,
    checked,
    connect,
    eventually,
    fixture_settings,
    ready,
    wait,
)
from playwright.sync_api import expect, sync_playwright

client = connect()
ready(client)
fixture_settings(client, browser_auto=True)
OUT = HOME / "screenshots"
OUT.mkdir(exist_ok=True)
marker = uuid.uuid4().hex
bot = checked(client.post("/api/bots", json={"profile": {"name": "Research partner", "browser": "private"}}))


def release(phase):
    subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "fixture",
            "python",
            "-c",
            f"import httpx; httpx.post('http://localhost:8888/watch-fixture/{marker}/{phase}').raise_for_status()",
        ],
        check=True,
        capture_output=True,
    )


def activity():
    return checked(client.get("/api/browser/activity"))


try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 960}, color_scheme="light")
        page = context.new_page()
        errors, mutations = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                mutations.append(request.url)
                if "/api/browser/" in request.url and request.method == "POST"
                else None
            ),
        )
        page.goto(URL)
        page.get_by_label("Password", exact=True).fill(json.loads(STATE.read_text())["password"])
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Your bots")).to_be_visible()
        page.locator('[data-view="chat"]').click()
        page.locator("#prompt").fill("Keep this draft while I watch the research.")
        task = checked(
            client.post("/api/tasks", json={"bot_id": bot["id"], "content": "fixture-watch:" + marker})
        )
        eventually(
            lambda: activity()["lease"] == {"kind": "agent", "owner": task["id"]}, "real agent browser lease"
        )
        # The navigation is held open. Both status and VNC connection must work before it ends.
        start = time.monotonic()
        expect(page.locator("#computer-preview canvas")).to_be_visible(timeout=10000)
        expect(page.locator("[data-preview-status]")).to_have_text("Live · View only", timeout=10000)
        expect(page.locator("[data-preview-owner]")).to_have_text("Research partner is browsing")
        assert time.monotonic() - start < 12
        assert activity()["browser_id"] == bot["browser_id"]
        assert not mutations, mutations
        expect(page.locator("#computer-dock #computer-preview")).to_be_visible()
        assert page.locator("#computer-preview").evaluate(
            "el => el.getBoundingClientRect().right <= document.querySelector('main').getBoundingClientRect().left"
        ), "The default preview must not cover page controls"
        release("navigation")
        page.screenshot(path=str(OUT / "browser-preview-light.png"), full_page=True)
        # Minimizing and restoring never switches a profile, and the same run stays minimized.
        page.get_by_role("button", name="Minimize browser preview").click()
        expect(page.locator("#computer-preview canvas")).to_have_count(0)
        page.locator('[data-view="notebook"]').click()
        expect(page.get_by_role("button", name="Show browser preview")).to_be_visible()
        page.locator("#header-computer").click()
        expect(page.locator("#computer-preview canvas")).to_be_visible(timeout=10000)
        # Desktop movement, theme and mobile bounds.
        move = page.get_by_role("button", name="Move browser preview")
        before = page.locator("#computer-preview").bounding_box()
        move.press("ArrowLeft")
        assert page.locator("#computer-preview").bounding_box()["x"] < before["x"]
        page.screenshot(path=str(OUT / "browser-preview-floating.png"), full_page=True)
        page.get_by_role("button", name="Dock browser preview").click()
        expect(page.locator("#computer-dock #computer-preview")).to_be_visible()
        page.set_viewport_size({"width": 1280, "height": 640})
        assert page.locator(".sidebar-footer").evaluate(
            "el => el.getBoundingClientRect().bottom <= innerHeight"
        ), "The dock and navigation must fit on short laptop screens"
        page.screenshot(path=str(OUT / "browser-preview-short.png"), full_page=True)
        page.set_viewport_size({"width": 844, "height": 390})
        assert page.locator("#computer-preview").evaluate(
            "el => el.getBoundingClientRect().top >= 0 && el.getBoundingClientRect().bottom <= innerHeight"
        ), "The preview must also fit on a landscape phone"
        page.screenshot(path=str(OUT / "browser-preview-landscape.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 960})
        page.locator('[data-view="settings"]').click()
        page.get_by_role("tab", name="Appearance", exact=True).click()
        page.get_by_role("button", name="Minimize browser preview").click()
        page.get_by_role("radio", name="GitHub Dark", exact=False).check()
        page.locator('[data-view="chat"]').click()
        page.locator("#header-computer").click()
        expect(page.locator("#computer-preview canvas")).to_be_visible(timeout=10000)
        expect(page.locator("#prompt")).to_have_value("Keep this draft while I watch the research.")
        page.screenshot(path=str(OUT / "browser-preview-dark.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#navigation-panel")).to_be_hidden()
        expect(page.locator("#computer-dock-mobile #computer-preview")).to_be_visible()
        page.wait_for_function(
            """() => {const r=document.querySelector('#computer-preview').getBoundingClientRect();return r.width>0 && r.x>=0 && r.right<=innerWidth;}"""
        )
        bounds = page.locator("#computer-preview").bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#computer-preview").evaluate(
            "el => el.getBoundingClientRect().bottom <= document.querySelector('#page').getBoundingClientRect().top"
        ), "The mobile preview must sit above the page instead of covering it"
        page.screenshot(path=str(OUT / "browser-preview-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 960})
        page.get_by_role("button", name="Expand browser", exact=True).click()
        expect(page.locator("#browser-screen canvas")).to_be_visible(timeout=10000)
        expect(page.locator("#computer-preview")).to_be_hidden()
        expect(page.locator("#active-browser-profile")).to_contain_text("Research partner is browsing")
        assert activity()["lease"] == {"kind": "agent", "owner": task["id"]}
        assert not mutations, mutations
        page.screenshot(path=str(OUT / "browser-expanded.png"), full_page=True)
        page.get_by_role("button", name="Minimize to preview", exact=False).click()
        expect(page.locator("#computer-preview canvas")).to_be_visible(timeout=10000)
        # Gateway/browser outage clears stale frames and reconnects automatically.
        subprocess.run([*COMPOSE, "stop", "browser"], check=True, capture_output=True)
        expect(page.locator("[data-preview-status]")).to_contain_text("Retrying", timeout=20000)
        expect(page.locator("#computer-preview canvas")).to_have_count(0)
        subprocess.run([*COMPOSE, "start", "browser"], check=True, capture_output=True)
        eventually(lambda: activity().get("idle"), "browser restart")
        # Release the old model run, then start a fresh one after browser recovery.
        release("model")
        wait(client, task["id"])
        marker = uuid.uuid4().hex
        task = checked(
            client.post("/api/tasks", json={"bot_id": bot["id"], "content": "fixture-watch:" + marker})
        )
        expect(page.locator("#computer-preview canvas")).to_be_visible(timeout=20000)
        expect(page.locator("[data-preview-status]")).to_have_text("Live · View only", timeout=10000)
        release("navigation")
        page.get_by_role("button", name="Expand browser", exact=True).click()
        expect(page.locator("#browser-screen canvas")).to_be_visible(timeout=10000)
        # Explicit takeover keeps the private profile we were actually watching.
        page.get_by_role("button", name="Take control", exact=True).click()
        expect(page.locator("#browser-mode")).to_have_text("You have control", timeout=10000)
        assert activity()["browser_id"] == bot["browser_id"]
        assert activity()["lease"]["kind"] == "human"
        page.get_by_role("button", name="Hand back", exact=True).click()
        expect(page.locator("#browser-mode")).to_have_text("View only", timeout=10000)
        release("model")
        wait(client, task["id"])
        # Login revocation must tear down the global viewer, not leave private pixels visible.
        page.locator('[data-view="chat"]').click()
        marker = uuid.uuid4().hex
        task = checked(
            client.post("/api/tasks", json={"bot_id": bot["id"], "content": "fixture-watch:" + marker})
        )
        expect(page.locator("#computer-preview canvas")).to_be_visible(timeout=20000)
        release("navigation")
        # Revoking access mid-drag must also remove global pointer handlers.
        handle = page.get_by_role("button", name="Move browser preview").bounding_box()
        page.mouse.move(handle["x"] + 16, handle["y"] + 12)
        page.mouse.down()
        page.mouse.move(handle["x"] + 80, handle["y"] + 40)
        checked(client.post("/api/sessions/revoke", json={}))
        expect(page.get_by_role("heading", name="Welcome back.")).to_be_visible(timeout=15000)
        expect(page.locator("#computer-preview")).to_have_count(0)
        page.mouse.move(400, 200)
        page.mouse.up()
        assert not errors, errors
        context.close()
        browser.close()
finally:
    release("navigation")
    release("model")
    client.close()
print(
    "PASS live preview during in-flight navigation, owner identity, minimize/restore, expand, preserved drafts, light/dark/mobile, service restart/reconnection, takeover profile and session revocation"
)
