"""Exercise Auto against a real browser DOM through the authenticated executor API."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from shinbot.config import Config
from shinbot.core import Core
from shinbot.db import uid
from shinbot.services import browser as service
from shinbot.services.common import Journal

HTML = """<html><title>Search</title><body>
<form action="/search" method="get"><textarea name="q" aria-label="Search"></textarea><input type="submit" name="btnK" value="Google Search"></form>
<button id="delete" type="button" onclick="document.body.dataset.deleted=Number(document.body.dataset.deleted||0)+1">Delete account</button>
<button id="unknown" type="button" onclick="document.body.dataset.unknown='yes'">Continue</button>
</body></html>"""


async def main():
    with tempfile.TemporaryDirectory(prefix="shinbot-auto-") as temporary:
        root = Path(temporary)
        core = Core(Config(data=root / "control", coordinate=False))
        core.db.set("preferences", {**core.db.setting("preferences"), "provider": "test", "model": "test"})
        task = core.tasks.create("Search a website")
        os.environ["SHINBOT_EXECUTOR_TOKEN"] = "test-browser-token" * 4
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            await context.route(
                "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=HTML)
            )
            page = await context.new_page()
            service.browser, service.page = context, page
            service.active_browser = "shared"
            service.lease, service.human_pending = None, False
            service.cancelled, service.observations = set(), {}
            service.last_agent, service.control_generation = None, 0
            service.lock = asyncio.Lock()
            (root / "journal").mkdir()
            service.journal = Journal(root / "journal")

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=service.app),
                base_url="http://browser",
                headers={"Authorization": "Bearer " + os.environ["SHINBOT_EXECUTOR_TOKEN"]},
            ) as client:

                async def remote(name, path, body=None, method="POST", **kwargs):
                    assert name == "browser"
                    response = await client.request(
                        method, path, **({"json": body} if body is not None else {})
                    )
                    response.raise_for_status()
                    return response.json()

                core.remote = remote

                def payload(args):
                    return {
                        "id": uid(),
                        "epoch": core.db.setting("epoch"),
                        "task_id": task["id"],
                        "tool": "browser",
                        "args": args,
                    }

                async def finish(request):
                    result = await core.action(request)
                    if result["status"] == "executing":
                        await asyncio.gather(*core.running_actions.values())
                        result = await core.action(request)
                    return result

                for args in (
                    {"operation": "goto", "url": "https://search.test/"},
                    {"operation": "fill", "selector": "textarea[name=q]", "text": "delete old files safely"},
                    {"operation": "click", "selector": "input[name=btnK]"},
                ):
                    result = await finish(payload(args))
                    assert result["status"] == "completed", result
                assert "/search?q=" in page.url
                assert core.db.one("SELECT COUNT(*) AS n FROM actions WHERE status='pending'")["n"] == 0
                invalid = payload({"operation": "click", "selector": "#missing"})
                for _ in range(2):
                    failed = await finish(invalid)
                    assert failed["status"] == "failed" and "Observe again" in failed["error"]
                assert service.journal.receipt(invalid["id"])["status"] == "missing"
                delete = payload({"operation": "click", "selector": "#delete"})
                assert (await finish(delete))["status"] == "pending"
                assert await page.get_attribute("body", "data-deleted") is None
                core.approvals.decide(delete["id"], True)
                await page.locator("#delete").evaluate("el=>el.textContent='Permanently delete account'")
                assert (await finish(delete))["status"] == "pending"
                assert await page.get_attribute("body", "data-deleted") is None
                core.approvals.decide(delete["id"], True)
                assert (await finish(delete))["status"] == "completed"
                assert (await finish(delete))["status"] == "completed"
                assert await page.get_attribute("body", "data-deleted") == "1"
                unknown = payload({"operation": "click", "selector": "#unknown"})
                assert (await finish(unknown))["status"] == "pending"
                assert await page.get_attribute("body", "data-unknown") is None
                core.tasks.options(task["id"], {"approval_mode": "full"})
                assert (await finish(unknown))["status"] == "completed"
                assert await page.get_attribute("body", "data-unknown") == "yes"
                service.lease = {"kind": "human", "owner": "owner"}
                assert (await finish(payload({"operation": "goto", "url": "https://search.test/"})))[
                    "error"
                ] == "Waiting for the shared browser."
            await browser.close()
            await core.close()
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "verified": [
                        "navigation",
                        "Google-style search fill and submit without prompts",
                        "destructive click approval",
                        "changed target invalidates approval",
                        "no duplicate click",
                        "unclear control pauses",
                        "explicit Full access",
                        "human takeover",
                    ],
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
