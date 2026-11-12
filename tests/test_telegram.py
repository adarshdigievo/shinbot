import time
from urllib.parse import parse_qs

import httpx
import pytest
from telegram import Bot
from telegram.request import HTTPXRequest

from shinbot.services.telegram import cycle, deliver, split_message

OWNER = 123456


@pytest.fixture
def channel(client, application, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", str(OWNER))
    client.headers["Authorization"] = "Bearer " + application.state.core.config.channel_token
    return client


def update(client, id, content, owner=OWNER, chat=OWNER):
    return client.post(
        "/channel/updates", json={"update_id": id, "owner_id": owner, "chat_id": chat, "content": content}
    )


def test_telegram_commands_bot_switching_and_replay(channel, application):
    core = application.state.core
    planner = core.bots.save({"name": "Planner"})
    assert update(channel, 1, "/start").status_code == 200
    assert update(channel, 1, "/start").json()["duplicate"]
    assert core.db.one("SELECT count(*) AS n FROM outbox")["n"] == 1
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 0
    assert update(channel, 2, "/bot Planner").status_code == 200
    task_id = update(channel, 3, "Plan my week").json()["task_id"]
    assert core.tasks.detail(task_id)["bot_id"] == planner["id"]
    assert update(channel, 3, "Plan my week").json()["task_id"] == task_id
    assert len(core.tasks.detail(task_id)["messages"]) == 1
    assert update(channel, 4, "Keep Friday free").json()["task_id"] == task_id
    update(channel, 5, "/status")
    update(channel, 6, "/new")
    new_id = update(channel, 7, "A new plan").json()["task_id"]
    assert new_id != task_id and core.tasks.detail(new_id)["bot_id"] == planner["id"]
    update(channel, 8, "/bots")
    assert "Planner" in core.db.all("SELECT body FROM outbox ORDER BY created")[-1]["body"]
    assert channel.get("/channel/offset").json()["offset"] == 9
    channel.post("/channel/checkpoint", json={"offset": 2})
    assert channel.get("/channel/offset").json()["offset"] == 9


def test_missing_model_replies_once_and_keeps_connector_moving(channel, application):
    core = application.state.core
    prefs = core.db.setting("preferences")
    core.db.set("preferences", {**prefs, "provider": "", "model": ""})
    response = update(channel, 1, "Help me plan")
    assert response.status_code == 200 and response.json()["task_id"] is None
    assert update(channel, 1, "Help me plan").json()["duplicate"]
    assert channel.get("/channel/offset").json()["offset"] == 2
    assert len(core.db.all("SELECT * FROM outbox")) == 1
    reply = channel.post("/channel/outbox/claim", json={}).json()["item"]
    assert "Choose a model" in reply["body"]["text"]
    core.db.set("preferences", prefs)
    assert update(channel, 2, "Help me plan").json()["task_id"]


def test_telegram_auth_recovery_and_recipient_binding(channel, application, monkeypatch):
    core = application.state.core
    assert update(channel, 1, "private", owner=999).status_code == 403
    assert update(channel, 1, "group", chat=999).status_code == 403
    core.db.set("restore_hold", True)
    assert update(channel, 1, "hello").status_code == 409
    assert channel.post("/channel/outbox/claim", json={}).json()["item"] is None
    core.db.set("restore_hold", False)
    update(channel, 1, "/help")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "987654")
    assert channel.post("/channel/outbox/claim", json={}).json()["item"] is None
    assert core.db.one("SELECT status FROM outbox")["status"] == "failed"
    channel.headers.pop("Authorization")
    assert channel.get("/channel/offset").status_code == 401


def test_telegram_delivery_claim_and_uncertainty_are_not_replayed(channel, application):
    core = application.state.core
    update(channel, 1, "/help")
    item = channel.post("/channel/outbox/claim", json={}).json()["item"]
    assert channel.post("/channel/outbox/claim", json={}).json()["item"] is None
    core.db.execute("UPDATE outbox SET claim_until=? WHERE id=?", (time.time() - 1, item["id"]))
    assert channel.post("/channel/outbox/claim", json={}).json()["item"] is None
    assert core.db.one("SELECT status FROM outbox WHERE id=?", (item["id"],))["status"] == "uncertain"
    channel.post(f"/channel/outbox/{item['id']}/receipt", json={"status": "sent"})
    assert core.db.one("SELECT status FROM outbox WHERE id=?", (item["id"],))["status"] == "uncertain"


def test_telegram_account_change_resets_offsets_and_pending_delivery(channel, application):
    core = application.state.core
    assert channel.post("/channel/register", json={"owner_id": OWNER, "bot_id": 111}).status_code == 200
    update(channel, 100, "Hello")
    update(channel, 101, "/help")
    channel.post("/channel/register", json={"owner_id": OWNER, "bot_id": 222})
    assert channel.get("/channel/offset").json()["offset"] == 0
    assert core.db.all("SELECT * FROM channel_chats") == []
    assert core.db.one("SELECT status FROM outbox")["status"] == "failed"
    assert update(channel, 100, "New account").json()["duplicate"] is False


