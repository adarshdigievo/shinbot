"""Telegram connector: Bot API client, reply formatting and the loops that run inside the gateway."""

import asyncio
import html
import json
import logging
import os
import re

import httpx

from .channels import COMMANDS, OWNER, PLUGIN, TOKEN

log = logging.getLogger(__name__)
LIMIT = 3500
TICK = 4  # Telegram clears a typing indicator after about five seconds.
EMPTY = "The task finished without a text reply. Open ShinBot to check its result."
MEDIA = (
    "photo",
    "document",
    "voice",
    "audio",
    "video",
    "video_note",
    "animation",
    "sticker",
    "location",
    "contact",
    "poll",
)
FENCE = re.compile(r"^\s{0,3}(```+|~~~+)\s*([\w+#.-]*)\s*$")


class TelegramError(Exception):
    """A rejected Bot API call. Never carries the request URL, which contains the bot token."""

    def __init__(self, code, description="", retry_after=None):
        super().__init__(f"Telegram error {code}")
        self.code, self.description, self.retry_after = code, description, retry_after


class Api:
    def __init__(self, token, transport=None):
        self.client = httpx.AsyncClient(
            # Overridable for a self-hosted Bot API server.
            base_url=f"{os.getenv('SHINBOT_TELEGRAM_API_URL', 'https://api.telegram.org').rstrip('/')}/bot{token}/",
            timeout=httpx.Timeout(20, read=45),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    async def call(self, method, files=None, **params):
        if files:
            # Multipart fields are strings, so structured values travel as JSON text.
            data = {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
            response = await self.client.post(method, data=data, files=files)
        else:
            response = await self.client.post(method, json=params)
        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(response.status_code) from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            payload = payload if isinstance(payload, dict) else {}
            raise TelegramError(
                payload.get("error_code", response.status_code),
                str(payload.get("description", "")),
                (payload.get("parameters") or {}).get("retry_after"),
            )
        return payload["result"]


def units(text):
    """Telegram counts message length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def split_message(text, limit=LIMIT):
    """Split a Markdown reply at paragraph or line breaks, reopening code blocks cut in two."""
    chunks, rest, fence = [], text, ""
    while units(rest) > limit:
        cut, used = 0, 0
        for char in rest:
            size = 2 if ord(char) > 0xFFFF else 1
            if used + size > limit - 16:
                break
            used, cut = used + size, cut + 1
        for separator in ("\n\n", "\n", " "):
            at = rest.rfind(separator, cut // 2, cut)
            if at > 0:
                cut = at + len(separator)
                break
        piece, rest = rest[:cut], rest[cut:]
        for line in piece.split("\n"):
            mark = FENCE.match(line)
            if mark:
                fence = "" if fence else "```" + mark[2]
        if fence:
            piece, rest = piece.rstrip("\n") + "\n```", fence + "\n" + rest
            fence = ""
        chunks.append(piece)
    chunks.append(rest)
    return [chunk for chunk in chunks if chunk.strip()] or [EMPTY]


def inline(text):
    """Inline Markdown to Telegram HTML. Code and links are set aside so emphasis cannot split them."""
    kept, text = [], text.replace("\x00", "")

    def keep(value):
        kept.append(value)
        return f"\x00{len(kept) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", lambda m: keep("<code>" + html.escape(m[1], quote=False) + "</code>"), text)
    text = html.escape(text, quote=False)
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)\)",
        lambda m: keep(f'<a href="{m[2].replace(chr(34), "%22")}">') + m[1] + keep("</a>"),
        text,
    )
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]*?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?=[^\s_])([^_\n]*?)(?<=[^\s_])_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m[1])], text)


def markdown_html(text):
    """The subset of Markdown that models write, as the HTML Telegram accepts."""
    out, lines, index = [], text.split("\n"), 0
    while index < len(lines):
        line = lines[index]
        fence = FENCE.match(line)
        if fence:
            body, index = [], index + 1
            while index < len(lines) and not lines[index].strip().startswith(fence[1][:3]):
                body.append(lines[index])
                index += 1
            index += 1
            code = html.escape("\n".join(body), quote=False)
            out.append(
                f'<pre><code class="language-{fence[2]}">{code}</code></pre>' if fence[2] else f"<pre>{code}</pre>"
            )
            continue
        if line.lstrip().startswith("|") and index + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[index + 1]):
            table = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table.append(lines[index])
                index += 1
            out.append("<pre>" + html.escape("\n".join(table), quote=False) + "</pre>")
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote.append(inline(re.sub(r"^\s*>\s?", "", lines[index])))
                index += 1
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading:
            out.append("<b>" + inline(heading[1]) + "</b>")
        elif re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", line):
            out.append("———")
        else:
            bullet = re.match(r"^(\s*)[-*+]\s+(\[[ xX]\]\s+)?(.*)$", line)
            if bullet:
                box = "" if not bullet[2] else "☑ " if "x" in bullet[2].lower() else "☐ "
                out.append(bullet[1] + ("" if box else "• ") + box + inline(bullet[3]))
            else:
                out.append(inline(line))
        index += 1
    return "\n".join(out)


def keyboard(buttons):
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in buttons]}


class Session:
    """One connection of one bot token: intake, delivery and the working indicator."""

    def __init__(self, core, settings, api):
        self.core, self.channel, self.api = core, core.telegram, api
        self.owner = int(settings["owner"]) if settings["owner"] else None
        self.wake = asyncio.Event()
        self.note = None

    async def send(self, text, markup=None, parse=None, quiet=False):
        """Send one message, waiting out short rate limits. A RetryAfter means it was not accepted."""
        params = {"chat_id": self.owner, "text": text, "link_preview_options": {"is_disabled": True}}
        if parse:
            params["parse_mode"] = parse
        if markup:
            params["reply_markup"] = markup
        if quiet:
            params["disable_notification"] = True
        waited = 0
        for _ in range(4):
            try:
                return await self.api.call("sendMessage", **params)
            except TelegramError as exc:
                if exc.code != 429 or not exc.retry_after or waited + exc.retry_after + 1 > 120:
                    raise
                await asyncio.sleep(exc.retry_after + 1)
                waited += exc.retry_after + 1
        raise TelegramError(429)

    async def send_text(self, text, markdown, markup=None):
        sent = 0
        try:
            parts = split_message(text)
            for index, part in enumerate(parts):
                if sent:
                    await asyncio.sleep(1)
                last = markup if index == len(parts) - 1 else None
                try:
                    await self.send(markdown_html(part) if markdown else part, last, "HTML" if markdown else None)
                except TelegramError as exc:
                    if not (markdown and exc.code == 400 and "parse" in exc.description.lower()):
                        raise
                    # Telegram refused the markup outright, so nothing was sent: fall back to plain text.
                    await self.send(part, last)
                sent += 1
            return "sent"
        except TelegramError:
            return "uncertain" if sent else "failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            # A timeout does not tell us whether Telegram accepted the message.
            return "uncertain"

    async def send_files(self, ids):
        for item in self.channel.files(ids):
            try:
                data = await asyncio.to_thread(item["path"].read_bytes)
                file = (item["name"], data, item["mime_type"])
                if item["preview"] and item["mime_type"] != "image/gif":
                    try:
                        await self.api.call("sendPhoto", files={"photo": file}, chat_id=str(self.owner))
                        continue
                    except TelegramError as exc:
                        # Telegram shows only some images inline; the rest go as documents.
                        if exc.code != 400:
                            raise
                await self.api.call("sendDocument", files={"document": file}, chat_id=str(self.owner))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Telegram could not deliver an attachment (%s).", type(exc).__name__)
                with_note = f"I couldn’t send {item['name']} here. Open it in ShinBot: {self.core.config.origin}"
                await self.send_text(with_note, False)

    async def deliver(self, item):
        body = item["body"]
        if str(body.get("chat_id")) != str(self.owner):
            return "failed"
        if body.get("approval"):
            prompt = self.channel.approval_prompt(body["approval"])
            if not prompt:
                return "sent"
            try:
                await self.send(prompt["html"], keyboard(prompt["buttons"]), "HTML")
                return "sent"
            except TelegramError:
                return "failed"
            except asyncio.CancelledError:
                raise
            except Exception:
                return "uncertain"
        if item.get("task_id") and self.note and self.note["task_id"] == item["task_id"]:
            await self.clear_note()
        markup = keyboard(body["buttons"]) if body.get("buttons") else None
        outcome = await self.send_text(body.get("text") or EMPTY, bool(body.get("markdown")), markup)
        if outcome == "sent" and body.get("attachments"):
            await self.send_files(body["attachments"])
        return outcome

    async def delivery(self):
        while True:
            item = self.channel.claim()
            if not item:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except TimeoutError:
                    pass
                continue
            self.channel.receipt(item["id"], await self.deliver(item))

    async def clear_note(self):
        note, self.note = self.note, None
        if note:
            try:
                await self.api.call("deleteMessage", chat_id=self.owner, message_id=note["message_id"])
            except (TelegramError, httpx.HTTPError):
                pass

    async def activity(self):
        """Show that work is under way: a typing indicator plus one quietly updated list of steps."""
        while True:
            await asyncio.sleep(TICK)
            try:
                progress = None
                if not self.channel.held():
                    self.channel.attention()
                    progress = self.channel.progress()
                if not progress:
                    await self.clear_note()
                    continue
                await self.api.call("sendChatAction", chat_id=self.owner, action="typing")
                steps = progress["steps"]
                if self.note and self.note["task_id"] != progress["task_id"]:
                    await self.clear_note()
                if not steps:
                    continue
                shown = [f"• {label}" + (f": {detail}" if detail else "") for label, detail in steps[-6:]]
                earlier = len(steps) - len(shown)
                text = "Working…\n" + (f"…{earlier} earlier step{'s' if earlier != 1 else ''}\n" if earlier else "")
                text += "\n".join(shown)
                if not self.note:
                    message = await self.send(text, quiet=True)
                    self.note = {"task_id": progress["task_id"], "message_id": message["message_id"], "text": text}
                elif self.note["text"] != text:
                    self.note["text"] = text
                    await self.api.call(
                        "editMessageText", chat_id=self.owner, message_id=self.note["message_id"], text=text
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The indicator is a courtesy: never let it disturb intake or delivery.
                log.debug("Telegram progress update skipped (%s).", type(exc).__name__)

    async def message(self, update):
        message = update["message"]
        chat, sender = message.get("chat", {}), message.get("from", {})
        if chat.get("type") != "private" or sender.get("is_bot"):
            return
        if not self.owner:
            # Not linked yet: tell whoever writes how to link, but accept nothing from them.
            await self.api.call(
                "sendMessage",
                chat_id=chat["id"],
                text=f"This bot isn’t linked to a ShinBot owner yet.\n\nYour Telegram user ID is {sender.get('id')}\n\n"
                "If this is your ShinBot, open Plugins → Telegram → Connection in the app and enter that number.",
            )
            return
        if sender.get("id") != self.owner or chat.get("id") != self.owner:
            return
        content, notice = message.get("text") or message.get("caption") or "", ""
        if any(kind in message for kind in MEDIA):
            if content.strip():
                notice = "I can’t open files sent on Telegram yet, so I passed along only your caption."
            else:
                content = "/unsupported"
        quoted = (message.get("reply_to_message") or {}).get("text")
        if quoted and content.strip() and not content.startswith("/"):
            content = "> " + quoted[:300].replace("\n", "\n> ") + "\n\n" + content
        result = self.channel.inbound(update["update_id"], content[:20000] or " ", notice)
        if result.get("cancel"):
            await self.core.cancel(result["cancel"])
        if result.get("task_id"):
            self.core.coordinator.wake.set()
            try:
                await self.api.call("sendChatAction", chat_id=self.owner, action="typing")
            except (TelegramError, httpx.HTTPError):
                pass

    async def callback(self, update):
        query = update["callback_query"]
        data, message = query.get("data", ""), query.get("message") or {}
        if not self.owner or query.get("from", {}).get("id") != self.owner:
            await self.api.call("answerCallbackQuery", callback_query_id=query["id"], text="Only the owner can do this.")
            return
        kind, _, rest = data.partition(":")
        if kind == "b":
            self.channel.inbound(update["update_id"], "/bot " + rest)
            await self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        action_id, _, choice = rest.partition(":")
        done, text = self.channel.decide(action_id, choice) if kind == "a" else (False, "This button is no longer valid.")
        if done:
            self.core.coordinator.wake.set()
        await self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=text[:190], show_alert=not done)
        if message.get("message_id"):
            # Keep the request readable and replace its buttons with what happened.
            edit = {"chat_id": self.owner, "message_id": message["message_id"]}
            try:
                if done and message.get("text"):
                    await self.api.call(
                        "editMessageText",
                        **edit,
                        text=message["text"] + "\n\n" + ("✅ " if text != "Declined" else "🚫 ") + text,
                        entities=message.get("entities", []),
                    )
                else:
                    await self.api.call("editMessageReplyMarkup", **edit, reply_markup={"inline_keyboard": []})
            except TelegramError:
                pass

    async def intake(self):
        offset = 0
        while True:
            if self.channel.held():
                await asyncio.sleep(2)
                continue
            if self.owner:
                # The saved position belongs to the linked account; an unlinked bot starts from
                # whatever Telegram still holds.
                offset = max(offset, self.channel.offset())
            updates = await self.api.call(
                "getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"]
            )
            for update in updates:
                try:
                    if "message" in update:
                        await self.message(update)
                    elif "callback_query" in update:
                        await self.callback(update)
                except (TelegramError, httpx.HTTPError) as exc:
                    # The update itself is recorded; a reply Telegram refuses must not stall the queue.
                    log.warning("Telegram refused a reply (%s).", type(exc).__name__)
                offset = update["update_id"] + 1
            if updates:
                if self.owner:
                    self.channel.checkpoint(offset)
                self.wake.set()

    async def run(self):
        me = await self.api.call("getMe")
        if (await self.api.call("getWebhookInfo")).get("url"):
            raise ValueError(
                "This Telegram bot has an active webhook. Use a separate BotFather bot, or remove its webhook."
            )
        await self.api.call(
            "setMyCommands", commands=[{"command": name, "description": text} for name, text in COMMANDS]
        )
        username = me.get("username", "")
        if self.owner:
            self.channel.register(self.owner, me["id"])
            self.channel.status("connected", username=username)
            await asyncio.gather(self.intake(), self.delivery(), self.activity())
        else:
            self.channel.status(
                "pairing",
                f"Send any message to @{username} on Telegram. It replies with your user ID to enter here.",
                username,
            )
            await self.intake()


async def session(core, settings, transport=None):
    """Keep one bot connected until cancelled, reporting why whenever it is not."""
    channel, delay = core.telegram, 5
    while True:
        try:
            async with Api(settings["token"], transport) as api:
                await Session(core, settings, api).run()
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            channel.status("error", str(exc))
            delay = 60
        except TelegramError as exc:
            if exc.code in {401, 404}:
                channel.status("error", "Telegram rejected the bot token. Copy it again from @BotFather.")
                delay = 60
            elif exc.code == 409:
                channel.status(
                    "error",
                    "Another program is reading this bot’s messages. Give each ShinBot its own BotFather bot.",
                )
                delay = 15
            else:
                log.warning("Telegram connection retry (error %s).", exc.code)
                delay = 5
        except Exception as exc:
            # Request URLs contain the bot token. Never log the URL or the error text.
            log.warning("Telegram connection retry (%s). Check the network.", type(exc).__name__)
            if channel.connection()["state"] != "connected":
                channel.status("error", "Can’t reach Telegram. ShinBot keeps trying; check the server’s network.")
            delay = 5
        await asyncio.sleep(delay)


async def adopt_host_settings(core):
    """Carry over a connector that was configured on the host before Telegram moved into the app."""
    token, owner = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_OWNER_ID", "")
    if not token or not owner or core.db.setting("telegram_host_adopted"):
        return
    entry = next((p for m in core.plugins.catalog() for p in m["plugins"] if p["id"] == PLUGIN and m["id"] == "official"), None)
    if not entry:
        return
    core.plugins.stage_from_catalog("official", PLUGIN, entry["version"])
    core.plugins.configuration(PLUGIN, entry["version"], {TOKEN: token, OWNER: owner})
    await core.activate(PLUGIN, entry["version"])
    core.db.set("telegram_host_adopted", True)
    core.db.event("plugin", "Telegram now runs inside ShinBot. Manage it in Plugins.")


async def run(core, transport=None):
    """Start, restart and stop the connector as the Telegram plugin is enabled, changed or disabled."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    channel = core.telegram
    try:
        await adopt_host_settings(core)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Telegram host settings were not carried over (%s). Connect it in Plugins.", type(exc).__name__)
    current, worker = None, None
    try:
        while True:
            try:
                settings = channel.settings()
            except ValueError as exc:
                settings = None
                channel.status("error", str(exc))
            else:
                if not settings:
                    channel.status("off")
            if settings != current or (worker and worker.done()):
                if worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                current = settings
                if settings and channel.connection()["state"] != "connected":
                    channel.status("connecting")
                worker = asyncio.create_task(session(core, settings, transport)) if settings else None
            await asyncio.sleep(3)
    finally:
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
