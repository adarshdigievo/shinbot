import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import require_service
from .db import packed, uid


class TelegramUpdate(BaseModel):
    update_id: int = Field(ge=0)
    owner_id: int = Field(gt=0)
    chat_id: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=20000)


def telegram_owner():
    return os.getenv("TELEGRAM_OWNER_ID", "")


def enqueue(db, text, owner, task_id=None):
    db.execute(
        "INSERT INTO outbox VALUES (?,?,?,'pending',?,?,NULL)",
        (uid(), "telegram", packed({"text": text, "chat_id": str(owner)}), time.time(), task_id),
    )


def channel_router(core):
    def authenticate(request: Request):
        require_service(request, core.config.channel_token)

    def check_owner(owner):
        if not telegram_owner() or str(owner) != telegram_owner():
            raise HTTPException(403, "Only the configured Telegram owner can send messages.")

    def held():
        return core.config.safe_mode or core.db.setting("restore_hold", False)

    router = APIRouter(prefix="/channel", dependencies=[Depends(authenticate)])

    @router.post("/register")
    async def register(payload: dict):
        check_owner(payload["owner_id"])
        account = {"owner_id": str(payload["owner_id"]), "bot_id": str(int(payload["bot_id"]))}
        previous = core.db.setting("telegram_account")
        with core.db.transaction() as db:
            if previous and previous != account:
                db.execute("DELETE FROM channel_updates WHERE channel='telegram'")
                db.execute("DELETE FROM channel_chats WHERE channel='telegram'")
                db.execute("DELETE FROM settings WHERE key IN ('telegram_offset','telegram_selected_bot')")
                db.execute(
                    "UPDATE outbox SET status='failed' WHERE channel='telegram' AND status IN ('pending','sending')"
                )
            db.execute("INSERT OR REPLACE INTO settings VALUES ('telegram_account',?)", (packed(account),))
        return {"ok": True}

    @router.get("/offset")
    async def offset():
        return {"offset": core.db.setting("telegram_offset", 0), "held": held()}

    @router.post("/checkpoint")
    async def checkpoint(payload: dict):
        value = payload.get("offset")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("Invalid Telegram offset.")
        with core.db.transaction() as db:
            old = db.execute("SELECT value FROM settings WHERE key='telegram_offset'").fetchone()
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('telegram_offset',?)",
                (packed(max(value, json.loads(old[0]) if old else 0)),),
            )
        return {"ok": True}

    @router.post("/updates")
    async def inbound(payload: TelegramUpdate):
        check_owner(payload.owner_id)
        check_owner(payload.chat_id)
        if held():
            raise HTTPException(409, "Connector is paused during recovery.")
        owner, update_id, prompt = str(payload.owner_id), str(payload.update_id), payload.content
        selected = core.db.setting("telegram_selected_bot", "default")
        bots = core.bots.list()
        bot = next((bot for bot in bots if bot["id"] == selected), bots[0])
        with core.db.transaction() as db:
            prior = db.execute(
                "SELECT task_id FROM channel_updates WHERE channel='telegram' AND update_id=?", (update_id,)
            ).fetchone()
            if prior:
                return {"task_id": prior["task_id"] or None, "duplicate": True}
            chat = db.execute(
                "SELECT task_id FROM channel_chats WHERE channel='telegram' AND chat_id=?", (owner,)
            ).fetchone()
            existing = (
                db.execute("SELECT status,bot_id FROM tasks WHERE id=?", (chat["task_id"],)).fetchone()
                if chat
                else None
            )
            task_id, reply = None, None
            command, _, argument = prompt.strip().partition(" ")
            command = command.split("@", 1)[0].lower()
            if command in {"/start", "/help"}:
                reply = (
                    f"You’re talking to {bot['name']}. Send a message to begin.\n\n"
                    "/bots — list bot profiles\n/bot NAME — switch profile\n/new — start a new conversation\n/status — check your conversation\n\n"
                    f"Review approvals and manage your workspace at {core.config.origin}"
                )
            elif command == "/bots":
                reply = "Your bots:\n" + "\n".join(f"{b['name']}\n/bot {b['id']}" for b in bots)
            elif command == "/bot":
                matches = [
                    b
                    for b in bots
                    if b["id"] == argument.strip() or b["name"].casefold() == argument.strip().casefold()
                ]
                if len(matches) != 1:
                    reply = "Send /bots, then use /bot followed by a profile’s name or ID. Use the ID if names match."
                else:
                    bot = matches[0]
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES ('telegram_selected_bot',?)",
                        (packed(bot["id"]),),
                    )
                    db.execute("DELETE FROM channel_chats WHERE channel='telegram' AND chat_id=?", (owner,))
                    reply = f"Switched to {bot['name']}. Your next message starts a new conversation."
            elif command == "/new":
                db.execute("DELETE FROM channel_chats WHERE channel='telegram' AND chat_id=?", (owner,))
                reply = f"Ready for a new conversation with {bot['name']}. Send your first message."
            elif command == "/status":
                reply = f"Bot: {bot['name']}\nConversation: {existing['status'] if existing else 'not started'}\n{core.config.origin}"
            elif command == "/unsupported":
                reply = "Send a text message. Photos, voice messages and attachments are not supported yet."
            elif command.startswith("/"):
                reply = "I don’t recognize that command. Send /help for the available commands."
            elif not prompt.strip():
                reply = "Send a message with some text to start."
            elif (
                not existing
                or existing["bot_id"] != bot["id"]
                or existing["status"] in {"cancelled", "paused", "uncertain"}
            ):
                try:
                    task_id = core.tasks.create(
                        prompt, source=f"telegram:{owner}", connection=db, bot_id=bot["id"]
                    )["id"]
                except ValueError as exc:
                    reply = f"{str(exc)[:500]} Open {core.config.origin}, then send your message again."
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO channel_chats VALUES ('telegram',?,?)", (owner, task_id)
                    )
            else:
                task_id = chat["task_id"]
                core.tasks._message(db, task_id, prompt, f"telegram:{update_id}")
                db.execute(
                    "UPDATE tasks SET status='queued' WHERE id=? AND status IN ('completed','failed')",
                    (task_id,),
                )
            if reply:
                enqueue(db, reply, owner)
            db.execute("INSERT INTO channel_updates VALUES ('telegram',?,?)", (update_id, task_id or ""))
            old = db.execute("SELECT value FROM settings WHERE key='telegram_offset'").fetchone()
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('telegram_offset',?)",
                (packed(max(payload.update_id + 1, json.loads(old[0]) if old else 0)),),
            )
        return {"task_id": task_id, "duplicate": False}

    @router.post("/outbox/claim")
    async def claim():
        if held() or not telegram_owner():
            return {"item": None}
        with core.db.transaction() as db:
            db.execute(
                "UPDATE outbox SET status='uncertain' WHERE channel='telegram' AND status='sending' AND claim_until<?",
                (time.time(),),
            )
            # Messages retain their intended recipient even if the owner changes.
            db.execute(
                "UPDATE outbox SET status='failed' WHERE channel='telegram' AND status='pending' AND COALESCE(json_extract(body,'$.chat_id'),'')<>?",
                (telegram_owner(),),
            )
            item = db.execute(
                "SELECT * FROM outbox WHERE channel='telegram' AND status='pending' ORDER BY created LIMIT 1"
            ).fetchone()
            if not item:
                return {"item": None}
            db.execute(
                "UPDATE outbox SET status='sending',claim_until=? WHERE id=?", (time.time() + 300, item["id"])
            )
            return {"item": {"id": item["id"], "body": json.loads(item["body"])}}

    @router.post("/outbox/{item_id}/receipt")
    async def receipt(item_id: str, payload: dict):
        if payload.get("status") not in {"sent", "uncertain", "failed"}:
            raise ValueError("Invalid delivery receipt.")
        core.db.execute(
            "UPDATE outbox SET status=? WHERE id=? AND channel='telegram' AND status='sending'",
            (payload["status"], item_id),
        )
        return {"ok": True}

    return router
