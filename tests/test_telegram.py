import asyncio
import json
import time
import uuid

import httpx
import pytest

from shinbot import telegram
from shinbot.channels import OWNER as OWNER_KEY
from shinbot.channels import TOKEN as TOKEN_KEY
from shinbot.channels import describe
from shinbot.telegram import Api, Session, markdown_html, split_message, units

OWNER = 123456
BOT_TOKEN = "555:" + "x" * 35


class FakeTelegram:
    """A Bot API double: records every call and serves queued updates to long polls."""

    def __init__(self, updates=()):
        self.updates, self.calls, self.replies, self.next_message = list(updates), [], {}, 100

    def sent(self, method="sendMessage"):
        return [params for name, params in self.calls if name == method]

    async def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path.startswith(f"/bot{BOT_TOKEN}/")
        if request.headers["content-type"].startswith("multipart"):
            params = {"multipart": request.content}
        else:
            params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        if method in self.replies:
            reply = self.replies[method](params)
            if reply is not None:
                return reply
        if method == "getMe":
            result = {"id": 555, "is_bot": True, "first_name": "ShinBot", "username": "shinbot_test"}
        elif method == "getWebhookInfo":
            result = {"url": ""}
        elif method == "getUpdates":
            if not self.updates:
                await asyncio.sleep(0.01)
            result = [u for u in self.updates if u["update_id"] >= params["offset"]]
            self.updates.clear()
        elif method in {"sendMessage", "sendPhoto", "sendDocument"}:
            self.next_message += 1
            result = {"message_id": self.next_message, "chat": {"id": OWNER, "type": "private"}}
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def error(code, description="", **parameters):
    return httpx.Response(
        code, json={"ok": False, "error_code": code, "description": description, "parameters": parameters}
    )


def text_update(id, text, who=OWNER, **extra):
    return {
        "update_id": id,
        "message": {
            "message_id": id,
            "date": 1700000000,
            "chat": {"id": who, "type": "private"},
            "from": {"id": who, "is_bot": False, "first_name": "Owner"},
            **({"text": text} if text is not None else {}),
            **extra,
        },
    }


def press(id, data, message=None, who=OWNER):
    return {
        "update_id": id,
        "callback_query": {
            "id": f"query-{id}",
            "from": {"id": who, "is_bot": False, "first_name": "Owner"},
            "data": data,
            "message": message or {"message_id": 77, "text": "Approval needed", "entities": []},
        },
    }


async def until(check, timeout=5):
    deadline = time.time() + timeout
    while not check():
        assert time.time() < deadline, "The connector did not reach the expected state."
        await asyncio.sleep(0.01)


@pytest.fixture
def core(application):
    return application.state.core


@pytest.fixture
def linked(core):
    core.telegram.register(OWNER, 555)
    core.telegram.status("connected", username="shinbot_test")
    return core.telegram


@pytest.fixture
async def running(core, monkeypatch):
    """Run a linked session against the fake API for the length of a test."""
    monkeypatch.setattr(telegram, "TICK", 0.02)
    fake, tasks = FakeTelegram(), []

    async def start(owner=OWNER):
        api = Api(BOT_TOKEN, fake.transport)
        tasks.append((asyncio.create_task(Session(core, {"owner": str(owner or "")}, api).run()), api))
        await until(lambda: any(name == "getUpdates" for name, _ in fake.calls))
        return fake

    yield start
    for task, api in tasks:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await api.client.aclose()


def outbox(core):
    return [json.loads(row["body"]) for row in core.db.all("SELECT body FROM outbox ORDER BY created,rowid")]


def pending_action(core, task_id, tool="run_command", args=None):
    return {
        "id": uuid.uuid4().hex,
        "task_id": task_id,
        "epoch": core.db.setting("epoch"),
        "tool": tool,
        "args": args or {"command": "rm -rf build"},
    }


def enable(core, owner=str(OWNER)):
    version = "2.0.0"
    core.plugins.stage_from_catalog("official", "telegram", version)
    core.plugins.configuration("telegram", version, {TOKEN_KEY: BOT_TOKEN, OWNER_KEY: owner})
    core.plugins.activate("telegram", version)
    return version


# Commands and conversations


