"""Exercise update notices and controls with unpublished release fixtures."""

import json
import time

from integration_support import ROOT, STATE, URL, checked, connect, ready
from playwright.sync_api import expect, sync_playwright


def main():
    client = connect()
    ready(client)
    actual = checked(client.get("/api/updates"))
    assert actual["current_version"]
    assert client.put("/api/updates/settings", json={"automatic": False}).status_code == 200
    report = {
        **actual,
        "status": "not_published",
        "latest_version": None,
        "checked_at": time.time(),
        "automatic": False,
        "stale": False,
        "message": "A release is not available from both GitHub and PyPI yet.",
    }
    checks = 0
    out = ROOT / "test-results/updates"
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            color_scheme="light",
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def status(route):
            response = route.fetch()
            data = response.json()
            data["updates"] = report
            route.fulfill(response=response, json=data)

        def check(route):
            nonlocal checks
            checks += 1
            if checks == 1:
                report.update(
                    status="available",
                    latest_version="1.10.0",
                    message="ShinBot 1.10.0 is available. Run the update command on the host where ShinBot is installed.",
                    update_command="shinbot update --latest",
                    release_url="https://github.com/adarshdigievo/shinbot/releases/tag/v1.10.0",
                )
            else:
                report.update(
                    status="unavailable",
                    latest_version=None,
                    message="Could not verify a release. Check your connection and try again. Your installation is unchanged.",
                )
            route.fulfill(json=report)

        page.route("**/api/status", status)
        page.route("**/api/updates", lambda route: route.fulfill(json=report))
        page.route("**/api/updates/check", check)
        page.goto(URL)
        page.get_by_label("Password", exact=True).fill(json.loads(STATE.read_text())["password"])
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.locator('[data-view="settings"]').click()
        page.get_by_role("tab", name="Updates", exact=True).click()
        expect(page.get_by_text("No published release yet", exact=True)).to_be_visible()
        expect(page.locator("#copy-update-command")).to_have_count(0)
        page.get_by_role("button", name="Check now", exact=True).click()
        expect(page.get_by_text("An update is available", exact=True)).to_be_visible()
        page.get_by_role("button", name="Copy command", exact=True).click()
        assert page.evaluate("navigator.clipboard.readText()") == "shinbot update --latest"
        page.get_by_label("Check for updates daily", exact=True).check()
        expect(page.get_by_text("Daily update checks enabled.", exact=True)).to_be_visible()
        assert checked(client.get("/api/updates"))["automatic"] is True
        page.get_by_label("Check for updates daily", exact=True).uncheck()
        expect(page.get_by_text("Daily update checks disabled.", exact=True)).to_be_visible()
        assert checked(client.get("/api/updates"))["automatic"] is False
        page.reload()
        expect(page.locator("#update-notice")).to_be_visible()
        page.locator("#update-notice").click()
        expect(page.get_by_role("tab", name="Updates", exact=True)).to_have_attribute("aria-selected", "true")
        page.screenshot(path=str(out / "updates-light.png"), full_page=True)
        page.emulate_media(color_scheme="dark")
        page.screenshot(path=str(out / "updates-dark.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(out / "updates-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_role("button", name="Check now", exact=True).click()
        expect(page.get_by_text("Couldn’t check for updates", exact=True)).to_be_visible()
        expect(page.locator("#copy-update-command")).to_have_count(0)
        assert not errors, errors
        browser.close()
    print(
        "PASS unpublished, available and offline update UI; host command copy; persisted automatic-check preference; sidebar notice; light/dark/mobile",
        flush=True,
    )


if __name__ == "__main__":
    main()
