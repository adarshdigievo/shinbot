"""Exercise the real browser UI and save visual review captures locally."""

import json
import uuid
from pathlib import Path

from integration_support import HOME, STATE, URL, connect, fixture_settings, ready
from playwright.sync_api import expect, sync_playwright

helper_name = "UI personal helper " + uuid.uuid4().hex[:6]
OUT = HOME / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
client = connect()
ready(client)
fixture_settings(client)
client.close()
secret = json.loads(STATE.read_text())
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": 1440, "height": 1050})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: (
            errors.append(f"Missing static asset: {response.url}")
            if "/static/" in response.url and response.status >= 400
            else None
        ),
    )
    page.goto(URL)
    expect(page.get_by_role("heading", name="Welcome back.", exact=True)).to_be_visible()
    page.screenshot(path=str(OUT / "login.png"), full_page=True)
    page.get_by_label("Password", exact=True).fill(secret["password"])
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Your bots")).to_be_visible()
    # CSS masks must resolve in the packaged app; source-only icon checks miss broken asset paths.
    assert page.locator(".icon").evaluate_all(
        "nodes => nodes.every(node => getComputedStyle(node).maskImage !== 'none')"
    )
    # Check actual image decoding, including the favicon, in the packaged app.
    expect(page.locator(".brand img[data-brand]:visible")).to_be_visible()
    page.wait_for_function(
        "Array.from(document.querySelectorAll('img[data-brand]')).every(image => image.complete && image.naturalWidth > 0)"
    )
    favicon = page.locator('link[rel="icon"]').get_attribute("href")
    assert page.request.get(URL + favicon).ok, "The packaged favicon must be served"
    page.screenshot(path=str(OUT / "home.png"), full_page=True)
    before = page.locator(".bot-card").count()
    page.locator(".template-card").filter(
        has=page.get_by_role("heading", name="Research & writing team", exact=True)
    ).get_by_role("button").click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/templates/install")
        and response.request.method == "POST", timeout=30000
    ) as installed_template:
        page.get_by_role("button", name="Add to my bots", exact=True).click()
    assert installed_template.value.ok
    expect(page.locator(".bot-card")).to_have_count(before + 2)
    page.get_by_role("button", name="Create bot", exact=True).click()
    page.get_by_label("Bot name", exact=True).fill(helper_name)
    page.get_by_label("Role and instructions", exact=True).fill("Help me plan the day.")
    page.get_by_label("Browser sign-ins").select_option("private")
    page.get_by_role("button", name="Save profile", exact=True).click()
    helper = page.locator(".bot-card").filter(has=page.get_by_role("heading", name=helper_name, exact=True))
    expect(helper).to_be_visible()
    with page.expect_download() as exported:
        helper.get_by_role("button", name="Export " + helper_name + " template").click()
    data = json.loads(Path(exported.value.path()).read_text())
    assert data["profiles"][0]["browser"] == "private" and "credentials" not in data
    helper.get_by_role("button", name="Edit", exact=True).click()
    page.get_by_label("Browser sign-ins").select_option("group")
    page.get_by_role("button", name="Save profile", exact=True).click()
    expect(helper).to_contain_text("This group")
    # Bot profiles are listed by default so switching bots needs no extra step.
    expect(page.locator(".bot-roster")).to_have_attribute("open", "")
    page.locator('[data-sidebar-bot="default"]').click()
    expect(page.get_by_role("heading", name="What can I help with?")).to_be_visible()
    page.screenshot(path=str(OUT / "chat.png"), full_page=True)
    for view in ["notebook", "jobs", "plugins", "settings", "computer"]:
        page.locator('[data-view="' + view + '"]').click()
        expect(page.locator("#page h1")).to_have_text(view.title())
        expect(page.get_by_text("Could not load this page", exact=True)).to_have_count(0)
        if view == "settings":
            page.get_by_role("tab", name="Permissions", exact=True).click()
            learning = page.get_by_label(
                "Remember preferences and corrections I share without asking each time"
            )
            learning.check()
            page.get_by_role("button", name="Save settings", exact=True).click()
            expect(page.locator("#toast")).to_have_text("Settings saved.")
            page.locator('[data-view="notebook"]').click()
            page.locator('[data-view="settings"]').click()
            page.get_by_role("tab", name="Permissions", exact=True).click()
            expect(learning).to_be_checked()
            learning.uncheck()
            page.get_by_role("button", name="Save settings", exact=True).click()
        if view == "computer":
            expect(page.locator("#browser-screen canvas")).to_be_visible(timeout=20000)
            page.get_by_role("button", name="Take control", exact=True).click()
            expect(page.locator("#browser-mode")).to_contain_text("You have control", timeout=15000)
            expect(page.locator("#browser-hint")).to_contain_text("You control", timeout=15000)
            page.get_by_role("button", name="Hand back", exact=True).click()
            expect(page.locator("#browser-mode")).to_contain_text("View only", timeout=15000)
            expect(page.locator("#browser-hint")).to_contain_text("Connected", timeout=15000)
        page.screenshot(path=str(OUT / (view + ".png")), full_page=True)
    page.locator("#new-task").click()
    page.get_by_role("textbox", name="Message ShinBot", exact=True).fill("hello UI fixture")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(".message.assistant")).to_contain_text(
        "Fixture completed. hello UI fixture", timeout=30000
    )
    page.screenshot(path=str(OUT / "conversation.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#navigation-panel")).to_be_hidden()
    page.screenshot(path=str(OUT / "mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
    assert not errors, errors
    context.close()
    browser.close()
print(
    "PASS Home template install, profile create/edit/export, sign-in, six views, live VNC takeover, chat and mobile; no JS errors"
)