def test_commands_bot_switching_and_replay(linked, core):
    planner = core.bots.save({"name": "Planner"})
    assert not linked.inbound(1, "/start")["duplicate"]
    assert linked.inbound(1, "/start")["duplicate"]
    assert len(outbox(core)) == 1 and "/stop" in outbox(core)[0]["text"]
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 0
    linked.inbound(2, "/bot Planner")
    task_id = linked.inbound(3, "Plan my week")["task_id"]
    assert core.tasks.detail(task_id)["bot_id"] == planner["id"]
    assert linked.inbound(3, "Plan my week")["task_id"] == task_id
    assert len(core.tasks.detail(task_id)["messages"]) == 1
    assert linked.inbound(4, "Keep Friday free")["task_id"] == task_id
    linked.inbound(5, "/status")
    assert "waiting to start" in outbox(core)[-1]["text"]
    linked.inbound(6, "/new")
    assert "earlier one keeps working" in outbox(core)[-1]["text"]
    new_id = linked.inbound(7, "A new plan")["task_id"]
    assert new_id != task_id and core.tasks.detail(new_id)["bot_id"] == planner["id"]
    linked.inbound(8, "/bots")
    buttons = outbox(core)[-1]["buttons"]
    assert ["✓ Planner", "b:" + planner["id"]] in [row[0] for row in buttons]
    assert linked.offset() == 9
    linked.checkpoint(2)
    assert linked.offset() == 9
    linked.inbound(9, "/unknown")
    assert "/help" in outbox(core)[-1]["text"]


def test_bot_list_follows_plugin_access(linked, core):
    planner = core.bots.save({"name": "Planner"})
    core.bots.save({"name": "Private"})
    enable(core)
    core.plugins.set_access("telegram", {"mode": "bots", "bots": [planner["id"]]})
    linked.inbound(1, "/bots")
    assert [row[0][0] for row in outbox(core)[-1]["buttons"]] == ["✓ Planner"]
    task_id = linked.inbound(2, "Hello")["task_id"]
    assert core.tasks.detail(task_id)["bot_id"] == planner["id"]


def test_missing_model_replies_once_and_keeps_intake_moving(linked, core):
    prefs = core.db.setting("preferences")
    core.db.set("preferences", {**prefs, "provider": "", "model": ""})
    assert linked.inbound(1, "Help me plan")["task_id"] is None
    assert linked.inbound(1, "Help me plan")["duplicate"]
    assert linked.offset() == 2 and len(outbox(core)) == 1
    assert "Choose a model" in linked.claim()["body"]["text"]
    core.db.set("preferences", prefs)
    assert linked.inbound(2, "Help me plan")["task_id"]


async def test_stop_steer_pending_and_jobs(linked, core):
    assert "Nothing is running" in (linked.inbound(1, "/stop") and outbox(core)[-1]["text"])
    task_id = linked.inbound(2, "Deploy the site")["task_id"]
    core.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    core.db.execute("UPDATE messages SET status='running' WHERE task_id=?", (task_id,))
    assert linked.inbound(3, "/steer")["task_id"] is None
    assert linked.inbound(4, "/steer use staging instead")["task_id"] == task_id
    steer = core.tasks.detail(task_id)["messages"][-1]
    assert (steer["content"], steer["mode"]) == ("use staging instead", "steer")
    linked.inbound(5, "/pending")
    assert "Nothing is waiting" in outbox(core)[-1]["text"]
    action = pending_action(core, task_id)
    assert (await core.action(action))["status"] == "pending"
    linked.inbound(6, "/pending")
    assert [item.get("approval") for item in outbox(core)][-2:] == [action["id"], action["id"]]
    result = linked.inbound(7, "/stop")
    assert result["cancel"] == task_id and "Stopped" in outbox(core)[-1]["text"]
    await core.cancel(result["cancel"])
    assert core.tasks.detail(task_id)["status"] == "cancelled"
    assert linked.inbound(8, "Start again")["task_id"] != task_id
    linked.inbound(9, "/jobs")
    assert "No scheduled jobs" in outbox(core)[-1]["text"]
    core.jobs.save({"name": "Morning brief", "prompt": "hello", "kind": "interval", "spec": "3600", "timezone": "UTC"})
    linked.inbound(10, "/jobs")
    assert "• Morning brief — next " in outbox(core)[-1]["text"]


