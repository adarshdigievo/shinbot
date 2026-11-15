import fcntl
import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from shinbot import security
from shinbot.app import create_app
from shinbot.config import Config


@pytest.fixture(scope="session", autouse=True)
def scan_each_artifact_once(tmp_path_factory):
    """Run the real scanner once per distinct artifact in a test session.

    Each scan starts a separate analyzer process, and every test has a new
    database, so the bundled plugins and templates were being scanned again in
    test after test. Verdicts depend only on the scanner version, the policy and
    the artifact's bytes, so they are kept in a directory shared by all workers.
    Scanner errors are never kept, and a test that changes the scan timeout
    always reaches the real scanner. A per-artifact lock makes workers that
    miss at the same moment wait for one scan instead of each starting their own.
    """
    base = tmp_path_factory.getbasetemp()
    cache = (base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base) / "scan-verdicts"
    cache.mkdir(exist_ok=True)
    run, timeout = security.Security._run, security.SCAN_TIMEOUT

    def cached(self, files):
        if security.SCAN_TIMEOUT != timeout:
            return run(self, files)
        key = f"{security.SCANNER_VERSION}:{security.POLICY}:{security.content_digest(files)}"
        path = cache / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        if path.exists():
            return json.loads(path.read_text())
        with path.with_suffix(".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                return json.loads(path.read_text())
            result = run(self, files)
            if result["status"] in {"passed", "blocked"}:
                partial = path.with_suffix(f".{os.getpid()}.tmp")
                partial.write_text(json.dumps(result))
                partial.replace(path)
            return result

    security.Security._run = cached
    yield
    security.Security._run = run


@pytest.fixture(autouse=True)
def offline_names(monkeypatch):
    """Browser policy resolves host names; tests answer with a public address instead of the network."""
    from shinbot import browser_policy

    async def resolve(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(browser_policy, "resolve", resolve)
    monkeypatch.setattr(browser_policy, "resolved", {})


@pytest.fixture
def application(tmp_path):
    application = create_app(
        Config(
            data=tmp_path,
            origin="http://testserver",
            coordinate=False,
            service_token="r" * 40,
            computer_token="c" * 40,
            browser_token="b" * 40,
        )
    )

    core = application.state.core
    core.db.set(
        "preferences", {**core.db.setting("preferences"), "provider": "openai", "model": "test-model"}
    )
    return application


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


@pytest.fixture
def owner(client, application):
    result = client.post(
        "/auth/login",
        json={
            "bootstrap": application.state.core.auth.bootstrap,
            "password": "test-password-with-length",
            "cli": True,
        },
    )
    assert result.status_code == 200, result.text
    client.headers["Authorization"] = "Bearer " + result.json()["token"]
    return client
