"""Private, isolated context for the explicitly provisioned Docker test lane."""

import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]


def context():
    names = ("HOME", "PROJECT", "URL", "STATE")
    values = {name: os.environ.get("SHINBOT_TEST_" + name, "") for name in names}
    if not all(values.values()):
        raise RuntimeError("Run tests/run_e2e.py first; integration tests require an isolated test context.")
    home = Path(values["HOME"]).resolve()
    marker = home / "test-deployment.json"
    project = values["PROJECT"]
    url = urlparse(values["URL"])
    if not re.fullmatch(r"shinbot-test-[a-f0-9]{12}", project):
        raise RuntimeError("Refusing a Docker project that was not created by the test runner.")
    if not marker.is_file() or json.loads(marker.read_text()) != {
        "project": project,
        "url": values["URL"],
    }:
        raise RuntimeError("The test deployment marker does not match the requested context.")
    state = Path(values["STATE"]).resolve()
    if state.parent != home or home == (Path.home() / ".shinbot").resolve():
        raise RuntimeError("Test credentials and deployment must stay inside their private test home.")
    if url.scheme != "http" or url.hostname != "localhost" or not url.port or url.port == 8765:
        raise RuntimeError("Integration tests require a dedicated localhost test port.")
    saved = dict(line.split("=", 1) for line in (home / "deploy.env").read_text().splitlines() if "=" in line)
    if saved.get("SHINBOT_PROJECT") != project or saved.get("SHINBOT_ORIGIN") != values["URL"]:
        raise RuntimeError("Test deployment settings do not match the isolated context.")
    # Shell environment takes precedence over Compose's --env-file. Reject anything
    # that could silently point this run at the owner's port or deployment instead.
    for key, value in saved.items():
        if key in os.environ and os.environ[key] != value:
            raise RuntimeError(f"Conflicting {key}; use the environment created by tests/run_e2e.py.")
    os.environ["SHINBOT_HOME"] = str(home)
    return home, project, values["URL"], state


HOME, PROJECT, URL, STATE = context()
COMPOSE = ["docker"]
_saved_deployment = dict(
    line.split("=", 1) for line in (HOME / "deploy.env").read_text().splitlines() if "=" in line
)
_docker_context = _saved_deployment.get("SHINBOT_DOCKER_CONTEXT") or os.environ.get("SHINBOT_DOCKER_CONTEXT")
if _docker_context:
    COMPOSE += ["--context", _docker_context]
COMPOSE += [
    "compose",
    "--project-name",
    PROJECT,
    "--env-file",
    str(HOME / "deploy.env"),
    "-f",
    str(ROOT / "deploy/compose.yaml"),
    "-f",
    str(ROOT / "tests/compose.test.yaml"),
]


def checked(response):
    response.raise_for_status()
    return response.json()


def eventually(check, description, timeout=60, interval=0.2):
    """Poll an observable condition, never repeat the action under test."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except httpx.TransportError as error:
            last_error = str(error)
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {502, 503, 504}:
                raise
            last_error = str(error)
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {description}" + (f": {last_error}" if last_error else ""))


def connect():
    client = httpx.Client(base_url=URL, timeout=40)
    auth = eventually(lambda: checked(client.get("/auth/status")), "gateway startup")
    if auth["claimed"]:
        if not STATE.exists():
            raise RuntimeError(
                "This test deployment was already claimed, but its test credential file is missing."
            )
        credentials = json.loads(STATE.read_text())
    else:
        result = subprocess.run(
            [*COMPOSE, "exec", "-T", "control", "cat", "/data/bootstrap-token"],
            capture_output=True,
            text=True,
            check=True,
        )
        credentials = {"bootstrap": result.stdout.strip(), "password": secrets.token_urlsafe(24)}
        with open(STATE, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            json.dump(credentials, file)
    response = checked(client.post("/auth/login", json={**credentials, "cli": True}))
    client.headers["Authorization"] = "Bearer " + response["token"]
    return client


def ready(client):
    eventually(lambda: checked(client.get("/api/status"))["runtime_ready"], "runtime readiness")
    eventually(lambda: checked(client.get("/api/browser")), "browser readiness")


def fixture_settings(client, **changes):
    settings = checked(client.get("/api/settings"))
    settings.update(provider="custom-fixture", model="fixture", computer_auto=False, browser_auto=True)
    settings.update(changes)
    checked(client.put("/api/settings", json=settings))
    return settings


def wait(client, task_id, status="completed", timeout=60):
    last = {}

    def reached():
        nonlocal last
        last = checked(client.get(f"/api/tasks/{task_id}"))
        if last["status"] == status:
            return last
        if last["status"] in {"failed", "uncertain", "cancelled"}:
            raise AssertionError(json.dumps(last))
        return None

    try:
        return eventually(reached, f"task {task_id} to become {status}", timeout=timeout)
    except AssertionError as exc:
        raise AssertionError(f"{exc}; last task: {json.dumps(last)}") from exc


def install_echo_plugin(client):
    """Create the real MCP fixture used by plugin and persistence journeys."""
    manifest = {
        "schema_version": 1,
        "id": "personal-echo",
        "name": "Personal echo",
        "version": "1.0.0",
        "description": "A local integration fixture",
        "kind": "mcp",
        "license": "MIT",
        "files": ["LICENSE", "server.py"],
        "entrypoint": "server.py",
        "tools": [
            {
                "name": "echo",
                "description": "Echo a short value",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ],
    }
    files = {
        "shinbot-plugin.json": json.dumps(manifest),
        "LICENSE": (ROOT / "LICENSE").read_text(),
        "server.py": 'from mcp.server.fastmcp import FastMCP\nm=FastMCP("Fixture")\n@m.tool()\ndef echo(text: str) -> str:\n    return "plugin said: " + text\nm.run()\n',
    }
    checked(client.post("/api/plugins/stage", json={"files": files}))
    checked(client.post("/api/plugins/personal-echo/1.0.0/activate", json={}))