def test_recovery_hold_owner_change_and_stale_messages(linked, core):
    linked.inbound(1, "/help")
    core.db.set("restore_hold", True)
    assert linked.claim() is None
    core.db.set("restore_hold", False)
    linked.register(987654, 555)
    assert linked.claim() is None
    assert core.db.one("SELECT status FROM outbox")["status"] == "failed"
    linked.inbound(2, "/help")
    core.db.execute("UPDATE outbox SET created=? WHERE status='pending'", (time.time() - 90000,))
    assert linked.claim() is None
    linked.status("off")
    assert linked.owner() == ""
    with pytest.raises(ValueError, match="not linked"):
        linked.inbound(3, "hello")


def test_delivery_claim_and_uncertainty_are_not_replayed(linked, core):
    linked.inbound(1, "/help")
    item = linked.claim()
    assert linked.claim() is None
    core.db.execute("UPDATE outbox SET claim_until=? WHERE id=?", (time.time() - 1, item["id"]))
    assert linked.claim() is None
    assert core.db.one("SELECT status FROM outbox WHERE id=?", (item["id"],))["status"] == "uncertain"
    linked.receipt(item["id"], "sent")
    assert core.db.one("SELECT status FROM outbox WHERE id=?", (item["id"],))["status"] == "uncertain"
    with pytest.raises(ValueError):
        linked.receipt(item["id"], "lost")


def test_account_change_resets_offsets_and_pending_delivery(linked, core):
    linked.inbound(100, "Hello")
    linked.inbound(101, "/help")
    linked.register(OWNER, 222)
    assert linked.offset() == 0
    assert core.db.all("SELECT * FROM channel_chats") == []
    assert core.db.one("SELECT status FROM outbox")["status"] == "failed"
    assert linked.inbound(100, "New account")["duplicate"] is False


# Formatting


def test_chunks_preserve_unicode_and_long_answers():
    text = ("A useful paragraph.\n" + "🌍" * 100) * 100
    parts = split_message(text)
    assert "".join(parts) == text and len(parts) > 1
    assert all(units(part) <= 3500 for part in parts)
    assert split_message("  \n ") == [telegram.EMPTY]


def test_chunks_break_at_paragraphs_and_reopen_code_blocks():
    prose = "\n\n".join(f"Paragraph {n}. " + "word " * 120 for n in range(12))
    parts = split_message(prose)
    assert "".join(parts) == prose and all(part.endswith("\n\n") for part in parts[:-1])
    code = "Here is the script:\n\n```python\n" + "print('line')\n" * 600 + "```\nDone."
    parts = split_message(code)
    assert len(parts) > 1 and all(units(part) <= 3500 for part in parts)
    for part in parts:
        assert part.count("```") % 2 == 0
        assert "<pre>" in markdown_html(part)
    assert all(part.startswith("```python\n") for part in parts[1:])
    assert sum(part.count("print('line')") for part in parts) == 600


def test_markdown_becomes_telegram_html():
    assert markdown_html("**Bold** and *italic* and ~~gone~~") == "<b>Bold</b> and <i>italic</i> and <s>gone</s>"
    assert markdown_html("Use `a < b` & snake_case_name, 2 * 3 * 4") == (
        "Use <code>a &lt; b</code> &amp; snake_case_name, 2 * 3 * 4"
    )
    assert markdown_html("## Plan\n- [x] done\n- [ ] todo\n* item\n1. first") == (
        "<b>Plan</b>\n☑ done\n☐ todo\n• item\n1. first"
    )
    assert markdown_html("[the _docs_](https://example.com/a_b_c?x=1&y=2)") == (
        '<a href="https://example.com/a_b_c?x=1&amp;y=2">the <i>docs</i></a>'
    )
    assert markdown_html("```sh\necho <hi> **raw**\n```") == (
        '<pre><code class="language-sh">echo &lt;hi&gt; **raw**</code></pre>'
    )
    assert markdown_html("> quoted *text*\n> more\nafter") == "<blockquote>quoted <i>text</i>\nmore</blockquote>\nafter"
    assert markdown_html("| a | b |\n|---|---|\n| 1 | 2 |") == "<pre>| a | b |\n|---|---|\n| 1 | 2 |</pre>"
    assert markdown_html("<script>alert(1)</script>\n---") == "&lt;script&gt;alert(1)&lt;/script&gt;\n———"


