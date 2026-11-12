import asyncio
import base64
import hmac
import math
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import websockets
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from playwright.async_api import async_playwright

from ..attachments import FileStore, file_name
from ..browser_policy import assess
from . import desktop
from .browser_backends import BACKENDS, catalog, launch
from .common import Journal, service_auth

DATA = Path(os.getenv("SHINBOT_BROWSER_DATA", "/data"))
browser = None
playwright = None
page = None
journal = None
lease = None
human_pending = False
lock = asyncio.Lock()
cancelled = set()
vnc_password = None
control_generation = 0
observations = {}
last_agent = None
active_browser = "shared"
active_backend = "chromium"
backend_driver = None
lease_touched = 0.0


def native_view():
    return os.getenv("SHINBOT_BROWSER_VIEW") == "native"


@asynccontextmanager
async def lifespan(app):
    global browser, playwright, page, journal, vnc_password
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True)
    journal = Journal(DATA)
    password_file = DATA / "view-password"
    if not password_file.exists():
        password_file.write_text(secrets.token_hex(4))
    vnc_password = password_file.read_text().strip()
    playwright = await async_playwright().start()
    launcher = None
    try:
        if desktop.enabled():
            from .desktop_link import serve

            launcher = await serve(
                desktop.state_path() / "browser-launch.sock",
                int(os.environ["SHINBOT_WORKSPACE_UID"]),
                int(os.environ["SHINBOT_WORKSPACE_GID"]),
                open_desktop_browser,
            )
        yield
    finally:
        if launcher:
            launcher.close()
            await launcher.wait_closed()
        await close_browser()
        await playwright.stop()
        journal.db.close()


async def close_browser():
    global browser, page, backend_driver, control_generation
    context, driver = browser, backend_driver
    if context is None:
        return
    if driver:
        try:
            await driver.close()
        except Exception:
            pass  # The owned Playwright context still provides the final process cleanup.
    await context.close()
    browser = page = backend_driver = None
    observations.clear()
    control_generation += 1


