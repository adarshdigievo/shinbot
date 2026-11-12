"""Exercise successful updates, failed migrations, interrupted updates and rollback in Docker."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from integration_support import COMPOSE, HOME, checked, connect, eventually, ready

from shinbot.maintenance import compose_document, unpack

CLI = str(Path(sys.executable).parent / "shinbot")


def command(*args, env=None, succeeds=True):
    result = subprocess.run([CLI, *args], env=env, capture_output=True, text=True, timeout=240)
    if succeeds and result.returncode:
        raise AssertionError(f"CLI {args[0]} failed: {result.stdout[-1500:]} {result.stderr[-1500:]}")
    return result


def release_hold(c):
    if checked(c.get("/api/status"))["restore_hold"]:
        checked(c.post("/api/recovery/release", json={"old_installation_stopped": True}))
    ready(c)


def main():
    c = connect()
    release_hold(c)
    config = json.loads(
        subprocess.run(
            [*COMPOSE, "config", "--format", "json"], check=True, capture_output=True, text=True
        ).stdout
    )
    # A literal dollar must survive saving and replaying a resolved Compose manifest.
    config["services"]["control"]["environment"]["SHINBOT_TEST_LITERAL"] = "keep$literal"
    good = HOME / "good-compose.json"
    compose_document(good, config)
    env = {**os.environ, "SHINBOT_COMPOSE_FILE": str(good)}
    command("start", "--no-open-browser", env=env)
    command("update", "--timeout", "45", env=env)
    c = connect()
    note = checked(c.post("/api/notes", json={"title": "Recovery marker", "body": "before update"}))
    command("update", "--timeout", "45", env=env)
    c = connect()
    assert not checked(c.get("/api/status"))["restore_hold"]
    checked(c.post("/api/notes", json={**note, "body": "after update"}))
    command("rollback", "--timeout", "45", env=env)
    c = connect()
    assert checked(c.get("/api/status"))["restore_hold"]
    assert next(n for n in checked(c.get("/api/notes")) if n["id"] == note["id"])["body"] == "before update"
    newer = max((HOME / "backups").glob("before-rollback-*.tar.gz"), key=lambda p: p.stat().st_mtime_ns)
    with unpack(newer) as (volume_archive, root, manifest):
        assert manifest["format"] == 2 and root
        with tarfile.open(volume_archive) as archive, tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.sqlite"
            for suffix in ("", "-wal", "-shm"):
                name = "control-data/shinbot.sqlite" + suffix
                if name in archive.getnames():
                    Path(str(database) + suffix).write_bytes(archive.extractfile(name).read())
            with sqlite3.connect(database) as db:
                assert (
                    db.execute("SELECT body FROM notes WHERE id=?", (note["id"],)).fetchone()[0]
                    == "after update"
                )
    check = subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "control",
            "python",
            "-c",
            "import os; assert os.environ['SHINBOT_TEST_LITERAL']=='keep$literal'",
        ],
        capture_output=True,
    )
    assert check.returncode == 0
    assert "already been restored" in command("rollback", env=env).stdout
    print(
        "PASS successful update, manual rollback, retained newer work and literal configuration values",
        flush=True,
    )
    release_hold(c)

    broken = copy.deepcopy(config)
    broken["services"]["control"]["command"] = [
        "python",
        "-c",
        "import sqlite3; db=sqlite3.connect('/data/shinbot.sqlite'); "
        "db.execute(\"UPDATE notes SET body='broken migration'\"); db.commit(); raise SystemExit(42)",
    ]
    bad = HOME / "broken-compose.json"
    compose_document(bad, broken)
    bad_env = {**env, "SHINBOT_COMPOSE_FILE": str(bad)}
    failed = command("update", "--timeout", "10", env=bad_env, succeeds=False)
    assert failed.returncode != 0 and "Previous release restored" in failed.stderr
    c = connect()
    assert checked(c.get("/api/status"))["restore_hold"]
    assert next(n for n in checked(c.get("/api/notes")) if n["id"] == note["id"])["body"] == "before update"
    assert c.post("/api/tasks", json={"content": "must stay held"}).status_code == 409
    print("PASS failed startup restores the old image and database, with automated work held", flush=True)
    release_hold(c)

    process = subprocess.Popen(
        [CLI, "update", "--timeout", "120"], env=bad_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:

        def started():
            state = json.loads((HOME / "recovery.json").read_text())
            if state.get("pending"):
                return state

        eventually(started, "persisted update checkpoint", timeout=90)
        process.kill()
        process.wait(timeout=10)
        blocked = command("start", "--no-open-browser", env=env, succeeds=False)
        assert blocked.returncode != 0 and "interrupted" in blocked.stderr
        command("rollback", "--timeout", "45", env=env)
        c = connect()
        assert checked(c.get("/api/status"))["restore_hold"]
        assert (
            next(n for n in checked(c.get("/api/notes")) if n["id"] == note["id"])["body"] == "before update"
        )
        release_hold(c)
        print("PASS killed updater recovers from its durable checkpoint without duplicate work", flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