def test_action_wording_matches_the_app():
    assert describe("run_command", {"command": "ls -la\nrm x"}) == ("Running a command", "ls -la")
    assert describe("browser", {"operation": "goto", "url": "https://example.com/a?b=1"}) == (
        "Opening a webpage",
        "example.com/a",
    )
    assert describe("plugin:github:1.0.0:create_issue", {}) == ("Using create issue", "")
    assert describe("write_file", {"path": "notes/a.md"}) == ("Writing a file", "notes/a.md")


# The connector against a Bot API double


async def test_round_trip_ignores_strangers_and_formats_replies(running, core):
    fake = await running()
    fake.updates += [text_update(10, "Ignore me", who=999), text_update(11, "/help")]
    await until(lambda: fake.sent())
    assert fake.sent()[0]["chat_id"] == OWNER and "/bots" in fake.sent()[0]["text"]
    assert "parse_mode" not in fake.sent()[0]
    assert core.db.one("SELECT count(*) AS n FROM channel_updates")["n"] == 1
    await until(lambda: core.db.one("SELECT status FROM outbox")["status"] == "sent")
    assert core.telegram.offset() == 12
    assert [c["command"] for c in fake.sent("setMyCommands")[0]["commands"]][:2] == ["new", "stop"]
    fake.updates.append(text_update(12, "What is **two** plus two?"))
    await until(lambda: core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1)
    task = core.db.one("SELECT * FROM tasks")
    assert task["source"] == f"telegram:{OWNER}"
    assert fake.sent("sendChatAction")
    core.db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task["id"],))
    with core.db.transaction() as db:
        telegram_enqueue(db, "It is **four**.", task["id"])
    await until(lambda: len(fake.sent()) == 2)
    assert fake.sent()[1]["text"] == "It is <b>four</b>." and fake.sent()[1]["parse_mode"] == "HTML"


def telegram_enqueue(db, text, task_id=None, **extra):
    from shinbot.channels import enqueue

    enqueue(db, text, OWNER, task_id, markdown=True, **extra)


async def test_media_captions_replies_and_unsupported_messages(running, core):
    fake = await running()
    fake.updates += [
        text_update(1, None, photo=[{"file_id": "p"}]),
        text_update(2, None, document={"file_id": "d"}, caption="Summarize this"),
        text_update(3, "Use the second one", reply_to_message={"text": "Option A\nOption B"}),
    ]
    await until(lambda: len(fake.sent()) == 2)
    assert "can’t open photos" in fake.sent()[0]["text"] and "only your caption" in fake.sent()[1]["text"]
    messages = core.tasks.detail(core.db.one("SELECT id FROM tasks")["id"])["messages"]
    assert [m["content"] for m in messages] == ["Summarize this", "> Option A\n> Option B\n\nUse the second one"]


async def test_markup_rejection_falls_back_to_plain_text(running, core):
    fake = await running()
    fake.replies["sendMessage"] = lambda p: error(400, "Bad Request: can't parse entities") if "parse_mode" in p else None
    with core.db.transaction() as db:
        telegram_enqueue(db, "Odd **markup")
    await until(lambda: core.db.one("SELECT status FROM outbox")["status"] == "sent")
    assert [p.get("parse_mode") for p in fake.sent()] == ["HTML", None]
    assert fake.sent()[1]["text"] == "Odd **markup"


async def session_for(core, fake):
    core.telegram.register(OWNER, 555)
    core.telegram.status("connected")
    return Session(core, {"owner": str(OWNER)}, Api(BOT_TOKEN, fake.transport))


async def test_lost_send_acknowledgement_is_uncertain(core):
    fake = FakeTelegram()

    def lost(_):
        raise httpx.ReadTimeout("Delivery acknowledgement lost")

    fake.replies["sendMessage"] = lost
    session = await session_for(core, fake)
    assert await session.deliver({"body": {"chat_id": str(OWNER), "text": "One reply"}}) == "uncertain"
    assert len(fake.sent()) == 1
    assert await session.deliver({"body": {"chat_id": "999", "text": "Wrong chat"}}) == "failed"


async def test_rate_limits_are_honored_without_losing_parts(core, monkeypatch):
    fake, delays, limited = FakeTelegram(), [], []

    def limit(_):
        if len(fake.sent()) == 2 and not limited:
            limited.append(True)
            return error(429, "Too Many Requests", retry_after=90)

    async def sleep(delay):
        delays.append(delay)

    fake.replies["sendMessage"] = limit
    session = await session_for(core, fake)
    monkeypatch.setattr(telegram.asyncio, "sleep", sleep)
    text = "Full answer " * 700
    assert await session.deliver({"body": {"chat_id": str(OWNER), "text": text}}) == "sent"
    delivered = [p["text"] for p in fake.sent()]
    assert "".join(delivered[:2] + delivered[3:]) == text and delivered[1] == delivered[2] and 91 in delays