async def select_profile(browser_id, backend=None):
    global browser, page, active_browser, active_backend, backend_driver, control_generation
    backend = backend or active_backend
    if backend not in BACKENDS:
        raise HTTPException(400, "Choose a supported browser in Settings → Browser.")
    if not isinstance(browser_id, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", browser_id):
        raise ValueError("Invalid browser profile.")
    # A settings change never replaces a browser while its owner is using it.
    if browser is not None and active_browser == browser_id and (active_backend == backend or lease):
        return
    await close_browser()
    try:
        browser, backend_driver = await launch(
            playwright,
            backend,
            DATA,
            browser_id,
            headless=native_view() or os.getenv("SHINBOT_HEADLESS", "0") == "1",
        )
    except Exception as error:
        page = None
        raise HTTPException(
            400,
            f"Could not open {BACKENDS[backend]}. No action ran. Check its installation in Settings → Browser, then try again.",
        ) from error
    active_backend = backend
    page = browser.pages[0] if browser.pages else await browser.new_page()
    page.set_default_timeout(15000)
    if not native_view():
        await page.bring_to_front()

    async def track(new_page):
        global page
        page = new_page
        page.set_default_timeout(15000)

    browser.on("page", track)
    active_browser = browser_id
    control_generation += 1


def expire_lease():
    global lease, control_generation
    if lease and not lock.locked() and time.monotonic() - lease_touched > 300:
        lease = None
        desktop.set_owner(None)
        control_generation += 1


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/actions/{action_id}", dependencies=[Depends(service_auth)])
async def action_receipt(action_id: str):
    return journal.receipt(action_id)


@app.get("/files/{digest}", dependencies=[Depends(service_auth)])
async def attachment_file(digest: str):
    try:
        return FileStore(DATA / "attachments").export(digest)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Attachment not found.") from exc


@app.get("/backends", dependencies=[Depends(service_auth)])
async def backends():
    return {
        "backends": await asyncio.to_thread(catalog, playwright),
        "active": active_backend,
        "desktop": desktop.enabled(),
    }


@app.get("/health", dependencies=[Depends(service_auth)])
async def health():
    return {"ok": playwright is not None, "browser_running": browser is not None, "backend": active_backend}


@app.get("/state", dependencies=[Depends(service_auth)])
async def state(browser_id: str | None = None):
    expire_lease()
    async with lock:
        if browser_id and browser_id != active_browser:
            return {"browser_id": browser_id, "active": False, "lease": lease}
        if browser is None:
            return {
                "browser_id": active_browser,
                "backend": active_backend,
                "url": "about:blank",
                "title": "Browser ready",
                "lease": lease,
                "human_pending": human_pending,
                "headless": os.getenv("SHINBOT_HEADLESS", "0") == "1",
                "native_view": native_view(),
                "desktop": desktop.enabled(),
                "idle": True,
            }
        await current_page()
        return {
            "browser_id": active_browser,
            "backend": active_backend,
            "url": page.url,
            "title": await page.title(),
            "lease": lease,
            "human_pending": human_pending,
            "headless": os.getenv("SHINBOT_HEADLESS", "0") == "1",
            "native_view": native_view(),
            "desktop": desktop.enabled(),
        }


@app.get("/lease", dependencies=[Depends(service_auth)])
async def lease_state():
    # Polling must not wait behind an in-flight navigation or observation.
    expire_lease()
    return {
        "lease": lease,
        "human_pending": human_pending,
        "browser_id": active_browser,
        "backend": active_backend,
        "url": page.url if page is not None and not page.is_closed() else "about:blank",
        "headless": os.getenv("SHINBOT_HEADLESS", "0") == "1",
        "native_view": native_view(),
        "desktop": desktop.enabled(),
        "generation": control_generation,
        "idle": browser is None,
    }


@app.post("/select", dependencies=[Depends(service_auth)])
async def select_browser(payload: dict):
    global browser, page, active_browser, active_backend, control_generation
    async with lock:
        if lease and lease["kind"] == "agent":
            raise HTTPException(
                409,
                "Another bot is using the browser. Wait for it to finish or take control before switching profiles.",
            )
        if native_view() or desktop.enabled():
            profile = payload["browser_id"]
            if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", profile):
                raise HTTPException(400, "Invalid browser profile.")
            backend = payload.get("backend", active_backend)
            if backend not in BACKENDS:
                raise HTTPException(400, "Choose a supported browser in Settings → Browser.")
            if not lease and (profile != active_browser or backend != active_backend):
                await close_browser()
                active_browser, active_backend = profile, backend
                control_generation += 1
        else:
            await select_profile(payload["browser_id"], payload.get("backend"))
    return await state()


@app.post("/takeover", dependencies=[Depends(service_auth)])
async def takeover(payload: dict):
    async with desktop.access(human=True):
        result = await take_control(payload)
        desktop.set_owner(payload["owner"])
        return result


async def take_control(payload: dict):
    global human_pending, lease, control_generation, lease_touched, last_agent

    human_pending = True
    try:
        async with lock:
            control_generation += 1
            try:
                if not desktop.enabled():
                    await select_profile(payload.get("browser_id", active_browser), payload.get("backend"))
                if native_view():
                    await current_page()
            except BaseException:
                lease = None
                raise
            last_agent = None
            lease = {"kind": "human", "owner": payload["owner"]}
            lease_touched = time.monotonic()
    finally:
        human_pending = False
    return {"lease": lease, "browser_id": active_browser}


@app.post("/release", dependencies=[Depends(service_auth)])
async def release(payload: dict):
    global lease, human_pending, control_generation

    async with lock:
        if lease and lease.get("kind") == "human" and lease.get("owner") != payload.get("owner"):
            raise HTTPException(403, "Only the device controlling the computer can hand it back.")
        lease = None
        desktop.set_owner(None)
        human_pending = False
        control_generation += 1
    return {"released": True}


@app.get("/view", dependencies=[Depends(service_auth)])
async def view():
    # A headed viewer only needs VNC credentials. Watching must not queue behind
    # the agent's browser lock or open, switch, or focus its current page.
    if os.getenv("SHINBOT_HEADLESS", "0") != "1" and not native_view():
        return {"password": vnc_password, "screenshot": ""}
    if native_view():
        # Reading a preview never starts a browser or takes control from an agent.
        active = page
        if active is None or active.is_closed():
            return {"screenshot": "", "empty": True, "generation": control_generation}
        empty = active.url == "about:blank" and await active.evaluate(
            "!document.body || (!document.body.childElementCount && !document.body.textContent.trim())"
        )
        screenshot = (
            ""
            if empty
            else base64.b64encode(await active.screenshot(type="jpeg", quality=65, timeout=3000)).decode()
        )
        return {"screenshot": screenshot, "empty": bool(empty), "generation": control_generation}
    async with lock:
        await current_page()
        screenshot = ""
        if os.getenv("SHINBOT_HEADLESS", "0") == "1":
            screenshot = base64.b64encode(await page.screenshot(type="jpeg", quality=65)).decode()
        return {
            "password": vnc_password,
            "screenshot": screenshot,
        }


@app.post("/input", dependencies=[Depends(service_auth)])
async def human_input(payload: dict):
    global lease_touched
    if not native_view():
        raise HTTPException(404, "Direct input is available in the native browser view.")
    expire_lease()
    async with lock:
        if not lease or lease["kind"] != "human" or lease["owner"] != payload.get("owner"):
            raise HTTPException(423, "Take control before interacting with this browser.")
        if payload.get("generation") != control_generation:
            raise HTTPException(409, "Browser control changed. Refresh the preview before interacting.")
        if page is None or page.is_closed():
            raise HTTPException(409, "This tab closed. Hand back and take control to open a new tab.")
        event = payload.get("event", {})
        if not isinstance(event, dict):
            raise HTTPException(400, "Invalid browser input.")
        kind = event.get("type")

        def number(name, low, high):
            value = event.get(name)
            if not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise HTTPException(400, "Invalid browser input coordinates.")
            return value

        lease_touched = time.monotonic()
        if kind in {"click", "wheel"}:
            size = page.viewport_size or {"width": 1280, "height": 800}
            x, y = number("x", 0, size["width"]), number("y", 0, size["height"])
            if kind == "click":
                button = event.get("button", "left")
                if button not in {"left", "right", "middle"} or event.get("count", 1) not in {1, 2, 3}:
                    raise HTTPException(400, "Invalid browser click.")
                # Each DOM click already represents one click; preserve its double-click
                # count without replaying the preceding click a second time.
                await page.mouse.move(x, y)
                await page.mouse.down(button=button, click_count=event.get("count", 1))
                await page.mouse.up(button=button, click_count=event.get("count", 1))
            else:
                dx, dy = number("dx", -4000, 4000), number("dy", -4000, 4000)
                await page.mouse.move(x, y)
                await page.mouse.wheel(dx, dy)
        elif kind == "text":
            value = event.get("text")
            if not isinstance(value, str) or len(value) > 10000:
                raise HTTPException(400, "Enter up to 10,000 characters at a time.")
            await page.keyboard.insert_text(value)
        elif kind == "key":
            key = event.get("key", "")
            if not isinstance(key, str) or not re.fullmatch(
                r"(?:(?:Control|Meta|Alt|Shift)\+){0,4}(?:[a-zA-Z0-9]|Enter|Tab|Escape|Backspace|Delete|Arrow(?:Up|Down|Left|Right)|Home|End|PageUp|PageDown|Space)",
                key,
            ):
                raise HTTPException(400, "This key is not supported in the browser view.")
            await page.keyboard.press(key)
        elif kind == "navigate":
            url = event.get("url", "")
            parsed = urlparse(url) if isinstance(url, str) else None
            if (
                not parsed
                or len(url) > 8000
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise HTTPException(400, "Enter an HTTP or HTTPS website address without credentials.")
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        else:
            raise HTTPException(400, "Unsupported browser input.")
        return {"ok": True}


async def current_page():
    global page
    if browser is None:
        await select_profile(active_browser)
    if page is None or page.is_closed():
        page = next((p for p in browser.pages if not p.is_closed()), None) or await browser.new_page()
        page.set_default_timeout(15000)
    return page


async def observe():
    await current_page()
    text = f"URL: {page.url}\nTitle: {await page.title()}\n{(await page.locator('body').inner_text())[:16000]}\n\n"
    text += str(
        await page.locator("a,button,input,textarea,select").evaluate_all(
            "els => els.slice(0,80).map(e => ({tag:e.tagName,text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,100),id:e.id,name:e.getAttribute('name'),type:e.getAttribute('type')}))"
        )
    )
    content = [{"type": "text", "text": text}]
    try:
        content.append(
            {
                "type": "image",
                "data": base64.b64encode(await page.screenshot(type="jpeg", quality=60)).decode(),
                "mimeType": "image/jpeg",
            }
        )
    except Exception:
        content[0]["text"] += "\nScreenshot unavailable; the page text above is current."
    return {"content": content}


@app.post("/assess", dependencies=[Depends(service_auth)])
async def assess_action(payload: dict):
    expire_lease()
    async with lock:
        if human_pending or (lease and (lease["kind"] == "human" or lease["owner"] != payload["task_id"])):
            raise HTTPException(423, "The browser is in use. Wait before acting.")
        if payload["args"].get("operation") == "close":
            return await assess(page or SimpleNamespace(url="about:blank"), payload["args"])
        await select_profile(payload.get("browser_id", "shared"), payload.get("backend"))
        await current_page()
        try:
            return await assess(page, payload["args"])
        except Exception as error:
            raise HTTPException(
                400, "Could not inspect the target. Observe again and select exactly one current element."
            ) from error


@app.post("/execute", dependencies=[Depends(service_auth)])
async def execute(payload: dict):
    # Receipts remain readable while the owner has control; replay never performs an action.
    prior = journal.lookup(payload)
    if prior is not None:
        return prior
    async with desktop.access():
        return await execute_browser(payload)


async def execute_browser(payload: dict):
    global lease, control_generation, last_agent, lease_touched
    prior = journal.lookup(payload)
    if prior is not None:
        return prior
    expire_lease()
    if payload["task_id"] in cancelled:
        raise HTTPException(423, "Task cancelled before execution.")
    if human_pending or (lease and lease["kind"] == "human"):
        raise HTTPException(423, "The owner has control. Wait until they hand it back.")
    async with lock:
        if human_pending or (lease and lease["kind"] == "human"):
            raise HTTPException(423, "The owner has control.")
        if lease and lease["kind"] == "agent" and lease["owner"] != payload["task_id"]:
            raise HTTPException(423, "Another task owns the browser.")
        operation = payload["args"]["operation"]
        if (
            operation == "close"
            and browser is not None
            and (last_agent != payload["task_id"] or active_browser != payload.get("browser_id", "shared"))
        ):
            raise HTTPException(400, "This task does not own the open browser. It was left open.")
        if last_agent != payload["task_id"]:
            control_generation += 1
            last_agent = payload["task_id"]
        operation = payload["args"]["operation"]
        if operation != "close":
            await select_profile(payload.get("browser_id", "shared"), payload.get("backend"))
        if operation in {"click", "fill", "press", "back"} and observations.get(payload["task_id"]) != (
            control_generation,
            page.url,
        ):
            raise HTTPException(400, "Browser changed since observation. Observe again before acting.")
        args = payload["args"]
        locator = None
        # Validate selectors and actionability before recording an external effect.
        try:
            if operation == "goto":
                parsed = urlparse(args["url"])
                if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                    raise ValueError("Use an HTTP or HTTPS website address without embedded credentials.")
            elif operation in {"click", "fill", "press"}:
                locator = page.locator(
                    args.get("selector", "body") if operation == "press" else args["selector"]
                )
                if await locator.count() != 1:
                    raise ValueError(
                        "Selector must match exactly one element. Observe again and choose a precise selector."
                    )
                if operation == "click":
                    await locator.click(trial=True, timeout=5000)
                elif operation == "fill":
                    await locator.wait_for(state="visible", timeout=5000)
                    if not await locator.is_editable():
                        raise ValueError("This element cannot be edited. Observe again and select an input.")
                    if not isinstance(args["text"], str):
                        raise ValueError("Fill requires text.")
                elif not isinstance(args["key"], str):
                    raise ValueError("Press requires a key.")
            elif operation == "screenshot":
                file_name(args.get("name") or "browser-screenshot.png")
            elif operation not in {"back", "observe", "tabs", "close"}:
                raise ValueError("Unknown browser operation.")
        except Exception as error:
            raise HTTPException(400, f"Browser action did not run: {str(error)[:600]}") from error
        mode = payload.get("approval_mode", "auto")
        if mode not in {"auto", "ask", "full"}:
            raise HTTPException(400, "Unknown approval mode.")
        if mode != "full" and operation not in {"observe", "tabs", "screenshot"}:
            review = await assess(page or SimpleNamespace(url="about:blank"), args)
            required = mode == "ask" or not review["safe"]
            # Recheck the live target under the execution lock. A changed page cannot reuse consent.
            approved = payload.get("approval_context") == review["context"]
            if required and not approved:
                return {"status": "approval_required", **review}
        prior = journal.claim(payload)
        if prior is not None:
            return prior
        lease = {"kind": "agent", "owner": payload["task_id"]}
        lease_touched = time.monotonic()
        read_only = operation in {"observe", "tabs", "screenshot"}
        try:
            if operation == "screenshot":
                await current_page()
                result = {
                    "file": FileStore(DATA / "attachments").put(
                        await page.screenshot(type="png", timeout=15000),
                        args.get("name") or "browser-screenshot.png",
                    )
                }
                journal.finish(payload["id"], result)
                return result
            if operation == "close":
                await close_browser()
                lease = None
                result = {
                    "content": [
                        {"type": "text", "text": "Browser closed. Saved sign-ins and profile data are kept."}
                    ]
                }
                journal.finish(payload["id"], result)
                return result
            if backend_driver and operation in {"goto", "click", "fill", "press", "back"}:
                await backend_driver.execute(page, operation, args)
            else:
                await perform_action(operation, args, locator)
        except Exception as error:
            no_connection = {
                "ERR_NAME_NOT_RESOLVED",
                "ERR_CONNECTION_REFUSED",
                "ERR_ADDRESS_UNREACHABLE",
                "ERR_INVALID_URL",
                "ERR_UNSAFE_PORT",
            }
            if operation == "goto" and any(code in str(error) for code in no_connection):
                result = {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "This website could not be reached. Check its address or try another source.",
                        }
                    ],
                }
                journal.finish(payload["id"], result)
                return result
            # A timeout can follow a successful HTTP request; arbitrary GET URLs can have effects.
            journal.uncertain(payload["id"])
            raise
        except BaseException:
            journal.uncertain(payload["id"])
            raise
        # Persist success before reading the new page: an observation failure must not replay a click.
        result = {
            "content": [
                {
                    "type": "text",
                    "text": f"Browser {operation} completed. Observe the page for current details.",
                }
            ]
        }
        if read_only:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": "Browser observation was interrupted. Observe again."}],
            }
        journal.finish(payload["id"], result)
        try:
            result = (
                {"tabs": [{"url": p.url} for p in browser.pages]} if operation == "tabs" else await observe()
            )
            observations[payload["task_id"]] = (control_generation, page.url)
        except Exception as error:
            if read_only:
                result = {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "Could not observe the browser. Wait for navigation to settle and observe again. "
                            + str(error)[:300],
                        }
                    ],
                }
            else:
                observations.pop(payload["task_id"], None)
        journal.finish(payload["id"], result)
        return result


