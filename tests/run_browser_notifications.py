"""Exercise native notifications through Chromium and an isolated ShinBot server."""

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

OUT = Path(__file__).resolve().parents[1] / "test-results/browser-notifications"
DELIVERIES = "() => window.notificationDeliveries || []"


def wait_alerts(page, count):
    for _ in range(100):
        alerts = page.evaluate(DELIVERIES)
        if len(alerts) == count:
            return alerts
        page.wait_for_timeout(100)
    raise AssertionError(f"Expected {count} notification deliveries, got {alerts}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinbot-native-notification-test-") as temporary:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        app = create_app(Config(data=Path(temporary), origin=origin, coordinate=False))
        core = app.state.core
        core.db.set("password", hash_password("isolated-browser-notification-password"))
        core.db.execute("DELETE FROM events")
        session = core.auth.session()
        bot = core.bots.save({"name": "Reviewer", "color": "violet"})
        task = core.tasks.create(
            "Review the release notes.",
            title="Release review",
            bot_id=bot["id"],
            provider="fixture",
            model="fixture",
        )
        core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task["id"],))
        core.db.execute(
            "UPDATE messages SET status='completed',answer='The release notes are ready.' WHERE task_id=?",
            (task["id"],),
        )
        core.db.event("completed", "Old reply", task["id"])

        async def runtime(method, path, payload=None, **kwargs):
            if path == "/v1/providers":
                return []
            if path == "/v1/health":
                return {"ready": True}
            return {}

        core.coordinator.request = runtime
        core.coordinator.ready = True
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            assert server.started
            with sync_playwright() as p:
                # macOS drops headless notifications; use a temporary offscreen window.
                browser = p.chromium.launch(
                    channel="chromium", headless=False, args=["--window-position=-10000,-10000"]
                )
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.grant_permissions(["notifications"], origin=origin)
                context.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                # Call the real API and record successful handoffs. macOS may hide
                # banners based on system permissions or Focus, even after success.
                context.add_init_script("""
                    // Playwright keeps every tab focused. Explicitly model focus
                    // changes while leaving the real notification API intact.
                    window.notificationTestFocused = true;
                    document.hasFocus = () => window.notificationTestFocused;
                    window.notificationDeliveries = [];
                    const show = ServiceWorkerRegistration.prototype.showNotification;
                    ServiceWorkerRegistration.prototype.showNotification = async function(title, options) {
                        const result = await show.call(this, title, options);
                        window.notificationDeliveries.push({title, ...options});
                        return result;
                    };
                """)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.emulate_media(reduced_motion="reduce")
                page.goto(origin)
                expect(page.locator("#new-bot")).to_be_visible()
                page.locator("#notifications-toggle").click()
                enable = page.locator("#notifications-menu [data-browser-notifications-toggle]")
                expect(enable).to_have_text("Enable notifications")
                assert page.evaluate(DELIVERIES) == []
                enable.click()
                expect(enable).to_have_text("Turn off notifications")
                # Granting permission must not replay the existing unread feed.
                page.wait_for_timeout(1200)
                assert page.evaluate(DELIVERIES) == []
                page.locator("#notifications-menu [data-browser-notifications-test]").click()
                assert wait_alerts(page, 1)[0]["tag"] == "shinbot:test"
                page.evaluate("window.notificationDeliveries = []")
                # A foreground tab stays quiet.
                page.bring_to_front()
                core.db.event("completed", "Foreground reply", task["id"])
                expect(page.locator("#notification-count")).to_have_text("2")
                page.wait_for_timeout(300)
                assert page.evaluate(DELIVERIES) == []
                # Two app tabs receive the same event; only one system alert is sent.
                peer = context.new_page()
                peer.goto(origin)
                expect(peer.locator("#notification-count")).to_have_text("2")
                background = context.new_page()
                background.goto("about:blank")
                background.bring_to_front()
                for tab in (page, peer):
                    tab.evaluate("notificationTestFocused=false;dispatchEvent(new Event('blur'))")
                core.db.event("failed", "A background failure", task["id"])
                expect(page.locator("#notification-count")).to_have_text("3")
                for _ in range(100):
                    notifications = page.evaluate(DELIVERIES) + peer.evaluate(DELIVERIES)
                    if notifications:
                        break
                    page.wait_for_timeout(100)
                assert len(notifications) == 1, {
                    "deliveries": notifications,
                    "page": page.evaluate(
                        "({focus:document.hasFocus(),visible:document.visibilityState,marker:localStorage.getItem('shinbot.browser-notifications.focus'),known:localStorage.getItem('shinbot.browser-notifications.delivered')})"
                    ),
                    "peer": peer.evaluate("({focus:document.hasFocus(),visible:document.visibilityState})"),
                }
                assert notifications[0]["title"] == "Release review"
                assert "Task failed" in notifications[0]["body"]
                notification_id = notifications[0]["data"]["notificationId"]
                known = page.evaluate(
                    "JSON.parse(localStorage.getItem('shinbot.browser-notifications.delivered'))"
                )
                assert known.count(notification_id + ":1") == 1
                # Polling and reload must not repeat a delivery.
                page.wait_for_timeout(1800)
                assert len(page.evaluate(DELIVERIES) + peer.evaluate(DELIVERIES)) == 1
                peer.reload()
                expect(peer.locator("#notification-count")).to_have_text("3")
                assert (
                    page.evaluate(
                        "JSON.parse(localStorage.getItem('shinbot.browser-notifications.delivered'))"
                    ).count(notification_id + ":1")
                    == 1
                )
                # Exercise the real worker-to-page navigation handoff. The worker's
                # notificationclick routing is covered separately without OS UI.
                worker = context.service_workers[0]
                worker.evaluate(
                    """async id => {
                      const windows = await self.clients.matchAll({type:'window', includeUncontrolled:true});
                      const client = windows.find(c => new URL(c.url).pathname === '/');
                      client.postMessage({type:'shinbot:notification-open', id});
                    }""",
                    notification_id,
                )

                def chat_opened():
                    return any(tab.locator("#task-title").count() for tab in (page, peer))

                for _ in range(50):
                    if chat_opened():
                        break
                    page.wait_for_timeout(100)
                opened = page if page.locator("#task-title").count() else peer
                expect(opened.locator("#task-title")).to_have_text("Release review")
                expect(opened.locator("#header-bot-name")).to_have_text("Reviewer")
                assert core.db.one("SELECT revision FROM notification_reads WHERE id=?", (notification_id,))
                # The deep link also survives a fresh page load.
                page.goto(origin + "/#notification=" + notification_id)
                expect(page.locator("#task-title")).to_have_text("Release review")
                expect(page.locator("#header-bot-name")).to_have_text("Reviewer")
                assert "notification=" not in page.url
                # Approval alerts must open the conversation without approving it.
                for tab in (page, peer):
                    tab.evaluate(
                        "notificationDeliveries=[];notificationTestFocused=false;dispatchEvent(new Event('blur'))"
                    )
                core.db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
                core.db.execute(
                    "INSERT INTO actions(id,task_id,epoch,tool,args,hash,status,created,expires) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "review-action",
                        task["id"],
                        "test-epoch",
                        "run_command",
                        "{}",
                        "test-hash",
                        "pending",
                        time.time(),
                        time.time() + 300,
                    ),
                )
                for _ in range(100):
                    approvals = page.evaluate(DELIVERIES) + peer.evaluate(DELIVERIES)
                    if approvals:
                        break
                    page.wait_for_timeout(100)
                assert len(approvals) == 1, approvals
                assert approvals[0]["data"]["notificationId"] == "action:review-action"
                assert "Approval needed" in approvals[0]["body"]
                page.goto(origin + "/#notification=action:review-action")
                expect(page.locator("#messages .approval")).to_have_count(1)
                expect(page.locator("#header-bot-name")).to_have_text("Reviewer")
                assert (
                    core.db.one("SELECT status FROM actions WHERE id='review-action'")["status"] == "pending"
                )
                # Settings exposes the same saved browser preference.
                page.locator('[data-view="settings"]').click()
                page.get_by_role("tab", name="Notifications", exact=True).click()
                panel = page.locator("#notifications-panel")
                expect(panel.locator("[data-browser-notifications-toggle]")).to_have_text(
                    "Turn off notifications"
                )
                for theme, width in (("light", 1440), ("dark", 1440), ("light", 390)):
                    page.evaluate("value => window.ShinBotTheme.set(value)", theme)
                    page.set_viewport_size({"width": width, "height": 900})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(OUT / f"settings-{theme}-{width}.png"), animations="disabled")
                panel.locator("[data-browser-notifications-toggle]").click()
                expect(panel.locator("[data-browser-notifications-toggle]")).to_have_text(
                    "Enable notifications"
                )
                peer.locator("#notifications-toggle").click()
                expect(peer.locator("#notifications-menu [data-browser-notifications-toggle]")).to_have_text(
                    "Enable notifications"
                )
                page.evaluate("window.notificationDeliveries = []")
                peer.evaluate("window.notificationDeliveries = []")
                background.bring_to_front()
                core.db.event("failed", "Off means off", task["id"])
                page.wait_for_timeout(1600)
                assert page.evaluate(DELIVERIES) + peer.evaluate(DELIVERIES) == []
                page.reload()
                # A reload keeps the current page, so wait for the shell rather than Home.
                expect(page.locator("#notifications-toggle")).to_be_visible()
                page.locator("#notifications-toggle").click()
                expect(page.locator("#notifications-menu [data-browser-notifications-toggle]")).to_have_text(
                    "Enable notifications"
                )
                # An explicit denial gives recovery instructions and no broken button.
                denied = browser.new_context(viewport={"width": 390, "height": 844})
                denied.add_cookies([{"name": "shinbot_session", "value": session, "url": origin}])
                denied.add_init_script(
                    "Object.defineProperty(Notification, 'permission', {get:()=> 'denied'});"
                )
                blocked = denied.new_page()
                blocked.goto(origin)
                expect(blocked.locator("#new-bot")).to_be_visible()
                blocked.locator("#notifications-toggle").click()
                expect(blocked.locator("[data-browser-notifications-toggle]")).to_be_disabled()
                expect(blocked.locator("[data-browser-notification-status]")).to_contain_text(
                    "browser’s site settings"
                )
                blocked.screenshot(path=str(OUT / "permission-blocked-mobile.png"), animations="disabled")
                assert not errors, errors
                browser.close()
            print(
                "PASS native API delivery, opt-in, test alert, foreground suppression, tab deduplication, reload, click routing, read receipts, settings, denied permission, themes and mobile"
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    main()
