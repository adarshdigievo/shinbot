"""Private Telegram chat connector with persisted intake and delivery receipts."""

import asyncio
import logging
import os

import httpx
from telegram import Bot
from telegram.error import BadRequest, Forbidden, RetryAfter


def split_message(text, limit=3500):
    """Preserve every character while keeping chunks below Telegram's text limit."""
    chunks, start, units = [], 0, 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            chunks.append(text[start:index])
            start, units = index, 0
        units += size
    if start < len(text):
        chunks.append(text[start:])
    return chunks or ["The task finished without a text reply. Open ShinBot to check its result."]


async def deliver(bot, owner, item):
    if str(item["body"].get("chat_id")) != str(owner):
        return "failed"
    sent, waited, retries = 0, 0, 0
    try:
        for part in split_message(item["body"]["text"]):
            if sent:
                await asyncio.sleep(1)
            while True:
                try:
                    await bot.send_message(owner, part)
                    sent += 1
                    break
                except RetryAfter as exc:
                    delay = exc.retry_after
                    delay = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                    if waited + delay + 1 > 120 or retries >= 3:
                        return "uncertain" if sent else "failed"
                    # RetryAfter confirms this part was rejected, so resending it is safe.
                    await asyncio.sleep(delay + 1)
                    waited += delay + 1
                    retries += 1
        return "sent"
    except (BadRequest, Forbidden):
        return "uncertain" if sent else "failed"
    except asyncio.CancelledError:
        raise
    except Exception:
        # A timeout does not tell us whether Telegram accepted the message.
        return "uncertain"


async def cycle(bot, control, owner, offset):
    response = await control.get("/channel/offset")
    response.raise_for_status()
    saved = response.json()
    if saved["held"]:
        await asyncio.sleep(2)
        return offset
    offset = max(offset, saved["offset"])
    updates = await bot.get_updates(offset=offset, timeout=5, read_timeout=10, allowed_updates=["message"])
    for update in updates:
        message = update.effective_message
        if (
            message
            and update.effective_user
            and update.effective_user.id == owner
            and message.chat_id == owner
            and message.chat.type == "private"
        ):
            response = await control.post(
                "/channel/updates",
                json={
                    "update_id": update.update_id,
                    "owner_id": owner,
                    "chat_id": owner,
                    "content": message.text or "/unsupported",
                },
            )
            response.raise_for_status()
        offset = update.update_id + 1
    if updates:
        response = await control.post("/channel/checkpoint", json={"offset": offset})
        response.raise_for_status()
    response = await control.post("/channel/outbox/claim", json={})
    response.raise_for_status()
    item = response.json()["item"]
    if item:
        outcome = await deliver(bot, owner, item)
        response = await control.post(f"/channel/outbox/{item['id']}/receipt", json={"status": outcome})
        response.raise_for_status()
    return offset


async def run():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    owner_text = os.getenv("TELEGRAM_OWNER_ID", "")
    channel_token = os.getenv("SHINBOT_CHANNEL_TOKEN", "")
    if not bot_token or not owner_text.isdigit() or int(owner_text) <= 0 or len(channel_token) < 32:
        raise RuntimeError("Telegram is not configured. Run shinbot telegram-setup on the host.")
    owner = int(owner_text)
    url = os.getenv("SHINBOT_CONTROL_URL", "http://control:8765")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {channel_token}"}, timeout=30
    ) as control:
        async with Bot(bot_token) as bot:
            if (await bot.get_webhook_info()).url:
                raise RuntimeError(
                    "This Telegram bot has an active webhook. Use a separate BotFather bot or remove its webhook before connecting ShinBot."
                )
            response = await control.post("/channel/register", json={"owner_id": owner, "bot_id": bot.id})
            response.raise_for_status()
            offset = 0
            while True:
                try:
                    offset = await cycle(bot, control, owner, offset)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Telegram request URLs contain bot tokens. Never log the URL or error text.
                    logging.warning(
                        "Telegram connection retry (%s). Check the connector settings and network.",
                        type(exc).__name__,
                    )
                    await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())