@app.post("/desktop/open", dependencies=[Depends(service_auth)])
async def desktop_open(payload: dict):
    if not desktop.enabled():
        raise HTTPException(404, "A desktop backend is not configured.")
    async with desktop.access(human=True):
        async with lock:
            if lease != {"kind": "human", "owner": payload.get("owner")}:
                raise HTTPException(403, "Take control before opening an application.")
            args = {"operation": "launch", "application": payload.get("application")}
            try:
                desktop.validate(args)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if args["application"] == "browser":
                await select_profile(active_browser, payload.get("backend", active_backend))
                await (await current_page()).bring_to_front()
            else:
                await desktop.perform(args)
    return {"opened": args["application"]}


async def open_desktop_browser(url=None):
    """Handle a desktop shortcut without granting a new lease or exposing service tokens."""
    global lease_touched, control_generation, page
    expire_lease()
    async with desktop.access(human=True):
        async with lock:
            if not lease or lease["kind"] != "human" or desktop.owner() != lease["owner"]:
                raise HTTPException(
                    423, "Take control in ShinBot’s Computer view before opening a browser from the desktop."
                )
            await select_profile(active_browser, active_backend)
            if url:
                page = await browser.new_page()
                page.set_default_timeout(15000)
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await (await current_page()).bring_to_front()
            lease_touched = time.monotonic()
            observations.clear()
            control_generation += 1


