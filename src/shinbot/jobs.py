import json
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .db import packed, uid


def time_zone(value):
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValueError("Choose a timezone such as Asia/Kolkata, America/New_York, or UTC.") from exc


def next_run(kind, spec, tz, after):
    zone = time_zone(tz)
    if kind == "once":
        try:
            stamp = datetime.fromisoformat(spec.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(
                "Enter a date and time with a UTC offset, such as 2027-01-15T09:00:00+05:30."
            ) from exc
        if stamp.tzinfo is None:
            raise ValueError("Include the UTC offset in the time, such as +05:30 or Z for UTC.")
        return stamp.timestamp() if stamp.timestamp() > after else None
    if kind == "interval":
        try:
            if not isinstance(spec, (str, int)) or isinstance(spec, bool):
                raise ValueError("Interval must be a whole number.")
            seconds = int(spec)
        except (ValueError, TypeError) as exc:
            raise ValueError("Enter the interval in whole seconds, such as 3600 for one hour.") from exc
        if not 60 <= seconds <= 31536000:
            raise ValueError("Intervals must be between one minute and one year.")
        return after + seconds
    if kind != "cron" or not isinstance(spec, str) or len(spec.split()) != 5 or not croniter.is_valid(spec):
        raise ValueError("Use a five-field cron expression, such as 0 9 * * * for 9 AM each day.")
    # Iterate wall time, then round-trip through UTC to reject DST gaps and fold=1.
    wall = datetime.fromtimestamp(after, zone).replace(tzinfo=None)
    iterator = croniter(spec, wall)
    for _ in range(10000):
        candidate = iterator.get_next(datetime).replace(tzinfo=zone, fold=0)
        stamp = candidate.timestamp()
        back = datetime.fromtimestamp(stamp, zone)
        if back.replace(tzinfo=None) == candidate.replace(tzinfo=None) and stamp > after:
            return stamp
    raise ValueError("No valid upcoming run was found.")


def schedule_preview(data):
    zone = time_zone(data.get("timezone", "UTC"))
    kind, spec = data.get("kind"), data.get("spec")
    note = ""
    if kind not in {"once", "interval", "cron"}:
        raise ValueError("Choose how often this job should run.")
    if kind == "once" and "local_time" in data:
        value = data["local_time"]
        if not isinstance(value, str) or len(value) > 30:
            raise ValueError("Choose a valid date and time.")
        try:
            wall = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("Choose a valid date and time.") from None
        if wall.tzinfo is not None:
            raise ValueError("Choose a local time and select its timezone separately.")
        aware = wall.replace(tzinfo=zone, fold=0)
        if datetime.fromtimestamp(aware.timestamp(), zone).replace(tzinfo=None) != wall:
            raise ValueError("That time is skipped by daylight saving time. Choose a different time.")
        if aware.utcoffset() != wall.replace(tzinfo=zone, fold=1).utcoffset():
            note = "This time occurs twice as clocks change. The first occurrence will be used."
        spec = aware.isoformat()
    run = next_run(kind, spec, str(zone), time.time())
    if run is None:
        raise ValueError("Choose a future time.")
    return {"kind": kind, "spec": str(spec), "timezone": str(zone), "next_run": run, "note": note}


class Jobs:
    def __init__(self, db, create_task):
        self.db, self.create_task = db, create_task

    def save(self, data, job_id=None, *, new_id=None, connection=None):
        now = time.time()
        old = self.db.one("SELECT * FROM schedules WHERE id=?", (job_id,)) if job_id else None
        if job_id and not old:
            raise ValueError("Job not found.")
        if old and old["archived"]:
            raise ValueError("Restore this job before editing it.")
        if old:
            data = {**old, "grants": json.loads(old["grants"]), **data}
        if not self.db.one("SELECT 1 FROM bots WHERE id=? AND archived=0", (data.get("bot_id", "default"),)):
            raise ValueError("Choose an active bot for this job in Home before saving it.")
        grants = data.get("grants", {})
        if (
            not isinstance(grants, dict)
            or not set(grants) <= {"computer", "browser", "extensions", "memory"}
            or any(type(v) is not bool for v in grants.values())
        ):
            raise ValueError("Invalid job permissions.")
        if type(data.get("lateness", 3600)) is not int or not 0 <= data.get("lateness", 3600) <= 31536000:
            raise ValueError("Invalid job lateness.")
        if data.get("notify", True) not in (True, False, 0, 1):
            raise ValueError("Invalid notification setting.")
        if "enabled" in data and data["enabled"] not in (True, False, 0, 1):
            raise ValueError("Choose whether this job is enabled.")
        if not all(isinstance(data.get(field), str) for field in ("name", "prompt")):
            raise ValueError("Give this job a name and describe what the bot should do.")
        name, prompt = data["name"].strip(), data["prompt"].strip()
        if not name or not prompt or len(prompt) > 20000 or len(name) > 200:
            raise ValueError("A job needs a name and a prompt of at most 20,000 characters.")
        if data.get("kind") not in {"once", "interval", "cron"}:
            raise ValueError("Choose how often this job should run.")
        run = next_run(data["kind"], data.get("spec"), data.get("timezone"), now)
        if run is None:
            raise ValueError("Choose a future time.")
        key = job_id or new_id or uid()
        with nullcontext(connection) if connection is not None else self.db.transaction() as db:
            old = db.execute("SELECT * FROM schedules WHERE id=?", (key,)).fetchone()
            revision = old["revision"] + 1 if old else 1
            # Messages admitted by a previous revision but not dispatched are withdrawn.
            if old:
                db.execute(
                    "UPDATE messages SET status='cancelled' WHERE status='queued' AND task_id IN "
                    "(SELECT task_id FROM occurrences WHERE schedule_id=?)",
                    (key,),
                )
                db.execute(
                    "UPDATE tasks SET status='cancelled' WHERE status='queued' AND id IN "
                    "(SELECT task_id FROM occurrences WHERE schedule_id=?)",
                    (key,),
                )
            db.execute(
                "INSERT OR REPLACE INTO schedules(id,name,prompt,kind,spec,timezone,enabled,revision,next_run,grants,lateness,notify,last_result,created,bot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    name,
                    prompt,
                    data["kind"],
                    data["spec"],
                    data["timezone"],
                    int(data.get("enabled", old["enabled"] if old else 1)),
                    revision,
                    run,
                    packed(data.get("grants", {})),
                    int(data.get("lateness", 3600)),
                    int(data.get("notify", True)),
                    old["last_result"] if old else None,
                    old["created"] if old else now,
                    data.get("bot_id", "default"),
                ),
            )
        if connection is not None:
            return dict(connection.execute("SELECT * FROM schedules WHERE id=?", (key,)).fetchone())
        return self.db.one("SELECT * FROM schedules WHERE id=?", (key,))

    def toggle(self, key, enabled):
        job = self.db.one("SELECT * FROM schedules WHERE id=?", (key,))
        if not job:
            raise ValueError("Job not found.")
        if job["archived"]:
            raise ValueError("Restore this job before resuming it.")
        if enabled and not self.db.one("SELECT 1 FROM bots WHERE id=? AND archived=0", (job["bot_id"],)):
            raise ValueError("This job belongs to an archived bot. Choose an active bot before resuming it.")
        run = next_run(job["kind"], job["spec"], job["timezone"], time.time()) if enabled else job["next_run"]
        if enabled and (run is None or (job["kind"] == "once" and job["next_run"] is None)):
            raise ValueError("This one-time job already passed. Edit it to choose a new time.")
        self.db.execute(
            "UPDATE schedules SET enabled=?,next_run=?,last_error='' WHERE id=?", (int(enabled), run, key)
        )

    def archive(self, key, archived=True):
        with self.db.transaction() as db:
            job = db.execute("SELECT * FROM schedules WHERE id=?", (key,)).fetchone()
            if not job:
                raise ValueError("Job not found.")
            if not archived and not job["archived"]:
                return
            db.execute("UPDATE schedules SET archived=?,enabled=0 WHERE id=?", (int(archived), key))
            if archived:
                db.execute(
                    "UPDATE messages SET status='cancelled' WHERE status='queued' AND task_id IN "
                    "(SELECT task_id FROM occurrences WHERE schedule_id=?)",
                    (key,),
                )
                db.execute(
                    "UPDATE tasks SET status='cancelled' WHERE status='queued' AND id IN "
                    "(SELECT task_id FROM occurrences WHERE schedule_id=?)",
                    (key,),
                )

    def tick(self, now=None):
        now = now or time.time()
        admitted = []
        for job in self.db.all(
            "SELECT * FROM schedules WHERE archived=0 AND enabled=1 AND next_run<=?", (now,)
        ):
            try:
                task_id = self.admit(job, now)
                if task_id:
                    admitted.append(task_id)
            except (ValueError, KeyError, TypeError) as exc:
                reason = str(exc)[:1000] if isinstance(exc, ValueError) else "Check this job's settings."
                with self.db.transaction() as db:
                    db.execute("UPDATE schedules SET enabled=0,last_error=? WHERE id=?", (reason, job["id"]))
                    db.execute(
                        "INSERT OR IGNORE INTO occurrences(id,schedule_id,revision,intended,task_id,status,error) VALUES (?,?,?,?,NULL,'failed',?)",
                        (uid(), job["id"], job["revision"], job["next_run"], reason),
                    )
                self.db.event("error", f"Paused {job['name']}: {reason}")
        return admitted

    def admit(self, job, now):
        with self.db.transaction() as db:
            current = db.execute("SELECT * FROM schedules WHERE id=?", (job["id"],)).fetchone()
            if (
                not current
                or current["archived"]
                or not current["enabled"]
                or current["revision"] != job["revision"]
                or current["next_run"] != job["next_run"]
            ):
                return None
            intended = job["next_run"]
            following = None
            if job["kind"] == "interval":
                seconds = int(job["spec"])
                intended += int((now - intended) // seconds) * seconds
                following = intended + seconds
            elif job["kind"] == "cron":
                # One coalesced occurrence, bounded even after a long outage.
                following = next_run("cron", job["spec"], job["timezone"], now)
                zone = ZoneInfo(job["timezone"])
                wall = datetime.fromtimestamp(now, zone).replace(tzinfo=None)
                iterator = croniter(job["spec"], wall + timedelta(microseconds=1))
                # Inclusive most recent valid occurrence; skip spring gaps and second folds.
                cursor = iterator.get_prev(datetime)
                for _ in range(10000):
                    candidate = cursor.replace(tzinfo=zone, fold=0)
                    stamp = candidate.timestamp()
                    if stamp <= now and datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == cursor:
                        intended = stamp
                        break
                    cursor = iterator.get_prev(datetime)
            busy = db.execute(
                "SELECT t.id,t.status FROM tasks t JOIN occurrences o ON o.task_id=t.id "
                "WHERE o.schedule_id=? AND t.status IN ('queued','running','waiting','paused','uncertain')",
                (job["id"],),
            ).fetchone()
            status = "overlap" if busy else "missed" if now - intended > job["lateness"] else "admitted"
            existing = db.execute(
                "SELECT 1 FROM occurrences WHERE schedule_id=? AND revision=? AND intended=?",
                (job["id"], job["revision"], intended),
            ).fetchone()
            if existing:
                return None
            previous = db.execute(
                "SELECT status FROM occurrences WHERE schedule_id=? ORDER BY intended DESC LIMIT 1",
                (job["id"],),
            ).fetchone()
            if busy and (not previous or previous["status"] != "overlap"):
                db.execute(
                    "INSERT INTO events(kind,title,task_id,created) VALUES ('warning',?,?,?)",
                    (
                        f"{job['name']}: the next run is waiting for an earlier task ({busy['status']}). Open that task to resolve it.",
                        busy["id"],
                        now,
                    ),
                )
            task = (
                self.create_task(
                    job["prompt"],
                    title=job["name"],
                    source=f"job:{job['id']}",
                    grants=json.loads(job["grants"]),
                    connection=db,
                    bot_id=job["bot_id"],
                )
                if status == "admitted"
                else None
            )
            db.execute(
                "INSERT INTO occurrences(id,schedule_id,revision,intended,task_id,status) VALUES (?,?,?,?,?,?)",
                (uid(), job["id"], job["revision"], intended, task["id"] if task else None, status),
            )
            db.execute(
                "UPDATE schedules SET next_run=?,enabled=? WHERE id=?",
                (following, int(following is not None), job["id"]),
            )
            return task["id"] if task else None
