"""Disconnect only runner-owned Docker networks and verify observable effects."""

import json
import subprocess
import time
import uuid
from contextlib import contextmanager

from integration_support import COMPOSE, PROJECT, checked, connect, eventually, fixture_settings, ready, wait

DOCKER = COMPOSE[: COMPOSE.index("compose")]


def compose(*args):
    return subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True).stdout.strip()


@contextmanager
def disconnected(service, network):
    container = compose("ps", "-q", service)
    name = PROJECT + "_" + network
    details = json.loads(subprocess.check_output([*DOCKER, "inspect", container]))[0]
    assert details["Config"]["Labels"]["com.docker.compose.project"] == PROJECT
    assert name in details["NetworkSettings"]["Networks"]
    subprocess.run([*DOCKER, "network", "disconnect", name, container], check=True)
    try:
        yield
    finally:
        subprocess.run([*DOCKER, "network", "connect", "--alias", service, name, container], check=True)


def count(marker):
    return int(
        compose(
            "exec",
            "-T",
            "computer",
            "python",
            "-c",
            f"from pathlib import Path; p=Path('/workspace/recovery-{marker}.txt'); print(len(p.read_text().splitlines()) if p.exists() else 0)",
        )
    )


def main():
    client = connect()
    ready(client)
    fixture_settings(client, computer_auto=True, approval_mode="full")

    def start(delay=0):
        marker = uuid.uuid4().hex
        task = checked(client.post("/api/tasks", json={"content": f"fixture-recovery:{marker}:{delay}"}))
        return task["id"], marker

    def detail(task):
        return checked(client.get(f"/api/tasks/{task}"))

    def once(task, marker):
        result = wait(client, task, timeout=180)
        assert len(result["actions"]) == 1 and len(result["messages"]) == 1, result
        assert result["actions"][0]["error"] is None and result["wait_reason"] is None
        assert count(marker) == 1
        return result

    with disconnected("fixture", "runtime-net"):
        task, marker = start()
        eventually(lambda: detail(task).get("wait_reason"), "model outage waiting state", timeout=150)
        eventually(
            lambda: (
                checked(client.get(f"/api/tasks/{task}/snapshot"))["docs"]
                .get("pi.live", {})
                .get("generation", {})
                .get("attempt", 0)
                >= 4
            ),
            "model outage to outlast the former two-retry limit",
            timeout=180,
        )
        before = detail(task)["messages"][0]["submission_id"]
        compose("kill", "-s", "SIGKILL", "control", "runtime")
        compose("up", "-d", "--no-build", "control", "runtime")
        ready(client)
        eventually(lambda: detail(task).get("wait_reason"), "persisted model wait after restart", timeout=150)
        assert detail(task)["messages"][0]["submission_id"] == before
        assert count(marker) == 0
    once(task, marker)
    print(
        "PASS model disconnection survives gateway/runtime hard restart and resumes one original submission",
        flush=True,
    )

    marker = uuid.uuid4().hex
    task = checked(client.post("/api/tasks", json={"content": f"fixture-recovery-stream:{marker}:0"}))["id"]
    eventually(
        lambda: any(
            part.get("type") == "toolCall"
            for part in checked(client.get(f"/api/tasks/{task}/snapshot"))["docs"]
            .get("pi.live", {})
            .get("generation", {})
            .get("message", {})
            .get("content", [])
        ),
        "partial tool call before stream disconnection",
    )
    with disconnected("fixture", "runtime-net"):
        eventually(lambda: detail(task).get("wait_reason"), "interrupted model stream", timeout=150)
        assert count(marker) == 0 and not detail(task)["actions"]
    once(task, marker)
    print("PASS interrupted model stream retries without executing its partial tool call", flush=True)

    with disconnected("computer", "computer-net"):
        task, marker = start()
        eventually(lambda: detail(task).get("wait_reason"), "offline executor before dispatch", timeout=120)
        assert count(marker) == 0
    once(task, marker)
    print("PASS disconnected executor resumes automatically with one effect", flush=True)

    task, marker = start(40)
    eventually(
        lambda: "still running" in (detail(task).get("wait_reason") or ""),
        "live receipt after the HTTP response deadline",
    )
    once(task, marker)
    print("PASS long-running action finishes through receipt polling without a second execution", flush=True)

    with disconnected("browser", "browser-net"):
        task = checked(client.post("/api/tasks", json={"content": "fixture-browser"}))["id"]
        eventually(lambda: detail(task).get("wait_reason"), "browser connection wait", timeout=120)
    result = wait(client, task, timeout=120)
    assert len(result["actions"]) == 1
    print("PASS browser service disconnection recovers one original tool call", flush=True)

    task, marker = start(15)
    eventually(lambda: count(marker) == 1, "external effect before response loss")
    original = detail(task)["actions"][0]["id"]
    with disconnected("computer", "computer-net"):
        compose("restart", "control", "runtime")
        ready(client)
        eventually(lambda: detail(task).get("wait_reason"), "executor receipt waiting state")
        assert detail(task)["actions"][0]["id"] == original
    result = once(task, marker)
    assert result["actions"][0]["id"] == original
    print("PASS lost executor response reconciles its receipt without duplicating the append", flush=True)

    fixture_settings(client, computer_auto=False)
    task, marker = start()
    pending = wait(client, task, "waiting")
    original = pending["actions"][0]["id"]
    with disconnected("control", "runtime-net"):
        checked(client.post(f"/api/actions/{original}/decision", json={"approved": True}))
        # Keep the connection broken across the bridge's request deadline.
        time.sleep(32)
        assert count(marker) == 0
        compose("restart", "runtime")
    ready(client)
    result = once(task, marker)
    assert result["actions"][0]["id"] == original
    print("PASS runtime/gateway partition and runtime restart preserve the approved action ID", flush=True)

    fixture_settings(client, computer_auto=True, approval_mode="full")
    task, marker = start(120)
    eventually(lambda: count(marker) == 1, "effect before executor crash")
    compose("kill", "-s", "SIGKILL", "computer")
    compose("up", "-d", "--no-build", "computer")
    compose("restart", "control", "runtime")
    ready(client)
    uncertain = wait(client, task, "uncertain", timeout=120)
    assert len(uncertain["actions"]) == 1 and uncertain["actions"][0]["status"] == "uncertain"
    compose("restart", "control", "runtime")
    ready(client)
    assert detail(task)["status"] == "uncertain" and count(marker) == 1
    print("PASS executor crash holds an uncertain write for review across another restart", flush=True)

    with disconnected("fixture", "runtime-net"):
        task, marker = start()
        eventually(lambda: detail(task).get("wait_reason"), "offline task before cancellation", timeout=150)
        checked(client.post(f"/api/tasks/{task}/cancel", json={}))
    compose("restart", "control", "runtime")
    ready(client)
    assert detail(task)["status"] == "cancelled" and count(marker) == 0
    print("PASS cancellation while offline stays cancelled after reconnection and restart", flush=True)
    task, marker = start()
    once(task, marker)
    print("PASS fresh work completes after recovery checks", flush=True)
    client.close()


if __name__ == "__main__":
    main()
