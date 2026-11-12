"""Exercise the assembled Pages site under its real project URL prefix."""

import functools
import re
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "_site"
OUTPUT = ROOT / "test-results/site"


def main():
    if not (SITE / "docs/index.html").is_file():
        raise SystemExit("Build the site with scripts/build_site.py first.")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    failures = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_error(self, code, message=None, explain=None):
            if code >= 400:
                failures.append(f"{code}: {self.path}")
            super().send_error(code, message, explain)

    with tempfile.TemporaryDirectory(prefix="shinbot-site-") as temporary:
        (Path(temporary) / "shinbot").symlink_to(SITE, target_is_directory=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=temporary))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        base = origin + "/shinbot/"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="light")
                context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
                # Production favicon URLs should exercise the local artifact too.
                context.route(
                    "https://adarshdigievo.github.io/shinbot/**",
                    lambda route: route.continue_(
                        url=route.request.url.replace("https://adarshdigievo.github.io", origin)
                    ),
                )
                page = context.new_page()
                page.on("pageerror", lambda error: failures.append(str(error)))
                page.goto(base)
                page.evaluate("document.fonts.ready")
                expect(page.get_by_role("heading", level=1)).to_be_visible()
                page.get_by_role("link", name="Install ShinBot", exact=True).click()
                expect(page.locator("#install-command")).to_be_visible()
                page.get_by_role("button", name="Copy installation command").click()
                expect(page.locator("#copy-status")).to_have_text("Command copied.")
                # Read only the command just copied in this isolated browser.
                assert (
                    page.evaluate("navigator.clipboard.readText()") == "uvx --prerelease allow shinbot start"
                )
                for label in ("Notebook", "Scheduled jobs", "Conversations"):
                    page.get_by_role("tab", name=label, exact=True).click()
                    image = page.locator('[role="tabpanel"]:visible img')
                    image.evaluate("img => img.decode()")
                    assert image.evaluate("img => img.naturalWidth > 0")
                page.get_by_role("tab", name="Conversations", exact=True).press("ArrowRight")
                expect(page.get_by_role("tab", name="Notebook", exact=True)).to_be_focused()
                page.get_by_role("tab", name="Notebook", exact=True).press("Home")
                page.get_by_role("tab", name="Conversations", exact=True).evaluate(
                    "element => element.blur()"
                )
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=OUTPUT / "landing-desktop.png", full_page=True)
                page.screenshot(path=OUTPUT / "landing-first-screen.png")

                page.goto(base + "docs/")
                expect(page.get_by_role("link", name="Install ShinBot", exact=True).first).to_be_visible()
                page.screenshot(path=OUTPUT / "docs-desktop.png", full_page=True)
                page.goto(base + "docs/user-guide/installation.html")
                page.get_by_label("Toggle dark mode", exact=True).click()
                expect(page.locator("html")).to_have_class(re.compile(r".*quarto-dark.*"))
                page.reload()
                expect(page.locator("html")).to_have_class(re.compile(r".*quarto-dark.*"))
                page.screenshot(path=OUTPUT / "docs-dark.png", full_page=True)
                page.get_by_label("Toggle dark mode", exact=True).click()
                page.locator(".aa-DetachedSearchButton").click()
                page.locator("input[type=search]").fill("backup")
                expect(page.locator(".aa-Panel")).to_contain_text("Data, backups and upgrades")
                page.keyboard.press("Escape")

                for width in (390, 320):
                    page.set_viewport_size({"width": width, "height": 844})
                    for route in ("", "docs/", "docs/developers/releases.html"):
                        page.goto(base + route)
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), route
                    if width == 390:
                        page.goto(base)
                        page.evaluate("document.fonts.ready")
                        page.screenshot(path=OUTPUT / "landing-mobile.png", full_page=True)
                        page.goto(base + "docs/user-guide/installation.html")
                        page.screenshot(path=OUTPUT / "docs-mobile.png", full_page=True)
                        page.get_by_label("Toggle navigation", exact=True).click()
                        expect(page.get_by_role("link", name="Developer guide", exact=True)).to_be_visible()
                browser.close()
            if failures:
                raise AssertionError("\n".join(failures))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    print("PASS website tabs, command copy, documentation search, theme persistence and mobile navigation")


if __name__ == "__main__":
    main()
