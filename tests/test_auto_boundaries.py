"""Auto mode's unattended surface: read-only commands, browser destinations and link clicks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shinbot import browser_policy
from shinbot.approvals import guarded_action
from shinbot.browser_policy import assess, classify, navigation_safe, public_destination
from shinbot.services import browser as service
from shinbot.services.common import Journal


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls",
        "ls -la src",
        "cat notes/today.md",
        "head -n 20 ./report.csv",
        "tail -c 200 logs/build.log",
        "wc -l data/a.txt data/b.txt",
        "ls /workspace/projects",
        "cat /workspace/notes.txt",
        "echo done",
        "git status",
    ],
)
def test_reads_inside_the_workspace_stay_unattended(command):
    assert not guarded_action("run_command", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/*/environ",
        "cat /proc/1/environ",
        "cat /proc/self/environ",
        "ls /proc",
        "head /sys/class/net/eth0/address",
        "tail /dev/urandom",
        "cat /etc/passwd",
        "cat ../../etc/passwd",
        "cat notes/../../secret",
        "cat /workspace/../data/actions.sqlite",
        "cat /workspacex/file",
        "cat ~/.ssh/id_rsa",
        "ls src/*.py",
        "cat file?.txt",
        "cat [ab].txt",
        "cat {a,b}.txt",
        "wc --files0-from=/proc/1/environ",
        "ls -I/etc",
        "cat -- /data/browser/view-password",
    ],
)
def test_globs_and_paths_outside_the_workspace_need_a_decision(command):
    assert guarded_action("run_command", {"command": command})


@pytest.mark.parametrize(
    "url,safe",
    [
        ("https://example.com/api/deleteAccount?id=1", False),
        ("https://example.com/api/delete2", False),
        ("https://example.com/users/5/sendMessage", False),
        ("https://example.com/account/close", False),
        ("https://example.com/subscription/cancel?now=1", False),
        ("https://example.com/orders/5/approve", False),
        ("https://example.com/invites/9/accept", False),
        ("https://example.com/plan?downgrade", False),
        ("https://example.com/app#/items/5/delete", False),
        # Headlines, long slugs, nouns and heading anchors are ordinary reading.
        ("https://news.example.com/2026/markets-close-higher-as-rates-hold", True),
        ("https://example.com/blog/why-we-cancel-so-many-meetings-these-days", True),
        ("https://shop.example.com/order/1234", True),
        ("https://explorer.example.com/block/800000", True),
        ("https://example.com/share/abc123", True),
        ("https://docs.example.com/guide#delete-a-project", True),
        ("https://example.com/watch?v=dQw4w9WgXcQ", True),
        ("https://example.com/article#:~:text=how%20to%20delete", True),
    ],
)
def test_navigation_words(url, safe):
    assert navigation_safe(url) is safe


def link(**changes):
    return {
        "url": "https://example.com/",
        "tag": "a",
        "href": "https://example.com/articles/5",
        "raw_href": "/articles/5",
        "label": "Read the full story",
        "public": True,
        **changes,
    }


@pytest.mark.parametrize(
    "target",
    [
        link(),
        link(label="Markets close higher as the central bank holds its rate"),
        link(label="Next"),
        link(href="https://example.com/docs#installation", raw_href="#installation", label="Installation"),
        link(href="https://other.example.org/page", raw_href="https://other.example.org/page"),
        link(role="link"),
        link(method_hint="get"),
    ],
)
def test_ordinary_links_are_followed_without_a_prompt(target):
    assert classify({"operation": "click"}, target) == (True, "Follow a page link.")


@pytest.mark.parametrize(
    "target",
    [
        link(href="https://example.com/#", raw_href="#", label=""),
        link(href="https://example.com/", raw_href="", label="Continue"),
        link(href="javascript:void(0)", raw_href="javascript:void(0)"),
        link(label="Delete", href="https://example.com/items/5", raw_href="/items/5"),  # title only
        link(label="Cancel subscription"),
        link(label="Close account"),
        link(label="Approve"),
        link(method_hint="delete"),
        link(method_hint="post"),
        link(confirm=True),
        link(role="button"),
        link(inline_handler=True),
        link(download=True),
        link(href="https://example.com/account/close", raw_href="/account/close", label=""),
        link(name="cancelOrder"),
        link(public=False),
        {k: v for k, v in link().items() if k != "public"},
    ],
)
def test_links_that_act_or_leave_the_public_web_wait_for_the_owner(target):
    assert not classify({"operation": "click"}, target)[0]


async def test_only_globally_routable_hosts_are_public(monkeypatch):
    answers = {
        "public.example.com": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
        "intranet.example.com": ["10.1.2.3"],
        "rebind.example.com": ["93.184.216.34", "127.0.0.1"],
        "mapped.example.com": ["::ffff:192.168.1.5"],
        "scoped.example.com": ["fe80::1%eth0"],
    }
    asked = []

    async def resolve(host):
        asked.append(host)
        if host not in answers:
            raise OSError("Name or service not known")
        return answers[host]

    monkeypatch.setattr(browser_policy, "resolve", resolve)
    for url in [
        "http://127.0.0.1:8765/",
        "http://localhost/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://172.16.4.1/",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.7/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://control:8765/",
        "http://fixture:8888/",
        "http://2130706433/",
        "http://printer.local/",
        "http://db.internal/",
        "http://host.docker.internal/",
        "http://nas.lan/",
        "http://intranet.example.com/",
        "http://rebind.example.com/",
        "http://mapped.example.com/",
        "http://scoped.example.com/",
        "http://unresolvable.example.com/",
    ]:
        assert not await public_destination(url), url
    for url in ["https://public.example.com/docs", "https://93.184.216.34/", "https://[2606:4700::1111]/"]:
        assert await public_destination(url), url
    # Names without a dot and local suffixes are refused without asking a resolver.
    assert not {"control", "fixture", "printer.local", "db.internal", "localhost"} & set(asked)
    # One answer is reused for the assessment and its recheck at execution.
    assert asked.count("public.example.com") == 1
    # A page that is already open may link within its own origin; other private hosts stay closed.
    assert await public_destination("http://192.168.1.10:8080/next", "http://192.168.1.10:8080/app")
    assert not await public_destination("http://192.168.1.10:9000/next", "http://192.168.1.10:8080/app")
    monkeypatch.setenv("SHINBOT_BROWSER_TRUSTED_HOSTS", "nas.lan, 192.168.1.20:3000")
    assert await public_destination("http://nas.lan/files")
    assert await public_destination("http://192.168.1.20:3000/")
    assert not await public_destination("http://192.168.1.20/")


async def test_name_resolution_runs_off_the_loop_with_a_deadline(monkeypatch):
    real = asyncio.get_running_loop().getaddrinfo
    monkeypatch.undo()  # Use the real resolver with a stalled lookup underneath.
    browser_policy.resolved.clear()

    async def stalled(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", stalled)
    started = asyncio.get_running_loop().time()
    try:
        assert not await public_destination("https://slow.example.com/")
    finally:
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", real)
        browser_policy.resolved.clear()
    assert asyncio.get_running_loop().time() - started < 5


async def test_assessment_requires_approval_for_private_destinations():
    page = SimpleNamespace(url="https://example.com/")
    public = await assess(page, {"operation": "goto", "url": "https://example.org/docs"})
    assert public["safe"] and public["destination"] == "https://example.org/docs"
    for url in ["http://192.168.1.1/admin", "http://control:8765/", "http://169.254.169.254/latest/meta-data/"]:
        review = await assess(page, {"operation": "goto", "url": url})
        assert not review["safe"] and "private or local network" in review["reason"]
    # A risky address keeps its own explanation.
    review = await assess(page, {"operation": "goto", "url": "https://example.org/account/delete"})
    assert not review["safe"] and "consequential" in review["reason"]


@pytest.fixture
def browser(monkeypatch, tmp_path):
    page = SimpleNamespace(
        url="https://example.org/",
        is_closed=lambda: False,
        title=AsyncMock(return_value="Page"),
        screenshot=AsyncMock(return_value=b"\x89PNG\r\n\x1a\npixels"),
    )

    async def goto(url, **_):
        # A public address that answers with a redirect to the router.
        page.url = "about:blank" if url == "about:blank" else page.lands_on.get(url, url)

    page.goto, page.lands_on = AsyncMock(side_effect=goto), {}
    for name, value in {
        "lock": asyncio.Lock(),
        "page": page,
        "browser": SimpleNamespace(pages=[page], close=AsyncMock()),
        "backend_driver": None,
        "active_backend": "chromium",
        "lease": None,
        "human_pending": False,
        "cancelled": set(),
        "observations": {},
        "private_origins": set(),
        "control_generation": 0,
        "last_agent": "task",
        "active_browser": "shared",
        "journal": Journal(tmp_path),
        "select_profile": AsyncMock(),
        "observe": AsyncMock(return_value={"content": [{"type": "text", "text": "page text"}]}),
        "DATA": tmp_path,
    }.items():
        monkeypatch.setattr(service, name, value)
    return page


def request(key, args, **extra):
    return {"id": key, "task_id": "task", "epoch": "e", "approval_mode": "auto", "args": args, **extra}


async def test_redirect_to_a_private_address_is_not_read_back(browser):
    browser.lands_on["https://example.org/go"] = "http://192.168.1.1/admin"
    result = await service.execute(request("redirect", {"operation": "goto", "url": "https://example.org/go"}))
    assert result["isError"] and "192.168.1.1" in result["content"][0]["text"]
    assert browser.url == "about:blank"
    service.observe.assert_not_awaited()
    # The same answer is replayed; nothing navigates again.
    assert await service.execute(request("redirect", {"operation": "goto", "url": "https://example.org/go"})) == result
    assert [call.args[0] for call in browser.goto.await_args_list] == ["https://example.org/go", "about:blank"]


async def test_scripted_redirect_is_caught_when_the_page_is_read(browser):
    browser.url = "http://10.0.0.8/status"  # Changed by the page after the last action finished.
    for operation in ("observe", "screenshot"):
        browser.url = "http://10.0.0.8/status"
        result = await service.execute(request(operation, {"operation": operation}))
        assert result["isError"] and "10.0.0.8" in result["content"][0]["text"]
        assert browser.url == "about:blank"
    service.observe.assert_not_awaited()
    browser.screenshot.assert_not_awaited()


async def test_private_address_needs_and_honours_the_owners_approval(browser):
    args = {"operation": "goto", "url": "http://192.168.1.10:8080/app"}
    review = await service.execute(request("lan", args))
    assert review["status"] == "approval_required" and "private or local network" in review["reason"]
    browser.goto.assert_not_awaited()
    result = await service.execute(request("lan", args, approval_context=review["context"]))
    assert result == {"content": [{"type": "text", "text": "page text"}]}
    assert browser.url == "http://192.168.1.10:8080/app"
    # Once approved, the bot keeps reading that application and follows its own links.
    assert not (await service.execute(request("read", {"operation": "observe"}))).get("isError")
    assert await public_destination("http://192.168.1.10:8080/reports", browser.url)
    # Closing the browser forgets the approval.
    await service.close_browser()
    assert not service.private_origins


async def test_pages_the_owner_left_open_can_be_read_and_full_access_is_unchanged(browser, monkeypatch):
    browser.url = "http://nas.lan/files"
    monkeypatch.setattr(service, "lease", {"kind": "human", "owner": "owner"})
    await service.release({"owner": "owner"})
    assert not (await service.execute(request("after", {"operation": "observe"}))).get("isError")
    service.private_origins.clear()
    assert not (await service.execute(request("full", {"operation": "observe"}, approval_mode="full"))).get(
        "isError"
    )
    assert browser.url == "http://nas.lan/files"
