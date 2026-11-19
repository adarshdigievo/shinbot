"""One daily pass over records that nothing reads once they age.

Conversations, actions, attachments, notes and memories are the owner's history and are never pruned.
"""

import asyncio
import logging
import sqlite3
import time

from .db import packed

log = logging.getLogger(__name__)
DAY = 86400


def prune(db, now=None):
    now = now or time.time()
    statements = [
        # Expired sessions already fail authentication.
        ("sessions", "DELETE FROM sessions WHERE expires<?", (now,)),
        # Spending caps only read the last 24 hours.
        ("usage_ledger", "DELETE FROM usage_ledger WHERE created<?", (now - 90 * DAY,)),
        # Chats show a task's latest 100 events and notifications the latest 200. Replies saved before
        # messages recorded their own finish time still take it from their event, so those stay.
        (
            "events",
            "DELETE FROM events WHERE created<? AND NOT (kind IN ('completed','failed') AND task_id IN "
            "(SELECT task_id FROM messages WHERE finished IS NULL AND (answer IS NOT NULL OR error IS NOT NULL)))",
            (now - 180 * DAY,),
        ),
        (
            "notification_reads",
            "DELETE FROM notification_reads WHERE id LIKE 'event:%' AND "
            "CAST(substr(id,7) AS INTEGER) NOT IN (SELECT id FROM events)",
            (),
        ),
        # Job history lists 100 runs. Keep 200 per job, anything recent, and every run still in progress.
        (
            "occurrences",
            "DELETE FROM occurrences WHERE intended<? AND id NOT IN (SELECT id FROM (SELECT id,ROW_NUMBER() OVER "
            "(PARTITION BY schedule_id ORDER BY intended DESC) AS position FROM occurrences) WHERE position<=200) "
            "AND (task_id IS NULL OR task_id IN (SELECT id FROM tasks WHERE status IN ('completed','failed','cancelled')))",
            (now - 30 * DAY,),
        ),
        # Delivered messages only; failed and uncertain deliveries stay for review.
        ("outbox", "DELETE FROM outbox WHERE status='sent' AND created<?", (now - 30 * DAY,)),
        # Telegram never redelivers below the saved offset; keep a wide margin under it anyway.
        (
            "channel_updates",
            "DELETE FROM channel_updates WHERE channel='telegram' AND update_id NOT GLOB '*[^0-9]*' AND "
            "CAST(update_id AS INTEGER)<CAST((SELECT value FROM settings WHERE key='telegram_offset') AS INTEGER)-10000",
            (),
        ),
        # A client retries a request within seconds; the receipt outlives that by a month.
        (
            "task_requests",
            "DELETE FROM task_requests WHERE task_id IN (SELECT id FROM tasks WHERE created<?)",
            (now - 30 * DAY,),
        ),
        # Hook receipts written before they carried a time start their month now.
        (
            "hook receipts",
            "UPDATE settings SET value=json_set(value,'$.at',?) WHERE key GLOB 'hook-[0-9a-f]*' "
            "AND json_extract(value,'$.at') IS NULL",
            (now,),
        ),
        (
            "hook receipts",
            "DELETE FROM settings WHERE key GLOB 'hook-[0-9a-f]*' AND json_extract(value,'$.at')<?",
            (now - 30 * DAY,),
        ),
        (
            "hook warnings",
            "UPDATE settings SET value=? WHERE key LIKE 'hook-warning:%' AND value='true'",
            (packed(now),),
        ),
        (
            "hook warnings",
            "DELETE FROM settings WHERE key LIKE 'hook-warning:%' AND CAST(value AS REAL)<?",
            (now - 30 * DAY,),
        ),
    ]
    removed = {}
    for name, query, args in statements:
        # Each table stands alone: one that changed shape must not stop the others.
        try:
            count = db.execute(query, args)
            removed[name] = removed.get(name, 0) + (count if query.startswith("DELETE") else 0)
        except sqlite3.Error as exc:
            log.warning("Retention skipped %s: %s", name, type(exc).__name__)
    db.set("retention_at", now)
    return removed


async def run(db):
    while True:
        try:
            if time.time() - db.setting("retention_at", 0) >= DAY:
                await asyncio.to_thread(prune, db)
        except Exception as exc:
            log.warning("Retention retry: %s", type(exc).__name__)
        await asyncio.sleep(3600)