@pytest.mark.parametrize("first_part_sent", [False, True])
async def test_excessive_rate_limit_does_not_block_intake(first_part_sent, core, monkeypatch):
    fake, sleeps = FakeTelegram(), []

    def limit(_):
        if not first_part_sent or len(fake.sent()) > 1:
            return error(429, "Too Many Requests", retry_after=3600)

    async def sleep(delay):
        sleeps.append(delay)

    fake.replies["sendMessage"] = limit
    session = await session_for(core, fake)
    monkeypatch.setattr(telegram.asyncio, "sleep", sleep)
    result = await session.deliver({"body": {"chat_id": str(OWNER), "text": "a" * 5000}})
    assert result == ("uncertain" if first_part_sent else "failed")
    assert sleeps == ([1] if first_part_sent else [])
    assert len(fake.sent()) == (2 if first_part_sent else 1)


# Approvals


async def test_approval_arrives_with_buttons_and_a_tap_decides(running, core):
    fake = await running()
    fake.updates.append(text_update(1, "Clean the build folder"))
    await until(lambda: core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1)
    task_id = core.db.one("SELECT id FROM tasks")["id"]
    action = pending_action(core, task_id)
    assert (await core.action(action))["status"] == "pending"
    await until(lambda: fake.sent())
    prompt = fake.sent()[0]
    assert prompt["parse_mode"] == "HTML" and "<pre>rm -rf build</pre>" in prompt["text"]
    rows = prompt["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["Allow once", "Decline"]
    assert rows[0][0]["callback_data"] == f"a:{action['id']}:o" and len(rows) == 1
    assert all(len(b["callback_data"].encode()) <= 64 for row in rows for b in row)
    fake.updates.append(press(2, f"a:{action['id']}:o", who=999))
    await until(lambda: fake.sent("answerCallbackQuery"))
    assert core.db.one("SELECT status FROM actions WHERE id=?", (action["id"],))["status"] == "pending"
    fake.updates.append(press(3, f"a:{action['id']}:o"))
    await until(lambda: core.db.one("SELECT status FROM actions WHERE id=?", (action["id"],))["status"] != "pending")
    await until(lambda: fake.sent("editMessageText"))
    assert fake.sent("editMessageText")[0]["text"] == "Approval needed\n\n✅ Allowed once"
    assert fake.sent("answerCallbackQuery")[-1]["text"] == "Allowed once"
    fake.updates.append(press(4, f"a:{action['id']}:d"))
    await until(lambda: fake.sent("editMessageReplyMarkup"))
    stale = fake.sent("answerCallbackQuery")[-1]
    assert stale["show_alert"] and "no longer pending" in stale["text"]


async def test_standing_approvals_and_job_notifications(linked, core):
    task_id = linked.inbound(1, "Save the report")["task_id"]
    action = pending_action(core, task_id, "write_file", {"path": "report.md", "content": "x"})
    assert (await core.action(action))["status"] == "pending"
    prompt = linked.approval_prompt(action["id"])
    assert "<pre>report.md</pre>" in prompt["html"] and len(prompt["buttons"]) == 1
    assert "separate decision" in linked.decide(action["id"], "a")[1]
    job = core.jobs.save({"name": "Nightly", "prompt": "run", "kind": "interval", "spec": "60", "timezone": "UTC"})
    job_task = core.jobs.tick(job["next_run"] + 1)[0]
    command = pending_action(core, job_task, "run_command", {"command": "ls"})
    core.db.execute("UPDATE tasks SET approval_mode='ask' WHERE id=?", (job_task,))
    assert (await core.action(command))["status"] == "pending"
    assert outbox(core)[-1]["approval"] == command["id"]
    prompt = linked.approval_prompt(command["id"])
    assert "Scheduled job: " in prompt["html"] and len(prompt["buttons"]) == 2
    assert linked.decide(command["id"], "c") == (True, "Allowed for this chat")
    assert linked.approval_prompt(command["id"]) is None
    assert linked.decide(command["id"], "x")[0] is False
    core.db.execute("UPDATE schedules SET notify=0 WHERE id=?", (job["id"],))
    before = len(outbox(core))
    assert (await core.action(pending_action(core, job_task, "run_command", {"command": "rm x"})))["status"] == "pending"
    assert len(outbox(core)) == before


# Working indicator, files and pairing


async def test_progress_note_follows_the_steps_and_is_removed(running, core):
    fake = await running()
    fake.updates.append(text_update(1, "Research this"))
    await until(lambda: core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 1)
    task_id = core.db.one("SELECT id FROM tasks")["id"]
    core.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    core.db.execute("UPDATE messages SET status='running' WHERE task_id=?", (task_id,))
    now = time.time()
    for index, args in enumerate([{"operation": "goto", "url": "https://example.com/news"}, {"operation": "observe"}]):
        core.db.execute(
            "INSERT INTO actions VALUES (?,?,?,?,?,?,'completed',NULL,NULL,?,?)",
            (uuid.uuid4().hex, task_id, "e", "browser", json.dumps(args), "h", now + index, now + 900),
        )
    await until(lambda: fake.sent() and "Reading the webpage" in (fake.sent("editMessageText") or fake.sent())[-1]["text"])
    note = fake.sent()[0]
    assert note["text"].startswith("Working…\n• Opening a webpage: example.com/news") and note["disable_notification"]
    with core.db.transaction() as db:
        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
        telegram_enqueue(db, "Here is the news.", task_id)
    await until(lambda: fake.sent("deleteMessage"))
    await until(lambda: fake.sent()[-1]["text"] == "Here is the news.")


async def test_owner_hears_once_when_the_conversation_needs_them(running, core):
    fake = await running()
    task_id = core.telegram.inbound(1, "Send the invoice")["task_id"]
    core.db.execute("UPDATE tasks SET status='uncertain' WHERE id=?", (task_id,))
    await until(lambda: fake.sent())
    assert "couldn’t confirm" in fake.sent()[0]["text"] and "http://testserver" in fake.sent()[0]["text"]
    await asyncio.sleep(0.1)
    assert len(fake.sent()) == 1
    assert core.telegram.inbound(2, "Try again")["task_id"] != task_id


async def test_files_made_by_the_bot_are_delivered(running, core):
    fake = await running()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    task_id = core.telegram.inbound(1, "Make a chart")["task_id"]
    ids = []
    for name, data in [("chart.png", png), ("report.csv", b"a,b\n1,2\n")]:
        action_id, saved = uuid.uuid4().hex, core.attachments.files.put(data, name)
        core.db.execute(
            "INSERT INTO actions VALUES (?,?,?,?,?,?,'completed',NULL,NULL,?,?)",
            (action_id, task_id, "e", "attach_file", "{}", "h", time.time(), time.time() + 900),
        )
        ids.append(uuid.uuid4().hex)
        core.db.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?)",
            (ids[-1], action_id, task_id, saved["digest"], name, saved["mime_type"], saved["size"], int(saved["preview"]), time.time()),
        )
    fake.replies["sendPhoto"] = lambda _: None
    with core.db.transaction() as db:
        telegram_enqueue(db, "Chart attached.", task_id, attachments=ids)
    await until(lambda: fake.sent("sendDocument"))
    assert b'filename="chart.png"' in fake.sent("sendPhoto")[0]["multipart"]
    assert b'filename="report.csv"' in fake.sent("sendDocument")[0]["multipart"]
    assert core.db.one("SELECT status FROM outbox WHERE task_id=?", (task_id,))["status"] == "sent"


