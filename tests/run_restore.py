"""Round-trip the isolated integration deployment, including real runtime and browser state."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from integration_support import (
    COMPOSE,
    HOME,
    checked,
    connect,
    fixture_settings,
    install_echo_plugin,
    ready,
    wait,
)


def command(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def main():
    c = connect()
    if c.get("/api/status").json()["restore_hold"]:
        c.post("/api/recovery/release", json={"old_installation_stopped": True}).raise_for_status()
    ready(c)
    install_echo_plugin(c)
    prefs = fixture_settings(c, computer_auto=False)
    pending = c.post("/api/tasks", json={"content": "fixture-command"}).json()["id"]
    wait(c, pending, "waiting")
    note = c.post("/api/notes", json={"title": "Backup fixture", "body": "Before backup"}).json()
    private = checked(c.post("/api/bots", json={"profile": {"name": "Backup browser", "browser": "private"}}))
    cookie_task = checked(
        c.post("/api/tasks", json={"content": "fixture-cookie-set:backup-cookie", "bot_id": private["id"]})
    )
    wait(c, cookie_task["id"])
    job = checked(
        c.post(
            "/api/jobs",
            json={
                "name": "Backed-up schedule",
                "prompt": "hello",
                "kind": "interval",
                "spec": "86400",
                "timezone": "UTC",
            },
        )
    )
    assert job["enabled"]
    command(
        *COMPOSE,
        "exec",
        "-T",
        "--user",
        "1001:1001",
        "computer",
        "python",
        "-m",
        "venv",
        "/workspace/backup-fixture-venv",
    )
    archive = HOME / f"backup-{uuid.uuid4().hex}.tar.gz"
    command(str(Path(sys.executable).parent / "shinbot"), "backup", str(archive))
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.stat().st_uid == os.getuid()
    print("PASS full backup with Chromium profiles and workspace virtual-environment symlinks", flush=True)
    c = connect()
    c.post(
        "/api/notes",
        json={
            "id": note["id"],
            "title": "Changed after backup",
            "body": "After",
            "revision": note["revision"],
        },
    ).raise_for_status()
    old_session = c.headers["Authorization"]
    command(str(Path(sys.executable).parent / "shinbot"), "restore", str(archive), "--replace")
    c = connect()
    assert c.get("/api/notes", headers={"Authorization": old_session}).status_code == 401
    assert c.get("/api/status").json()["restore_hold"]
    assert c.post("/api/tasks", json={"content": "must not run"}).status_code == 409
    restored = next(n for n in c.get("/api/notes").json() if n["id"] == note["id"])
    assert restored["body"] == "Before backup"
    restored_job = next(j for j in checked(c.get("/api/jobs")) if j["id"] == job["id"])
    assert not restored_job["enabled"]
    command(
        *COMPOSE,
        "exec",
        "-T",
        "--user",
        "1001:1001",
        "computer",
        "/workspace/backup-fixture-venv/bin/python",
        "--version",
    )
    held = c.get("/api/tasks/" + pending).json()
    assert all(a["status"] == "denied" for a in held["actions"])
    c.post("/api/recovery/release", json={"old_installation_stopped": True}).raise_for_status()
    assert not c.get("/api/status").json()["restore_hold"]
    assert c.get("/api/tasks/" + pending).json()["status"] == "failed"
    print(
        "PASS restore revokes sessions, holds old work, pauses schedules and preserves file ownership",
        flush=True,
    )
    ready(c)
    prefs.update(computer_auto=True)
    c.put("/api/settings", json=prefs).raise_for_status()
    task = c.post("/api/tasks", json={"content": "fixture-plugin"}).json()
    assert "plugin said: hello" in wait(c, task["id"])["messages"][-1]["answer"]
    print("PASS restored MCP plugin runs without reactivation", flush=True)
    task = c.post("/api/tasks", json={"content": "fixture-cookie-read", "bot_id": private["id"]}).json()
    result = wait(c, task["id"])
    action = next(a for a in result["actions"] if a["tool"] == "browser" and a["status"] == "completed")
    assert "backup-cookie" in json.loads(action["result"])["content"][0]["text"]
    print("PASS private browser cookies and profile identity survive full restore", flush=True)


if __name__ == "__main__":
    main()
