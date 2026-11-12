import asyncio
import fcntl
import json
import mimetypes
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import websockets
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import __version__
from .auth import digest, hash_password, require_service, verify_password
from .bots import BOT_COLORS
from .chat_options import ApprovalMode, ChatOptions, ThinkingLevel
from .config import Config
from .core import Core
from .db import packed, uid
from .jobs import schedule_preview, time_zone
from .notifications import ReadNotifications


class Message(BaseModel):
    bot_id: str = Field(default="default", max_length=64)
    content: str = Field(min_length=1, max_length=20000)
    request_id: str = Field(default_factory=uid, min_length=1, max_length=128)
    options: ChatOptions | None = None
    mode: Literal["queue", "steer"] = "queue"


class PromptSuggestion(BaseModel):
    bot_id: str = Field(default="default", max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=150)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="ShinBot", min_length=1, max_length=50)
    timezone: str = "UTC"
    provider: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=150)
    daily_tokens: int = Field(default=0, ge=0, le=100000000)
    task_tokens: int = Field(default=0, ge=0, le=10000000)
    computer_auto: bool = False
    browser_auto: bool = False
    browser_backend: Literal["chromium", "camoufox", "agent-browser"] = "camoufox"
    browser_close_on_finish: bool = True
    approval_mode: ApprovalMode = "auto"
    thinking_level: ThinkingLevel = "off"
    extensions_auto: bool = False
    learn_from_tasks: bool = True
    learn_preferences: bool = True
    max_active_runs: int = Field(default=4, ge=1, le=16)

    @model_validator(mode="after")
    def valid_choices(self):
        if bool(self.provider.strip()) != bool(self.model.strip()):
            raise ValueError("Choose both a provider and a model, or leave both unselected.")
        for limit in (self.task_tokens, self.daily_tokens):
            if 0 < limit < 1000:
                raise ValueError("Use at least 1,000 tokens for a cap, or 0 for no limit.")
        return self