def test_telegram_chunks_preserve_unicode_and_long_answers():
    text = ("A useful paragraph.\n" + "🌍" * 100) * 100
    parts = split_message(text)
    assert "".join(parts) == text
    assert len(parts) > 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)


def telegram_transport(updates, sent, *, fail=False):
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            result = {"id": 555, "is_bot": True, "first_name": "ShinBot", "username": "shinbot_test"}
        elif method == "getUpdates":
            result = list(updates)
            updates.clear()
        elif method == "sendMessage":
            fields = parse_qs(request.content.decode())
            sent.append(fields)
            if fail:
                raise httpx.ReadTimeout("Delivery acknowledgement lost")
            result = {
                "message_id": len(sent),
                "date": 1700000000,
                "chat": {"id": OWNER, "type": "private"},
                "text": fields["text"][0],
            }
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, "result": result})

    return httpx.MockTransport(handler)


def telegram_bot(updates, sent, **kwargs):
    transport = telegram_transport(updates, sent, **kwargs)
    return Bot(
        "555:" + "x" * 35,
        request=HTTPXRequest(httpx_kwargs={"transport": transport}),
        get_updates_request=HTTPXRequest(httpx_kwargs={"transport": transport}),
    )


async def test_telegram_real_client_round_trip_and_untrusted_sender_filter(application, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", str(OWNER))
    core = application.state.core
    sent = []
    updates = [
        {
            "update_id": id,
            "message": {
                "message_id": id,
                "date": 1700000000,
                "chat": {"id": who, "type": "private"},
                "from": {"id": who, "is_bot": False, "first_name": "Owner"},
                "text": text,
            },
        }
        for id, who, text in [(10, 999, "Ignore me"), (11, OWNER, "/help")]
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + core.config.channel_token},
    ) as control:
        async with telegram_bot(updates, sent) as bot:
            offset = await cycle(bot, control, OWNER, 0)
            assert offset == 12 and len(sent) == 1
            assert sent[0]["chat_id"] == [str(OWNER)] and "/bots" in sent[0]["text"][0]
            assert core.db.one("SELECT count(*) AS n FROM channel_updates")["n"] == 1
            assert core.db.one("SELECT status FROM outbox")["status"] == "sent"
            assert await cycle(bot, control, OWNER, 0) == 12
            assert len(sent) == 1


async def test_telegram_lost_send_acknowledgement_is_uncertain():
    sent = []
    async with telegram_bot([], sent, fail=True) as bot:
        outcome = await deliver(bot, OWNER, {"body": {"chat_id": str(OWNER), "text": "One reply"}})
    assert outcome == "uncertain" and len(sent) == 1


async def test_telegram_delivers_full_long_reply(monkeypatch):
    async def no_wait(_):
        pass

    monkeypatch.setattr("shinbot.services.telegram.asyncio.sleep", no_wait)
    text, sent = "🌍 hello\n" * 1500, []
    async with telegram_bot([], sent) as bot:
        assert await deliver(bot, OWNER, {"body": {"chat_id": str(OWNER), "text": text}}) == "sent"
    assert "".join(item["text"][0] for item in sent) == text


async def test_telegram_honors_long_rate_limit_without_losing_remaining_parts(monkeypatch):
    from telegram.error import RetryAfter

    sent, delays = [], []

    class LimitedBot:
        limited = False

        async def send_message(self, owner, text):
            if sent and not self.limited:
                self.limited = True
                raise RetryAfter(90)
            sent.append(text)

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("shinbot.services.telegram.asyncio.sleep", sleep)
    text = "Full answer " * 700
    assert await deliver(LimitedBot(), OWNER, {"body": {"chat_id": str(OWNER), "text": text}}) == "sent"
    assert "".join(sent) == text and 91 in delays


@pytest.mark.parametrize("first_part_sent", [False, True])
async def test_excessive_telegram_rate_limit_does_not_block_intake(first_part_sent, monkeypatch):
    from telegram.error import RetryAfter

    from shinbot.services import telegram

    calls, sleeps = [], []

    class Bot:
        async def send_message(self, owner, text):
            calls.append(text)
            if not first_part_sent or len(calls) > 1:
                raise RetryAfter(3600)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(telegram.asyncio, "sleep", sleep)
    result = await telegram.deliver(Bot(), 123, {"body": {"chat_id": "123", "text": "a" * 5000}})
    assert result == ("uncertain" if first_part_sent else "failed")
    assert sleeps == ([1] if first_part_sent else [])
    assert len(calls) == (2 if first_part_sent else 1)
