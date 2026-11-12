from datetime import datetime

import pytest

from shinbot.jobs import next_run


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def test_cron_skips_dst_gap_and_second_fold():
    after = stamp("2026-03-07T12:00:00+00:00")
    assert next_run("cron", "30 2 * * *", "America/New_York", after) == stamp("2026-03-09T06:30:00+00:00")
    after = stamp("2026-11-01T05:31:00+00:00")
    assert next_run("cron", "30 1 * * *", "America/New_York", after) == stamp("2026-11-02T06:30:00+00:00")


def test_one_time_requires_offset():
    with pytest.raises(ValueError):
        next_run("once", "2027-01-01T10:00:00", "UTC", 0)


def test_schedule_admission_is_atomic_and_deduplicated(application):
    core = application.state.core
    job = core.jobs.save(
        {"name": "Daily", "prompt": "Summarize", "kind": "interval", "spec": "60", "timezone": "UTC"}
    )
    when = job["next_run"]
    assert len(core.jobs.tick(when + 1)) == 1
    assert core.jobs.tick(when + 1) == []
    assert len(core.db.all("SELECT * FROM tasks")) == 1
    assert len(core.db.all("SELECT * FROM occurrences")) == 1
    assert core.jobs.tick(when + 61) == []
    assert core.db.one("SELECT status FROM occurrences ORDER BY intended DESC")["status"] == "overlap"


def test_pause_and_edit_do_not_duplicate_work(application):
    core = application.state.core
    data = {"name": "Daily", "prompt": "Summarize", "kind": "interval", "spec": "60", "timezone": "UTC"}
    job = core.jobs.save(data)
    core.jobs.toggle(job["id"], False)
    assert core.jobs.tick(job["next_run"] + 1) == []
    core.jobs.toggle(job["id"], True)
    job = core.db.one("SELECT * FROM schedules WHERE id=?", (job["id"],))
    core.jobs.tick(job["next_run"] + 1)
    core.jobs.save({**data, "prompt": "Updated"}, job["id"])
    assert core.db.one("SELECT status FROM tasks")["status"] == "cancelled"


@pytest.mark.parametrize(
    "patch,hint",
    [
        ({"kind": "once", "spec": "tomorrow morning"}, "UTC offset"),
        ({"kind": "interval", "spec": "every hour"}, "whole seconds"),
        ({"kind": "cron", "spec": "bad cron"}, "five-field cron"),
        ({"timezone": "IST"}, "Asia/Kolkata"),
    ],
)
def test_invalid_job_forms_explain_how_to_correct_them(owner, patch, hint):
    response = owner.post(
        "/api/jobs",
        json={
            "name": "Reminder",
            "prompt": "Hello",
            "kind": "interval",
            "spec": "60",
            "timezone": "UTC",
            **patch,
        },
    )
    assert response.status_code == 400
    assert hint in response.json()["detail"]
    assert owner.get("/api/jobs").json() == []


def test_invalid_settings_timezone_preserves_saved_preferences(owner):
    saved = owner.get("/api/settings").json()
    response = owner.put("/api/settings", json={**saved, "timezone": "IST"})
    assert response.status_code == 400
    assert "Asia/Kolkata" in response.json()["detail"]
    assert owner.get("/api/settings").json() == saved
