"""Container integration checks; uses the deterministic local model, never paid accounts."""

import json
import subprocess
import uuid

from integration_support import (
    COMPOSE,
    checked,
    connect,
    eventually,
    fixture_settings,
    install_echo_plugin,
    ready,
    wait,
)


def main():
    c = connect()
    ready(c)
    install_echo_plugin(c)
    prefs = fixture_settings(c, computer_auto=True)
    task = checked(c.post("/api/tasks", json={"content": "fixture-plugin"}))
    result = wait(c, task["id"])
    assert "plugin said: hello" in result["messages"][-1]["answer"], result
    print("PASS executable MCP plugin, pinned extension and tool result", flush=True)
    snapshot = checked(c.get("/api/tasks/" + task["id"] + "/snapshot"))
    assert "plugin:personal-echo:1.0.0" in snapshot["docs"]["pi.agent"]["extensions"]
    checked(c.post("/api/tasks/" + task["id"] + "/compact", json={}))
    eventually(
        lambda: (
            not checked(c.get("/api/tasks/" + task["id"] + "/snapshot"))["docs"]
            .get("pi.live", {})
            .get("compactions")
        ),
        "manual compaction to settle",
    )
    checked(
        c.post(
            "/api/tasks/" + task["id"] + "/messages",
            json={"content": "hello after compaction", "request_id": uuid.uuid4().hex},
        )
    )
    result = wait(c, task["id"])
    assert "hello after compaction" in result["messages"][-1]["answer"]
    print("PASS manual context compaction and follow-up", flush=True)
    prefs.update(computer_auto=False)
    checked(c.put("/api/settings", json=prefs))
    task = checked(c.post("/api/tasks", json={"content": "fixture-command"}))
    pending = wait(c, task["id"], "waiting")
    original = pending["actions"][0]["id"]
    subprocess.run([*COMPOSE, "restart", "runtime"], check=True, stdout=subprocess.DEVNULL)
    ready(c)
    current = checked(c.get("/api/tasks/" + task["id"]))
    assert len(current["actions"]) == 1 and current["actions"][0]["id"] == original
    checked(c.post("/api/actions/" + original + "/decision", json={"approved": True}))
    result = wait(c, task["id"])
    assert "approved command completed" in result["messages"][-1]["answer"]
    print("PASS pending approval survives runtime restart without duplicate action", flush=True)
    prefs.update(computer_auto=True)
    checked(c.put("/api/settings", json=prefs))
    task = checked(c.post("/api/tasks", json={"content": "fixture-wait"}))
    eventually(
        lambda: any(
            a["status"] == "executing" for a in checked(c.get("/api/tasks/" + task["id"]))["actions"]
        ),
        "command execution to start",
        timeout=30,
    )
    checked(c.post("/api/tasks/" + task["id"] + "/cancel", json={}))
    eventually(
        lambda: all(
            a["status"] != "executing" for a in checked(c.get("/api/tasks/" + task["id"]))["actions"]
        ),
        "cancelled command to stop",
        timeout=15,
    )
    cancelled = checked(c.get("/api/tasks/" + task["id"]))
    assert cancelled["status"] == "cancelled"
    for action in cancelled["actions"]:
        assert "should not finish" not in (action["result"] or ""), action
        if action["status"] == "completed":
            assert json.loads(action["result"])["exit_code"] != 0, action
    print("PASS cancellation terminates a running command and stays cancelled", flush=True)
    # Human control blocks agent observations until explicitly returned.
    checked(c.post("/api/browser/takeover", json={}))
    task = checked(c.post("/api/tasks", json={"content": "fixture-browser"}))
    eventually(
        lambda: any(
            t["name"] == "browser" and t["status"] == "running"
            for t in checked(c.get("/api/tasks/" + task["id"] + "/snapshot"))["docs"]
            .get("pi.live", {})
            .get("tools", [])
        ),
        "agent to reach the browser tool while the owner has control",
        timeout=30,
    )
    current = checked(c.get("/api/tasks/" + task["id"]))
    assert current["status"] in {"running", "queued"} and not current["actions"]
    checked(c.post("/api/browser/release", json={}))
    assert wait(c, task["id"])["status"] == "completed"
    print("PASS browser takeover blocks agent and handback resumes it", flush=True)
    from datetime import datetime, timedelta, timezone

    job = checked(
        c.post(
            "/api/jobs",
            json={
                "name": "Fixture scheduled job",
                "prompt": "hello scheduled fixture",
                "kind": "once",
                "spec": (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat(),
                "timezone": "UTC",
                "notify": False,
            },
        )
    )
    history = eventually(
        lambda: checked(c.get("/api/jobs/" + job["id"] + "/history")),
        "one-time job admission",
        timeout=20,
    )
    assert len(history) == 1 and history[0]["status"] == "admitted"
    assert wait(c, history[0]["task_id"])["status"] == "completed"
    print("PASS one-time job service admission and execution", flush=True)
    # Restart all services without deleting any volumes.
    note = checked(c.post("/api/notes", json={"title": "Restart survives", "body": "Persistent note"}))
    subprocess.run(
        [*COMPOSE, "restart", "control", "runtime", "computer", "browser"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    c.close()
    c = connect()
    ready(c)
    assert any(n["id"] == note["id"] for n in checked(c.get("/api/notes")))
    result = wait(c, checked(c.post("/api/tasks", json={"content": "fixture-plugin"}))["id"])
    assert "plugin said: hello" in result["messages"][-1]["answer"]
    print("PASS all-service restart preserves account, notes and executable plugin", flush=True)


if __name__ == "__main__":
    main()