@app.post("/desktop/execute", dependencies=[Depends(service_auth)])
async def execute_desktop(payload: dict):
    global lease, lease_touched, last_agent, control_generation
    if not desktop.enabled():
        raise HTTPException(400, "A desktop backend is not configured. Use the browser and file tools.")
    prior = journal.lookup(payload)
    if prior is not None:
        return prior
    args = payload["args"]
    try:
        desktop.validate(args)
        if args["operation"] == "screenshot":
            file_name(args.get("name") or "desktop.png")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    expire_lease()
    async with desktop.access():
        async with lock:
            if (
                payload["task_id"] in cancelled
                or human_pending
                or (lease and lease != {"kind": "agent", "owner": payload["task_id"]})
            ):
                raise HTTPException(423, "The computer is in use. Wait until it is handed back.")
            operation = args["operation"]
            if (
                operation not in {"observe", "screenshot", "launch"}
                and observations.get("desktop:" + payload["task_id"]) != control_generation
            ):
                raise HTTPException(400, "Desktop changed. Observe it again before interacting.")
            if (
                operation not in {"observe", "screenshot"}
                and payload.get("approval_mode") != "full"
                and not payload.get("desktop_approved")
            ):
                raise HTTPException(403, "Desktop interactions require approval in Auto and Ask modes.")
            prior = journal.claim(payload)
            if prior is not None:
                return prior
            lease = {"kind": "agent", "owner": payload["task_id"]}
            last_agent, lease_touched = payload["task_id"], time.monotonic()
            try:
                if operation == "screenshot":
                    result = {
                        "file": FileStore(DATA / "attachments").put(
                            await desktop.capture(), args.get("name") or "desktop.png"
                        )
                    }
                else:
                    if operation == "launch" and args["application"] == "browser":
                        await select_profile(payload.get("browser_id", "shared"), payload.get("backend"))
                    elif operation != "observe":
                        await desktop.perform(args)
                    control_generation += operation != "observe"
                    # Record the input before observing; a capture failure must never replay a click.
                    result = {
                        "content": [
                            {"type": "text", "text": "Desktop action completed. Observe for current details."}
                        ]
                    }
                    journal.finish(payload["id"], result)
                    try:
                        result = await desktop.observe()
                        observations["desktop:" + payload["task_id"]] = control_generation
                    except Exception:
                        if operation == "observe":
                            result = {
                                "isError": True,
                                "content": [
                                    {"type": "text", "text": "Desktop capture failed. Try observing again."}
                                ],
                            }
                journal.finish(payload["id"], result)
                return result
            except BaseException:
                journal.uncertain(payload["id"])
                raise