async def test_unlinked_bot_only_reveals_the_sender_id(running, core):
    fake = await running(owner=None)
    assert core.telegram.connection()["state"] == "pairing" and core.telegram.owner() == ""
    fake.updates.append(text_update(1, "hello", who=424242))
    await until(lambda: fake.sent())
    assert "424242" in fake.sent()[0]["text"] and fake.sent()[0]["chat_id"] == 424242
    assert core.db.one("SELECT count(*) AS n FROM tasks")["n"] == 0
    assert core.db.one("SELECT count(*) AS n FROM channel_updates")["n"] == 0


# Plugin lifecycle


def test_telegram_is_a_plugin_enabled_from_the_app(owner, core):
    owner.post("/api/plugins/stage", json={"marketplace": "official", "id": "telegram", "version": "2.0.0"})
    blocked = owner.post("/api/plugins/telegram/2.0.0/activate", json={})
    assert blocked.status_code == 400 and "Bot token" in blocked.text
    saved = owner.put(
        "/api/plugins/telegram/2.0.0/configuration", json={TOKEN_KEY: BOT_TOKEN, OWNER_KEY: str(OWNER)}
    ).json()
    assert [(f["label"], f["configured"], f["value"]) for f in saved["fields"]] == [
        ("Bot token", True, ""),
        ("Your Telegram user ID", True, str(OWNER)),
    ]
    assert owner.post("/api/plugins/telegram/2.0.0/activate", json={}).status_code == 200
    assert core.telegram.settings() == {"token": BOT_TOKEN, "owner": str(OWNER)}
    plugin = next(p for p in owner.get("/api/plugins").json()["installed"] if p["id"] == "telegram")
    assert plugin["connection"]["state"] == "off" and "inside ShinBot" in plugin["execution_access"]
    assert "plugin:telegram:2.0.0" not in core.plugins.versions()
    owner.put("/api/plugins/telegram/2.0.0/configuration", json={OWNER_KEY: "not-a-number"})
    with pytest.raises(ValueError, match="is a number"):
        core.telegram.settings()
    assert owner.post("/api/plugins/telegram/disable", json={}).status_code == 200
    assert core.telegram.settings() is None


