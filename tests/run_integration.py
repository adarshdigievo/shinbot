"""Exercise chat and tool approval against the isolated Docker test deployment."""

import json

from integration_support import approve, checked, connect, fixture_settings, ready, wait


def main():
    client = connect()
    ready(client)
    fixture_settings(client)
    fixture = next(
        provider for provider in checked(client.get("/api/providers")) if provider["id"] == "custom-fixture"
    )
    assert fixture["configured"], "An authless local provider must be usable without an API key"
    for prompt in ["hello fixture", "fixture-note", "fixture-write", "fixture-browser"]:
        response = client.post("/api/tasks", json={"content": prompt})
        response.raise_for_status()
        if prompt == "fixture-write":
            approve(client, response.json()["id"], "write_file")
        task = wait(client, response.json()["id"])
        assert task["messages"][-1]["answer"], task
        print(f"PASS {prompt}: {task['status']}")
    task_id = client.post("/api/tasks", json={"content": "fixture-command"}).json()["id"]
    waiting = wait(client, task_id, "waiting")
    approval = next(a for a in waiting["actions"] if a["status"] == "pending")
    response = client.post(f"/api/actions/{approval['id']}/decision", json={"approved": True})
    response.raise_for_status()
    task = wait(client, task_id)
    assert "approved command completed" in task["messages"][-1]["answer"]
    print("PASS command approval and result")
    failed = checked(client.post("/api/tasks", json={"content": "FIXTURE AUTH ERROR"}))
    failure = wait(client, failed["id"], status="failed")
    public = json.dumps(failure)
    assert "PRIVATE-FIXTURE-API-KEY" not in public
    error = failure["error"].lower()
    assert "key" in error and ("reconnect" in error or "settings" in error), failure["error"]
    print("PASS model authentication failure is actionable and keeps provider secrets private")
    for prompt, hint in [
        ("fixture-empty-answer", "without a reply"),
        ("fixture-truncated-answer", "response limit"),
    ]:
        task = checked(client.post("/api/tasks", json={"content": prompt}))
        failure = wait(client, task["id"], status="failed")
        assert hint in failure["error"], failure
    print("PASS empty and truncated replies show useful errors instead of completed work")
    print("Integration checks completed.")


if __name__ == "__main__":
    main()
