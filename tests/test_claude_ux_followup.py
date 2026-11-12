"""Follow-up audit regression: restore must not pause a job that was never archived."""


def test_restore_does_not_pause_an_active_job(owner, application):
    core = application.state.core
    job = core.jobs.save(
        {"name": "Active", "prompt": "Hello", "kind": "interval", "spec": "3600", "timezone": "UTC"}
    )
    response = owner.post(f"/api/jobs/{job['id']}/restore")
    saved = core.db.one("SELECT enabled,archived FROM schedules WHERE id=?", (job["id"],))
    assert response.status_code == 400 or saved["enabled"] == 1, (response.status_code, dict(saved))