def test_only_the_shipped_channel_package_is_accepted(core):
    from shinbot.plugins import validate_package

    manifest = {
        "schema_version": 1,
        "id": "telegram",
        "name": "Telegram",
        "version": "9.9.9",
        "description": "Impostor",
        "kind": "channel",
        "license": "MIT",
        "files": ["LICENSE"],
    }
    with pytest.raises(ValueError, match="bundled official"):
        validate_package({"shinbot-plugin.json": json.dumps(manifest).encode(), "LICENSE": b"MIT"})
    core.db.execute(
        "INSERT INTO plugins(id,version,digest,manifest,path,status,marketplace,created) VALUES "
        "('telegram','1.1.0','d','{}','/missing','staged','official',0)"
    )
    from shinbot.plugins import Plugins

    Plugins(core.db, core.config.data / "plugins")
    assert core.db.one("SELECT 1 FROM plugins WHERE id='telegram'") is None


async def test_connector_follows_the_plugin_state(core, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(telegram, "TICK", 0.02)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(telegram.asyncio, "sleep", lambda delay: real_sleep(min(delay, 0.02)))
    worker = asyncio.create_task(telegram.run(core, fake.transport))
    try:
        await real_sleep(0.05)
        assert core.telegram.connection()["state"] == "off" and not fake.calls
        version = enable(core, owner="")
        await until(lambda: core.telegram.connection()["state"] == "pairing")
        assert "@shinbot_test" in core.telegram.connection()["detail"]
        core.plugins.configuration("telegram", version, {OWNER_KEY: str(OWNER)})
        await until(lambda: core.telegram.connection()["state"] == "connected")
        assert core.telegram.owner() == str(OWNER)
        fake.replies["getMe"] = lambda _: error(401, "Unauthorized")
        core.plugins.configuration("telegram", version, {TOKEN_KEY: BOT_TOKEN})
        fake.replies["getUpdates"] = lambda _: error(409, "Conflict: terminated by other getUpdates request")
        await until(lambda: core.telegram.connection()["state"] == "error")
        assert "Another program" in core.telegram.connection()["detail"] and core.telegram.owner() == ""
        del fake.replies["getUpdates"]
        await until(lambda: "rejected the bot token" in core.telegram.connection()["detail"])
        core.plugins.disable("telegram")
        await until(lambda: core.telegram.connection()["state"] == "off")
        calls = len(fake.calls)
        await real_sleep(0.1)
        assert len(fake.calls) == calls
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_host_configured_connector_is_carried_over_once(core, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", str(OWNER))
    await telegram.adopt_host_settings(core)
    assert core.telegram.settings() == {"token": BOT_TOKEN, "owner": str(OWNER)}
    core.plugins.disable("telegram")
    await telegram.adopt_host_settings(core)
    assert core.telegram.settings() is None


def test_bot_token_never_reaches_errors_or_logs():
    exc = telegram.TelegramError(401, "Unauthorized")
    assert BOT_TOKEN not in str(exc) and BOT_TOKEN not in repr(exc)