def create_app(config: Config | None = None):
    config = config or Config()
    core = Core(config)

    @asynccontextmanager
    async def lifespan(app):
        owner = (config.data / "control.lock").open("a")
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner.close()
            raise RuntimeError("Another ShinBot control process owns this data directory.")
        core.recover()
        worker = asyncio.create_task(core.coordinator.run()) if config.coordinate else None
        releases = asyncio.create_task(core.updates.run()) if config.coordinate else None
        yield
        if releases:
            releases.cancel()
            with suppress(asyncio.CancelledError):
                await releases
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        for task in list(core.running_actions.values()):
            task.cancel()
        await asyncio.gather(*list(core.running_actions.values()), return_exceptions=True)
        await core.close()
        owner.close()

    app = FastAPI(title="ShinBot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.core = core

    @app.middleware("http")
    async def protection(request, call_next):
        host = request.headers.get("host", "")
        valid = {urlparse(config.origin).netloc, "control:8765", "127.0.0.1:8765"}
        origin = urlparse(config.origin)
        if origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1"}:
            valid.add(f"127.0.0.1:{origin.port or 80}")
        if host not in valid:
            return JSONResponse({"detail": "Untrusted host."}, status_code=400)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid content length."}, status_code=400)
        if length > 3_000_000:
            return JSONResponse({"detail": "Request exceeds 3 MB."}, status_code=413)
        if request.method in {"POST", "PUT", "PATCH"}:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > 3_000_000:
                    return JSONResponse({"detail": "Request exceeds 3 MB."}, status_code=413)
            request._body = bytes(content)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if re.fullmatch(r"/api/tasks/[^/]+/attachments/[^/]+", request.url.path):
            response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        fields = ", ".join(
            ".".join(str(p) for p in error["loc"] if p != "body") for error in exc.errors()[:4]
        )
        return JSONResponse(
            {"detail": f"Check {fields or 'the form fields'} and try again."}, status_code=422
        )

    @app.exception_handler(KeyError)
    async def missing_field(request, exc):
        return JSONResponse({"detail": "Missing or invalid request field."}, status_code=400)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)[:1000]}, status_code=400)

    @app.exception_handler(ValidationError)
    async def invalid_value(request, exc):
        issues = []
        for error in exc.errors(include_input=False, include_url=False)[:4]:
            field = ".".join(map(str, error["loc"]))
            message = error["msg"].removeprefix("Value error, ")
            issues.append(f"{field}: {message}" if field else message)
        return JSONResponse({"detail": "; ".join(issues)[:1000]}, status_code=400)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                body = exc.response.json()
            except ValueError:
                body = None
            reason = (body.get("error") or body.get("detail")) if isinstance(body, dict) else None
            return JSONResponse(
                {
                    "detail": reason
                    if isinstance(reason, str) and reason
                    else "A service rejected the request.",
                    "upstream_status": exc.response.status_code,
                },
                status_code=502,
            )
        return JSONResponse(
            {"detail": "A ShinBot service is unavailable. Check its status in Settings."}, status_code=503
        )

    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/welcome")
    async def welcome():
        # Only bundled public recipes; never return user profiles or a private repository before authentication.
        directory = Path(__file__).parent / "bundled" / "templates" / "templates"
        import json

        return {"templates": [json.loads(p.read_text()) for p in sorted(directory.glob("*/template.json"))]}

    @app.get("/auth/status")
    async def auth_status(request: Request):
        try:
            core.auth.require(request)
            authenticated = True
        except HTTPException:
            authenticated = False
        return {"claimed": core.db.setting("password") is not None, "authenticated": authenticated}

    @app.get("/auth/callback")
    async def provider_callback(request: Request):
        try:
            callback = await core.provider_login.callback(request.url.query)
            result = {"result": "received", "id": callback["id"]}
        except (ValueError, httpx.HTTPError):
            result = {"result": "failed"}
        return RedirectResponse(config.origin + "/auth/complete?" + urlencode(result), status_code=303)

    @app.get("/auth/complete")
    async def provider_complete():
        return FileResponse(Path(__file__).parent / "web" / "auth-complete.html")

    @app.post("/auth/github/pair")
    async def github_pair_complete(request: Request, payload: dict):
        code = request.headers.get("authorization", "").removeprefix("Bearer ")
        return await core.github.complete(code, payload.get("token"))

    @app.post("/auth/chatgpt-bridge/{operation}")
    async def chatgpt_bridge(operation: str, request: Request, payload: dict):
        code = request.headers.get("authorization", "").removeprefix("Bearer ")
        if operation == "start":
            return await core.provider_login.bridge_start(code, payload.get("redirect_uri"))
        if operation == "status":
            return await core.provider_login.bridge_progress(code)
        if operation == "callback":
            query = payload.get("query")
            if not isinstance(query, str):
                raise ValueError("Invalid callback.")
            return await core.provider_login.bridge_callback(code, query)
        if operation == "cancel":
            bridge = core.provider_login.require_bridge(code)
            return await core.provider_login.respond({"id": bridge["id"], "cancel": True})
        raise HTTPException(404)

    @app.post("/auth/login")
    async def login(request: Request, payload: dict):
        if request.headers.get("origin") not in {None, config.origin}:
            raise HTTPException(403, f"Open ShinBot at {config.origin} to sign in.")
        core.auth.rate_limit(request.client.host)
        password = payload.get("password", "")
        if not isinstance(password, str) or len(password) > 256:
            raise HTTPException(400, "Invalid password.")
        current = core.db.setting("password")
        if current is None:
            import hmac

            if not core.auth.bootstrap or not hmac.compare_digest(
                str(payload.get("bootstrap", "")), core.auth.bootstrap
            ):
                raise HTTPException(401, "Enter the installation's bootstrap token.")
            encoded = await asyncio.to_thread(hash_password, password)
            zone = payload.get("timezone", "UTC")
            try:
                time_zone(zone)
            except (ValueError, TypeError):
                raise HTTPException(400, "Choose a valid timezone, such as Asia/Kolkata.")
            with core.db.transaction() as db:
                if db.execute("SELECT 1 FROM settings WHERE key='password'").fetchone():
                    raise HTTPException(409, "This installation was already claimed.")
                db.execute("INSERT INTO settings VALUES ('password',?)", (packed(encoded),))
                preferences = json.loads(
                    db.execute("SELECT value FROM settings WHERE key='preferences'").fetchone()[0]
                )
                preferences["timezone"] = zone
                db.execute("UPDATE settings SET value=? WHERE key='preferences'", (packed(preferences),))
        elif not await asyncio.to_thread(verify_password, password, current):
            raise HTTPException(401, "Incorrect password.")
        cli = payload.get("cli") is True
        token = core.auth.session("cli" if cli else "browser")
        response = JSONResponse({"ok": True, **({"token": token} if cli else {})})
        if not cli:
            response.set_cookie(
                "shinbot_session",
                token,
                httponly=True,
                secure=config.origin.startswith("https://"),
                samesite="strict",
                max_age=86400 * 7,
            )
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(core.auth.require)])

    @api.post("/logout")
    async def logout(request: Request):
        core.db.execute("DELETE FROM sessions WHERE hash=?", (digest(core.auth.require(request)),))
        response = JSONResponse({"ok": True})
        response.delete_cookie("shinbot_session")
        return response

    @api.post("/sessions/revoke")
    async def revoke():
        core.db.execute("DELETE FROM sessions")
        return {"ok": True}

    @api.get("/status")
    async def status():
        return {
            "version": __version__,
            "runtime_ready": core.coordinator.ready,
            "runtime_error": core.coordinator.last_error,
            "safe_mode": config.safe_mode,
            "restore_hold": core.db.setting("restore_hold", False),
            "preferences": core.db.setting("preferences"),
            "marketplace": "adarshdigievo/shinbot-official-plugins",
            "updates": core.updates.snapshot(),
        }

    @api.get("/updates")
    async def updates():
        return core.updates.snapshot()

    @api.post("/updates/check")
    async def check_updates():
        return await core.updates.check(force=True)

    @api.put("/updates/settings")
    async def update_check_settings(payload: dict):
        if not isinstance(payload.get("automatic"), bool):
            raise HTTPException(400, "Choose whether to check for updates automatically.")
        core.db.set("release_checks_enabled", payload["automatic"])
        return core.updates.snapshot()

    @api.get("/settings")
    async def settings():
        return core.db.setting("preferences")

    @api.put("/settings")
    async def put_settings(preferences: Preferences):
        time_zone(preferences.timezone)
        if "browser_backend" not in preferences.model_fields_set:
            preferences.browser_backend = core.db.setting("preferences").get("browser_backend", "chromium")
        if preferences.browser_backend != core.db.setting("preferences").get("browser_backend", "chromium"):
            availability = await core.remote("browser", "/backends", method="GET")
            selected = next(
                (b for b in availability["backends"] if b["id"] == preferences.browser_backend), None
            )
            if not selected or not selected["ready"]:
                raise HTTPException(
                    400,
                    "Install this browser on the browser service host before selecting it. See Settings → Browser.",
                )
        core.db.set("preferences", preferences.model_dump())
        return preferences

    @api.get("/tasks/{task_id}/attachments/{attachment_id}")
    async def download_attachment(task_id: str, attachment_id: str, download: bool = False):
        row = core.db.one(
            "SELECT f.* FROM attachments f JOIN actions a ON a.id=f.action_id "
            "WHERE f.task_id=? AND f.id=? AND a.status='completed'",
            (task_id, attachment_id),
        )
        if not row:
            raise HTTPException(404, "Attachment not found.")
        path = core.attachments.files.path(row["digest"])
        if not path.is_file():
            raise HTTPException(404, "The attachment file is missing. Ask the bot to attach it again.")
        inline = bool(row["preview"]) and not download
        return FileResponse(
            path,
            filename=row["name"],
            media_type=row["mime_type"] if inline else "application/octet-stream",
            content_disposition_type="inline" if inline else "attachment",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @api.post("/recovery/release")
    async def release_recovery(payload: dict):
        if payload.get("old_installation_stopped") is not True:
            raise HTTPException(
                400, "Confirm the old installation is stopped before releasing this installation."
            )
        # Reconcile while held first: Pi must abort old execution before the new epoch can run.
        await core.coordinator.reconcile()
        with core.db.transaction() as db:
            db.execute(
                "UPDATE messages SET status='cancelled' WHERE status IN ('queued','dispatching','running') AND task_id IN (SELECT id FROM tasks WHERE status IN ('paused','uncertain'))"
            )
            db.execute(
                "UPDATE tasks SET status='failed',error='Restored work was stopped. Start a new task.' WHERE status='paused'"
            )
        core.db.set("restore_hold", False)
        await core.coordinator.reconcile()
        return {"released": True, "schedules": "paused"}

    @api.get("/bots")
    async def bots():
        return {
            "bots": core.bots.list(),
            "groups": core.db.all("SELECT * FROM bot_groups ORDER BY created"),
            "colors": BOT_COLORS,
        }

    @api.post("/bot-groups")
    async def create_group(payload: dict):
        name = payload.get("name", "").strip()
        if not name or len(name) > 100:
            raise ValueError("Give the group a name of 1–100 characters.")
        key = uid()
        core.db.execute("INSERT INTO bot_groups VALUES (?,?,?)", (key, name, time.time()))
        return {"id": key, "name": name}

    @api.post("/bots")
    async def save_bot(payload: dict):
        return core.bots.save(payload["profile"], payload.get("id"), payload.get("group_id"))

    @api.delete("/bots/{bot_id}")
    async def archive_bot(bot_id: str):
        core.bots.archive(bot_id)
        return {"ok": True}

    @api.get("/bots/{bot_id}/export")
    async def export_bot(bot_id: str):
        return core.bots.export(bot_id=bot_id)

    @api.get("/bot-groups/{group_id}/export")
    async def export_group(group_id: str):
        return core.bots.export(group_id=group_id)

    @api.get("/templates")
    async def templates():
        return core.bots.templates()

    @api.post("/templates/repositories")
    async def add_template_repository(payload: dict):
        revision, files = await core.plugins.repository(payload["source"], payload.get("ref", "HEAD"))
        return core.bots.catalog(payload["source"], revision, files)

    @api.post("/templates/install")
    async def install_template(payload: dict):
        return await asyncio.to_thread(
            core.bots.install,
            payload["template"],
            payload["request_id"],
            payload.get("allow_shared_browser") is True,
        )

    @api.get("/tasks")
    async def tasks():
        return core.db.all("SELECT * FROM tasks ORDER BY updated DESC LIMIT 200")

    @api.get("/tasks/search")
    async def search_tasks(
        q: str = Query(default="", max_length=200),
        bot_id: str | None = Query(default=None, max_length=64),
        offset: int = Query(default=0, ge=0, le=1000000),
    ):
        return await asyncio.to_thread(core.tasks.search, q, bot_id, offset)

    @api.post("/prompt-suggestion")
    async def prompt_suggestion(payload: PromptSuggestion):
        return await core.suggestions.get(**payload.model_dump())

    @api.post("/tasks")
    async def create_task(message: Message):
        if config.safe_mode or core.db.setting("restore_hold", False):
            raise HTTPException(409, "Resolve the recovery hold before starting work.")
        options = message.options.model_dump(exclude_none=True) if message.options else None
        if options:
            await validate_chat_options(options, bot_id=message.bot_id)
        result = await asyncio.to_thread(
            core.tasks.create_request, message.content, message.request_id, message.bot_id, options
        )
        core.coordinator.wake.set()
        return result

    async def validate_chat_options(options, task=None, bot_id="default"):
        if not (set(options) - {"approval_mode"}):
            return
        defaults = core.db.setting("preferences")
        profile = core.bots.get(bot_id)["profile"] if task is None else {}
        provider = (
            options.get("provider")
            or (task or {}).get("provider")
            or profile.get("provider")
            or defaults["provider"]
        )
        model_id = (
            options.get("model") or (task or {}).get("model") or profile.get("model") or defaults["model"]
        )
        providers = await core.coordinator.request("GET", "/v1/providers")
        provider_info = next((p for p in providers if p["id"] == provider and p["configured"]), None)
        model = next((m for m in (provider_info or {}).get("models", []) if m["id"] == model_id), None)
        if not model:
            raise ValueError("Choose a connected model, or add a connection in Settings.")
        levels = model.get("thinkingLevels", ["off"])
        effort = options.get("thinking_level", (task or defaults).get("thinking_level", "off"))
        if effort not in levels:
            if "thinking_level" in options:
                raise ValueError(
                    "This model does not support that thinking effort. Choose one of its available levels."
                )
            options["thinking_level"] = levels[0]

    @api.patch("/tasks/{task_id}/options")
    async def task_options(task_id: str, choices: ChatOptions):
        task = core.tasks.detail(task_id)
        values = choices.model_dump(exclude_none=True)
        await validate_chat_options(values, task=task)
        result = await asyncio.to_thread(core.tasks.options, task_id, values)
        core.coordinator.wake.set()
        return result

    @api.get("/tasks/{task_id}")
    async def task(task_id: str):
        detail = core.tasks.detail(task_id)
        if config.coordinate:
            try:
                # Manual compaction runs independently of the conversation's message status.
                detail["progress"] = await core.coordinator.request(
                    "GET", f"/v1/tasks/{task_id}/progress", timeout=2.0
                )
            except httpx.HTTPError:
                pass
        return detail

    @api.post("/tasks/{task_id}/messages")
    async def message(task_id: str, message: Message):
        result = await asyncio.to_thread(
            core.tasks.message, task_id, message.content, message.request_id, message.mode
        )
        core.coordinator.wake.set()
        return result

    @api.post("/tasks/{task_id}/messages/{message_id}/steer")
    async def steer_message(task_id: str, message_id: str):
        result = await asyncio.to_thread(core.tasks.steer, task_id, message_id)
        core.coordinator.wake.set()
        return result

    @api.get("/tasks/{task_id}/snapshot")
    async def snapshot(task_id: str):
        return await core.coordinator.request("GET", f"/v1/tasks/{task_id}/snapshot")

    @api.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        await core.cancel(task_id)
        return {"cancelled": True}

    @api.post("/tasks/{task_id}/compact")
    async def compact(task_id: str):
        return await core.coordinator.request("POST", f"/v1/tasks/{task_id}/compact", json={})

    @api.get("/notifications")
    async def notifications():
        return core.notifications.snapshot()

    @api.post("/notifications/read")
    async def read_notifications(payload: ReadNotifications):
        return core.notifications.read(payload)

    @api.get("/events")
    async def events(request: Request):
        async def stream():
            previous = None
            while not await request.is_disconnected():
                try:
                    core.auth.require(request)
                except HTTPException:
                    break
                data = {
                    "tasks": core.db.all(
                        "SELECT id,title,status,updated,bot_id FROM tasks ORDER BY updated DESC LIMIT 100"
                    ),
                    "approvals": core.db.all(
                        "SELECT id,task_id,tool,args,status,expires FROM actions WHERE status IN ('pending','uncertain') ORDER BY created"
                    ),
                    "events": core.db.all("SELECT * FROM events ORDER BY id DESC LIMIT 30"),
                    "notifications": core.notifications.snapshot(),
                }
                encoded = packed(data)
                if encoded != previous:
                    yield f"event: snapshot\ndata: {encoded}\n\n"
                    previous = encoded
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @api.post("/actions/{action_id}/decision")
    async def decision(action_id: str, payload: dict):
        try:
            core.approvals.decide(action_id, payload.get("approved") is True, payload.get("scope", "once"))
        except ValueError as error:
            raise HTTPException(409, str(error))
        return {"ok": True}

    @api.get("/approvals")
    async def approval_rules():
        return core.approvals.list()

    @api.delete("/approvals/{rule_id}")
    async def revoke_approval(rule_id: str):
        core.approvals.revoke(rule_id)
        return {"ok": True}

    @api.post("/actions/{action_id}/resolve")
    async def resolve(action_id: str, payload: dict):
        action = core.db.one("SELECT * FROM actions WHERE id=? AND status='uncertain'", (action_id,))
        if not action or payload.get("outcome") not in {"completed", "not_completed"}:
            raise HTTPException(400, "Inspect the destination and supply the observed outcome.")
        core.db.execute(
            "UPDATE actions SET status='failed',error=? WHERE id=?",
            (f"Owner reconciled: {payload['outcome']}. This action will not be dispatched again.", action_id),
        )
        if not core.db.one(
            "SELECT 1 FROM actions WHERE task_id=? AND status='uncertain'", (action["task_id"],)
        ):
            core.db.execute(
                "UPDATE tasks SET status='failed',error='Owner reconciled interrupted work.' WHERE id=?",
                (action["task_id"],),
            )
        return {"resolved": True}

    @api.get("/notes")
    async def notes(q: str = ""):
        return core.notebook.search(q)

    @api.post("/notes")
    async def save_note(payload: dict):
        return core.notebook.save(
            payload["title"], payload["body"], payload.get("id"), payload.get("revision")
        )

    @api.delete("/notes/{note_id}")
    async def delete_note(note_id: str):
        core.notebook.delete(note_id)
        return {"ok": True}

    @api.get("/memories")
    async def memories(q: str = "", bot_id: str = "default"):
        core.bots.get(bot_id)
        return core.notebook.memories(q, bot_id)

    @api.post("/memories")
    async def remember(payload: dict):
        core.bots.get(payload.get("bot_id", "default"))
        return core.notebook.remember(payload["text"], bot_id=payload.get("bot_id", "default"))

    @api.delete("/memories/{memory_id}")
    async def forget(memory_id: str, bot_id: str = "default"):
        core.notebook.forget(memory_id, bot_id)
        return {"ok": True}

    @api.get("/jobs")
    async def jobs(archived: bool = False):
        return core.db.all("SELECT * FROM schedules WHERE archived=? ORDER BY created DESC", (int(archived),))

    @api.get("/board")
    async def board(bot_id: str | None = None):
        require_board()
        return core.board.list(bot_id)

    @api.post("/board")
    async def save_card(payload: dict):
        require_board()
        return core.board.save(payload, payload.get("id"))

    @api.get("/board/{card_id}/runs")
    async def card_runs(card_id: str):
        require_board()
        return core.board.history(card_id)

    @api.post("/board/{card_id}/move")
    async def move_card(card_id: str, payload: dict):
        require_board()
        if payload["status"] == "ready" and (config.safe_mode or core.db.setting("restore_hold", False)):
            raise HTTPException(409, "Resolve the recovery hold before starting work.")
        old = core.board.get(card_id)
        result = core.board.move(card_id, payload["status"], payload["revision"])
        if old["status"] == "running" and payload["status"] == "blocked":
            await core.cancel(old["task_id"])
        core.coordinator.wake.set()
        return result

    def require_board():
        if not core.db.one("SELECT 1 FROM plugins WHERE id='kanban' AND status='active'"):
            raise HTTPException(409, "Install and activate Kanban in Plugins to open the board.")

    @api.get("/secrets")
    async def secrets_summary():
        plugins = []
        for plugin in core.plugins.list():
            if any(field.get("secret") for field in plugin.get("configuration", [])):
                fields = core.plugins.configuration(plugin["id"], plugin["version"])["fields"]
                plugins.append(
                    {
                        "id": plugin["id"],
                        "version": plugin["version"],
                        "name": plugin["name"],
                        "fields": [
                            {"label": f["label"], "configured": f["configured"]}
                            for f in fields
                            if f["secret"]
                        ],
                    }
                )
        return {
            "plugins": plugins,
            "github": bool(core.plugins.vault.read("github-inbox").get("token")),
            "calendar": bool(core.plugins.vault.read("google-calendar").get("refresh_token")),
        }

    @api.get("/github/account")
    async def github_account():
        return core.github.account()

    @api.post("/jobs/preview")
    async def preview_schedule(payload: dict):
        return schedule_preview(payload)

    @api.post("/github/account")
    async def github_account_connect(payload: dict):
        return await core.github.connect(payload.get("token"))

    @api.delete("/github/account")
    async def github_account_disconnect():
        return core.github.disconnect()

    @api.post("/github/pair")
    async def github_pair():
        return core.github.start()

    @api.get("/github/pair/{pair_id}")
    async def github_pair_progress(pair_id: str):
        return core.github.progress(pair_id)

    @api.delete("/github/pair/{pair_id}")
    async def github_pair_cancel(pair_id: str):
        core.github.cancel(pair_id)
        return {"ok": True}

    @api.get("/github/discover")
    async def github_discover(kind: str = "repositories", page: int = 1):
        return await core.github.discover(kind, page)

    @api.post("/github/select")
    async def github_select(payload: dict):
        return core.github.select(payload)

    @api.get("/github-inbox")
    async def github_inbox(view: str = "attention"):
        return core.inbox.snapshot(view)

    @api.put("/github-inbox/connection")
    async def github_connection(payload: dict):
        return core.inbox.configuration(payload)

    @api.post("/github-inbox/sync")
    async def sync_github():
        return await core.inbox.sync(force=True)

    @api.get("/github-inbox/items/{item_id:path}")
    async def github_item(item_id: str):
        return await core.inbox.detail(item_id)

    @api.put("/github-inbox/items/{item_id:path}/state")
    async def github_item_state(item_id: str, payload: dict):
        return core.inbox.state(item_id, payload)

    @api.post("/github-inbox/items/{item_id:path}/work")
    async def github_issue_work(item_id: str, payload: dict):
        try:
            if not core.db.one(
                "SELECT 1 FROM issue_links WHERE id=?", ("issue:" + str(payload.get("request_id", "")),)
            ):
                await core.inbox.detail(item_id)
            return core.inbox.work(item_id, payload)
        except ValueError as exc:
            stored = bool(
                core.db.one(
                    "SELECT 1 FROM issue_links WHERE id=?", ("issue:" + str(payload.get("request_id", "")),)
                )
            )
            return JSONResponse({"detail": str(exc)[:1000], "request_stored": stored}, status_code=400)

    @api.get("/calendar/settings")
    async def calendar_settings():
        return core.calendar.settings()

    @api.put("/calendar/settings")
    async def save_calendar_settings(payload: dict):
        return core.calendar.settings(payload)

    @api.post("/calendar/connect")
    async def connect_calendar(request: Request):
        result = core.calendar.begin(core.auth.require(request))
        response = JSONResponse(result)
        state = parse_qs(urlparse(result["url"]).query)["state"][0]
        response.set_cookie(
            "shinbot_calendar_oauth",
            state,
            httponly=True,
            secure=config.origin.startswith("https://"),
            samesite="lax",
            max_age=600,
            path="/api/calendar/oauth/callback",
        )
        return response

    @app.get("/api/calendar/oauth/callback")
    async def calendar_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        try:
            await core.calendar.finish_browser(
                request.cookies.get("shinbot_calendar_oauth", ""), state, code, error
            )
        except (ValueError, httpx.HTTPError):
            result = "failed"
        else:
            result = "connected"
        response = RedirectResponse("/?calendar=" + result, status_code=303)
        response.delete_cookie("shinbot_calendar_oauth", path="/api/calendar/oauth/callback")
        return response

    @api.post("/calendar/disconnect")
    async def disconnect_calendar():
        return core.calendar.disconnect()

    @api.get("/calendar/calendars")
    async def connected_calendars():
        return await core.calendar.calendars()

    @api.get("/calendar/agenda")
    async def calendar_agenda(start: str, end: str):
        return await core.calendar.agenda(start, end)

    @api.get("/projects")
    async def projects():
        return {"projects": core.projects.list()}

    @api.get("/projects/folders")
    async def project_folders(path: str = "."):
        core.projects.require()
        return await core.projects.computer("browse", path=path)

    @api.post("/projects")
    async def add_project(payload: dict):
        return await core.projects.add(payload["path"])

    @api.get("/projects/{project_id}/workspaces")
    async def project_workspaces(project_id: str):
        return core.projects.workspaces(project_id)

    @api.post("/projects/{project_id}/tasks")
    async def project_task(project_id: str, payload: dict):
        try:
            return await core.projects.start(project_id, payload)
        except (ValueError, httpx.HTTPError) as exc:
            saved = core.db.one(
                "SELECT 1 FROM project_workspaces WHERE request_id=?",
                (payload.get("request_id", "") if isinstance(payload.get("request_id"), str) else "",),
            )
            if isinstance(exc, httpx.HTTPError):
                response = await upstream_error(None, exc)
                return JSONResponse(
                    {**json.loads(response.body), "request_stored": bool(saved)},
                    status_code=response.status_code,
                )
            return JSONResponse({"detail": str(exc)[:1000], "request_stored": bool(saved)}, status_code=400)

    @api.get("/project-workspaces/{workspace_id}/{operation}")
    async def project_inspect(workspace_id: str, operation: str, file: str = "."):
        if operation not in {"changes", "files", "read"}:
            raise HTTPException(404, "Choose changes or files for this worktree.")
        return await core.projects.inspect_workspace(workspace_id, operation, file)

    @api.post("/project-workspaces/{workspace_id}/archive")
    async def archive_workspace(workspace_id: str):
        return await core.projects.archive(workspace_id)

    @api.post("/project-workspaces/{workspace_id}/retry")
    async def retry_workspace(workspace_id: str):
        core.projects.require()
        row = core.projects.workspace(workspace_id)
        parent = (
            core.db.one("SELECT * FROM tasks WHERE id=?", (row["parent_task_id"],))
            if row["parent_task_id"]
            else None
        )
        return await core.projects.start(
            row["project_id"],
            {
                "prompt": row["prompt"],
                "bot_id": row["bot_id"],
                "title": row["title"],
                "request_id": row["request_id"],
            },
            parent,
        )

    @api.post("/jobs")
    async def save_job(payload: dict):
        return core.jobs.save(payload, payload.get("id"))

    @api.post("/jobs/{job_id}/toggle")
    async def toggle_job(job_id: str, payload: dict):
        core.jobs.toggle(job_id, payload.get("enabled") is True)
        return {"ok": True}

    @api.get("/jobs/{job_id}/history")
    async def job_history(job_id: str):
        return core.db.all(
            "SELECT o.*,t.status AS task_status,t.bot_id,t.error AS task_error,"
            "(SELECT answer FROM messages WHERE task_id=t.id AND answer IS NOT NULL ORDER BY COALESCE(finished,started,created) DESC,rowid DESC LIMIT 1) AS answer "
            "FROM occurrences o LEFT JOIN tasks t ON t.id=o.task_id "
            "WHERE o.schedule_id=? ORDER BY o.intended DESC LIMIT 100",
            (job_id,),
        )

    @api.delete("/jobs/{job_id}")
    async def archive_job(job_id: str):
        core.jobs.archive(job_id)
        return {"ok": True}

    @api.post("/jobs/{job_id}/restore")
    async def restore_job(job_id: str):
        core.jobs.archive(job_id, False)
        return {"ok": True}

    @api.get("/plugins")
    async def plugins():
        return {"installed": core.plugins.list(), "marketplaces": core.plugins.catalog()}

    @api.get("/plugin-ui/{bot_id}/{plugin_id}/{version}/assets/{path:path}")
    def plugin_ui_asset(bot_id: str, plugin_id: str, version: str, path: str):
        manifest, files = core.plugins.ui_package(plugin_id, version, bot_id)
        ui = manifest["ui"]
        declared = {
            *ui.get("assets", []),
            *ui.get("styles", []),
            *ui.get("patches", []),
            *(page["entrypoint"] for page in ui.get("pages", [])),
        }
        if path not in declared:
            raise HTTPException(404, "Plugin UI asset not found.")
        media = (
            "text/javascript"
            if path.endswith(".js")
            else mimetypes.guess_type(path)[0] or "application/octet-stream"
        )
        return Response(files[path], media_type=media)

    def plugin_state_key(bot_id, plugin_id, version, key):
        core.plugins.ui_package(plugin_id, version, bot_id)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
            raise ValueError("Use a storage key with letters, numbers, underscores or hyphens.")
        return f"plugin-ui-state:{plugin_id}:{bot_id}:{key}"

    @api.get("/plugin-ui/{bot_id}/{plugin_id}/{version}/state/{key}")
    def plugin_ui_state(bot_id: str, plugin_id: str, version: str, key: str):
        return {"value": core.db.setting(plugin_state_key(bot_id, plugin_id, version, key))}

    @api.put("/plugin-ui/{bot_id}/{plugin_id}/{version}/state/{key}")
    def save_plugin_ui_state(bot_id: str, plugin_id: str, version: str, key: str, payload: dict):
        name = plugin_state_key(bot_id, plugin_id, version, key)
        value = payload.get("value")
        if len(packed(value)) > 200000:
            raise ValueError("Keep each plugin storage value under 200 KB.")
        # Bound the per-bot store so a drawing history cannot fill the settings database.
        with core.db.transaction() as sql:
            entries = sql.execute(
                "SELECT key,value FROM settings WHERE key LIKE ?",
                (f"plugin-ui-state:{plugin_id}:{bot_id}:%",),
            ).fetchall()
            if len(entries) >= 200 and not any(row["key"] == name for row in entries):
                raise ValueError(
                    "This plugin has 200 saved entries for this bot. Delete older entries first."
                )
            if sum(len(row["value"]) for row in entries if row["key"] != name) + len(packed(value)) > 2000000:
                raise ValueError(
                    "This plugin's storage for this bot exceeds 2 MB. Remove older entries first."
                )
            sql.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (name, packed(value)))
        return {"saved": True}

    @api.delete("/plugin-ui/{bot_id}/{plugin_id}/{version}/state/{key}")
    def delete_plugin_ui_state(bot_id: str, plugin_id: str, version: str, key: str):
        name = plugin_state_key(bot_id, plugin_id, version, key)
        core.db.execute("DELETE FROM settings WHERE key=?", (name,))
        return {"deleted": True}

    @api.post("/marketplaces")
    async def marketplace(payload: dict):
        return await core.plugins.refresh(payload["source"], payload.get("ref", "HEAD"))

    @api.delete("/marketplaces/{market_id}")
    async def remove_marketplace(market_id: str):
        with core.db.transaction() as db:
            market = db.execute("SELECT source FROM marketplaces WHERE id=?", (market_id,)).fetchone()
            if market:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?,?)",
                    ("marketplace-source:" + market_id, packed(market["source"])),
                )
                db.execute("DELETE FROM marketplaces WHERE id=?", (market_id,))
        return {"ok": True}

    @api.post("/plugins/stage")
    async def stage_plugin(payload: dict):
        if "files" in payload:
            return await asyncio.to_thread(
                core.plugins.stage, {name: content.encode() for name, content in payload["files"].items()}
            )
        return await asyncio.to_thread(
            core.plugins.stage_from_catalog, payload["marketplace"], payload["id"], payload["version"]
        )

    @api.post("/plugins/mcp/connect")
    async def connect_mcp(payload: dict):
        return await core.connect_mcp(payload)

    @api.post("/plugins/skills/install")
    async def install_skills(payload: dict):
        return await core.install_skills(payload)

    @api.post("/plugins/source/inspect")
    async def inspect_extension_source(payload: dict):
        return await core.source_extension(payload)

    @api.post("/plugins/source/import")
    async def import_extension_source(payload: dict):
        return await core.source_extension(payload, install=True)

    @api.post("/plugins/skills/import")
    async def import_skills(payload: dict):
        return await asyncio.to_thread(
            core.import_skills,
            [payload["files"]],
            payload["license"],
            "Local skill",
            payload.get("access", {"mode": "shared", "bots": []}),
        )

    @api.put("/plugins/{plugin_id}/access")
    async def plugin_access(plugin_id: str, payload: dict):
        return core.plugins.set_access(plugin_id, payload)

    @api.post("/templates/save")
    async def save_template(payload: dict):
        return await asyncio.to_thread(core.bots.save_template, payload)

    @api.get("/plugins/{plugin_id}/{version}/files")
    def plugin_files(plugin_id: str, version: str):
        row, files = core.plugins.package(plugin_id, version)
        manifest = json.loads(row["manifest"])
        return {
            "name": manifest["name"],
            "source": core.plugins.public(row).get("source"),
            "security": core.plugins.public(row).get("security"),
            "files": {name: data.decode("utf-8", errors="replace") for name, data in files.items()},
        }

    @api.post("/plugins/{plugin_id}/{version}/activate")
    async def activate(plugin_id: str, version: str):
        await core.activate(plugin_id, version)
        return {"ok": True}

    @api.post("/plugins/{plugin_id}/{version}/scan")
    async def scan_plugin(plugin_id: str, version: str):
        from .security import Security

        _, files = core.plugins.package(plugin_id, version)
        return await asyncio.to_thread(Security(core.db).scan, files, refresh=True)

    @api.get("/plugins/{plugin_id}/{version}/configuration")
    async def plugin_configuration(plugin_id: str, version: str):
        return core.plugins.configuration(plugin_id, version)

    @api.put("/plugins/{plugin_id}/{version}/configuration")
    async def save_plugin_configuration(plugin_id: str, version: str, payload: dict):
        return core.plugins.configuration(plugin_id, version, payload)

    @api.post("/plugins/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str):
        core.plugins.disable(plugin_id)
        return {"ok": True}

    @api.get("/providers")
    async def providers():
        return await core.coordinator.request("GET", "/v1/providers")

    @api.post("/providers/{operation}")
    async def provider(operation: str, payload: dict):
        if operation not in {"key", "custom"}:
            raise HTTPException(404)
        return await core.coordinator.request("POST", f"/v1/providers/{operation}", json=payload)

    @api.delete("/providers/{provider_id}")
    async def logout_provider(provider_id: str):
        return await core.coordinator.request("DELETE", f"/v1/providers/{provider_id}")

    @api.post("/login")
    async def provider_login(payload: dict):
        return await core.provider_login.start(payload.get("provider"))

    @api.get("/login")
    async def login_progress():
        return await core.provider_login.progress()

    @api.post("/login/respond")
    async def login_respond(payload: dict):
        return await core.provider_login.respond(payload)

    @api.get("/browser/backends")
    async def browser_backends():
        return await core.remote("browser", "/backends", method="GET")

    @api.get("/browser")
    async def browser_state():
        return await core.remote("browser", "/state", method="GET")

    @api.post("/browser/select")
    async def select_browser(payload: dict):
        bot = core.bots.get(payload["bot_id"])
        return await core.remote("browser", "/select", {"browser_id": bot["browser_id"]})

    @api.get("/browser/view")
    async def browser_view():
        return await core.remote("browser", "/view", method="GET")

    @api.get("/browser/activity")
    async def browser_activity(request: Request):
        result = await core.remote("browser", "/lease", method="GET", timeout=5)
        result["control_allowed"] = (result.get("lease") or {}).get("owner") == digest(
            core.auth.require(request)
        )
        return result

    @api.post("/desktop/open")
    async def open_desktop_app(request: Request, payload: dict):
        return await core.remote(
            "browser",
            "/desktop/open",
            {
                "owner": digest(core.auth.require(request)),
                "application": payload.get("application"),
            },
        )

    @api.post("/browser/input")
    async def browser_input(request: Request, payload: dict):
        return await core.remote(
            "browser",
            "/input",
            {
                "owner": digest(core.auth.require(request)),
                "generation": payload.get("generation"),
                "event": payload.get("event"),
            },
            timeout=25,
        )

    @api.post("/browser/{operation}")
    async def browser_control(operation: str, request: Request, payload: dict):
        if operation not in {"takeover", "release"}:
            raise HTTPException(404)
        data = {"owner": digest(core.auth.require(request))}
        if operation == "takeover" and payload.get("bot_id"):
            data["browser_id"] = core.bots.get(payload["bot_id"])["browser_id"]
        return await core.remote("browser", f"/{operation}", data)

    app.include_router(api)
    from .channels import channel_router

    app.include_router(channel_router(core))

    @app.websocket("/api/browser/vnc")
    async def browser_socket(socket: WebSocket):
        token = socket.cookies.get("shinbot_session", "")
        checked_at, allowed = 0.0, False

        def valid():
            nonlocal checked_at, allowed
            if time.monotonic() - checked_at >= 0.25:
                allowed = bool(
                    core.db.one(
                        "SELECT 1 FROM sessions WHERE hash=? AND expires>?", (digest(token), time.time())
                    )
                )
                checked_at = time.monotonic()
            return allowed

        if socket.headers.get("origin") != config.origin or not valid():
            return await socket.close(code=1008)
        mode = "control" if socket.query_params.get("mode") == "control" else "view"
        url = (
            config.browser_url.replace("http://", "ws://").replace("https://", "wss://") + f"/vnc?mode={mode}"
        )
        await socket.accept()
        try:
            async with websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {config.browser_token}",
                    "X-Shinbot-Owner": digest(token),
                },
                max_size=4_000_000,
            ) as upstream:

                async def up():
                    while valid():
                        await upstream.send(await socket.receive_bytes())

                async def down():
                    async for data in upstream:
                        if not valid():
                            return
                        await socket.send_bytes(data)

                async def guard():
                    while valid():
                        await asyncio.sleep(0.25)

                tasks = [asyncio.create_task(f()) for f in (up, down, guard)]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    APIRouter(prefix="/internal", dependencies=[Depends(lambda request: None)])

    def runtime_auth(request: Request):
        require_service(request, config.service_token)

    @app.post("/internal/actions", dependencies=[Depends(runtime_auth)])
    async def actions(payload: dict):
        return await core.action(payload)

    @app.post("/internal/admit", dependencies=[Depends(runtime_auth)])
    def admit(payload: dict):
        task = core.db.one("SELECT * FROM tasks WHERE id=?", (payload["task_id"],))
        if task:
            core.db.record_usage(task["id"], payload["tokens"], payload["cost"])
        if (
            not task
            or task["status"] in {"cancelled", "paused", "uncertain"}
            or config.safe_mode
            or core.db.setting("restore_hold", False)
        ):
            return {"allowed": False, "error": "Task is stopped."}
        total = core.db.one(
            "SELECT COALESCE(SUM(tokens),0) AS total FROM usage_ledger WHERE created>=?",
            (time.time() - 86400,),
        )["total"]
        current = core.db.one(
            "SELECT b.baseline FROM submission_budgets b JOIN messages m ON m.id=b.message_id WHERE m.task_id=? AND m.status IN ('dispatching','running') ORDER BY m.created DESC LIMIT 1",
            (task["id"],),
        )
        spent = payload["tokens"] - (current["baseline"] if current else task["tokens"])
        preferences = core.db.setting("preferences")
        budget_scope = None
        if preferences["daily_tokens"] > 0 and total >= preferences["daily_tokens"]:
            budget_scope = "workspace_daily"
        elif (
            task["max_tokens"] > 0
            and spent >= task["max_tokens"]
            and (not preferences["task_tokens"] or task["max_tokens"] <= preferences["task_tokens"])
        ):
            card = core.db.one(
                "SELECT c.max_tokens,c.tokens FROM work_cards c JOIN work_runs r ON r.card_id=c.id "
                "WHERE r.task_id=?",
                (task["id"],),
            )
            if card and card["max_tokens"] > 0 and card["max_tokens"] - card["tokens"] <= task["max_tokens"]:
                budget_scope = "card"
            else:
                budget_scope = "saved_run" if card else "saved_chat"
        elif preferences["task_tokens"] > 0 and spent >= preferences["task_tokens"]:
            budget_scope = "workspace_message"
        return {
            "allowed": budget_scope is None,
            "error": "The configured token budget has been reached." if budget_scope else None,
            "budget_scope": budget_scope,
        }

    web = Path(__file__).parent / "web"
    web.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/")
    async def index():
        return FileResponse(web / "index.html")

    return app