async def perform_action(operation, args, locator):
    match operation:
        case "goto":
            await page.goto(args["url"], wait_until="domcontentloaded", timeout=30000)
        case "click":
            await locator.click()
        case "fill":
            await locator.fill(args["text"])
        case "press":
            await locator.press(args["key"])
        case "back":
            await page.go_back(wait_until="domcontentloaded")


@app.post("/agent/release", dependencies=[Depends(service_auth)])
async def release_agent(payload: dict):
    global lease
    owner = {"kind": "agent", "owner": payload["task_id"]}
    async with lock:
        owns_browser = lease == owner or (lease is None and last_agent == payload["task_id"])
        if owns_browser and not human_pending:
            if payload.get("close", False):
                await close_browser()
            lease = None
    return {"released": True, "closed": browser is None}


@app.post("/cancel", dependencies=[Depends(service_auth)])
async def cancel(payload: dict):
    cancelled.add(payload["task_id"])
    await release_agent(payload)
    # An in-flight website effect cannot be undone. Let the current operation settle.
    return {"cancelled": True, "in_flight": lock.locked()}


@app.websocket("/vnc")
async def vnc(websocket: WebSocket):
    global lease_touched
    token = os.getenv("SHINBOT_EXECUTOR_TOKEN", "")
    if len(token) < 32 or not hmac.compare_digest(
        websocket.headers.get("authorization", ""), f"Bearer {token}"
    ):
        return await websocket.close(code=1008)
    mode = websocket.query_params.get("mode", "view")
    owner = websocket.headers.get("x-shinbot-owner")
    if mode == "control" and (not lease or lease != {"kind": "human", "owner": owner}):
        return await websocket.close(code=1008)
    await websocket.accept()
    port = 6081 if mode == "control" else 6080
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=4_000_000) as upstream:

            async def receive():
                while True:
                    data = await websocket.receive_bytes()
                    if mode == "control" and lease != {"kind": "human", "owner": owner}:
                        return
                    await upstream.send(data)

            async def send():
                async for data in upstream:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)

            async def fence():
                global lease_touched
                while True:
                    if mode == "control" and lease != {"kind": "human", "owner": owner}:
                        return
                    if mode == "control":
                        lease_touched = time.monotonic()
                    await asyncio.sleep(0.1)

            tasks = [asyncio.create_task(f()) for f in (receive, send, fence)]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
