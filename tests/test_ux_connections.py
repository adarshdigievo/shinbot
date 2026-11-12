import asyncio
import shlex
import time
from types import SimpleNamespace

import httpx
import pytest
from typer import BadParameter
from typer.testing import CliRunner

from shinbot import cli, connect_cli, integrations
from shinbot.jobs import schedule_preview
from shinbot.provider_login import loopback_callback


@pytest.fixture
def github_api(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-github-secret"
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "fixture-owner"})
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[
                {"full_name": "fixture-owner/project", "description": "Example project", "private": True},
            ], headers={"link": '<https://api.github.com/user/repos?page=2>; rel="next"'})
        return httpx.Response(200, json=[{"login": "example-org"}])

    monkeypatch.setattr(integrations, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return calls


def test_github_pairing_requires_owner_and_single_use_expiring_code(owner, application, github_api):
    core = application.state.core
    pair = owner.post("/api/github/pair").json()
    command = shlex.split(pair["command"])
    code = command[command.index("--code") + 1]
    owner.headers.pop("Authorization")
    owner.cookies.clear()
    assert owner.post("/api/github/pair").status_code == 401
    assert owner.post("/auth/github/pair", json={"token": "fixture-github-secret"}).status_code == 400
    response = owner.post("/auth/github/pair", headers={"Authorization": "Bearer " + code}, json={"token": "fixture-github-secret"})
    assert response.status_code == 200
    assert response.json()["login"] == "fixture-owner"
    assert "fixture-github-secret" not in response.text
    assert "fixture-github-secret" not in core.github.vault.path("github-account").read_text()
    assert core.github.progress(pair["id"])["status"] == "complete"
    assert "digest" not in core.github.progress(pair["id"])
    assert owner.post("/auth/github/pair", headers={"Authorization": "Bearer " + code}, json={"token": "fixture-github-secret"}).status_code == 400
    other = core.github.start()
    code = shlex.split(other["command"])[-1]
    core.github.pair["expires"] = time.time() - 1
    assert owner.post("/auth/github/pair", headers={"Authorization": "Bearer " + code}, json={"token": "fixture-github-secret"}).status_code == 400


def test_github_discovery_is_paginated_and_account_selection_is_bound(owner, application, github_api, monkeypatch):
    core = application.state.core
    account = owner.post("/api/github/account", json={"token": "fixture-github-secret"}).json()
    result = owner.get("/api/github/discover?page=2").json()
    assert result["has_more"] and result["items"][0]["private"]
    assert github_api[-1].url.params["page"] == "2"
    assert owner.get("/api/github/discover?page=0").status_code == 400
    assert owner.get("/api/github/discover?kind=secrets").status_code == 400
    assert "fixture-github-secret" not in owner.get("/api/github/account").text
    monkeypatch.setattr(core.inbox, "require", lambda: None)
    payload = {"target": "inbox", "revision": "stale", "repositories": ["fixture-owner/project"], "organizations": []}
    assert owner.post("/api/github/select", json=payload).status_code == 400
    payload["revision"] = account["revision"]
    assert owner.post("/api/github/select", json=payload).status_code == 200
    assert core.inbox.scopes()["repositories"] == ["fixture-owner/project"]
    assert core.plugins.vault.read("github-inbox")["token"] == "fixture-github-secret"


def test_github_cli_reuses_account_without_printing_or_passing_token_as_argument(monkeypatch):
    monkeypatch.setattr(connect_cli.shutil, "which", lambda name: "/usr/local/bin/gh")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert "fixture-github-secret" not in args
        assert kwargs["capture_output"]
        return SimpleNamespace(returncode=0, stdout="fixture-github-secret\n")

    def post(url, **kwargs):
        assert url == "http://localhost:8765/auth/github/pair"
        assert kwargs["json"] == {"token": "fixture-github-secret"}
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, json={"login": "fixture-owner"})

    monkeypatch.setattr(connect_cli.subprocess, "run", run)
    monkeypatch.setattr(connect_cli.httpx, "post", post)
    result = CliRunner().invoke(cli.app, ["github-connect", "--url", "http://localhost:8765", "--code", "test-pair"])
    assert result.exit_code == 0, result.output
    assert "fixture-github-secret" not in result.output
    assert len(calls) == 1 and "token" in calls[0]
    assert "fixture-owner" in result.output


@pytest.mark.parametrize("url", ["http://remote.example", "https://user:pass@example.com", "https://example.com/?token=x", "file:///tmp/test"])
def test_cli_rejects_unsafe_credential_destinations(url):
    with pytest.raises(BadParameter):
        connect_cli.server_url(url)


def test_local_chatgpt_uses_exposed_gateway_and_redacts_callback(owner, application, monkeypatch):
    core = application.state.core
    core.config.origin = "http://localhost:19876"
    requests = []

    async def runtime(method, path, **kwargs):
        requests.append((method, path, kwargs))
        if method == "GET":
            return {"status": "idle"}
        return {"id": "fixture-login", "status": "pending"}

    monkeypatch.setattr(core.coordinator, "request", runtime)
    response = owner.post("/api/login", json={"provider": "openai", "redirect_uri": "https://evil.example"}, headers={"Host": "localhost:19876"})
    assert response.status_code == 200
    assert requests[-1][2]["json"]["redirect_uri"] == "http://127.0.0.1:19876/auth/callback"
    response = owner.get("/auth/callback?state=test&code=private-code", headers={"Host": "127.0.0.1:19876"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "http://localhost:19876/auth/complete?result=received&id=fixture-login"
    assert "private-code" not in response.text
    assert requests[-1][2]["json"]["url"] == "http://127.0.0.1:19876/auth/callback?state=test&code=private-code"


async def test_remote_chatgpt_requires_scoped_helper_and_preserves_login_identity(application, monkeypatch):
    core = application.state.core
    core.config.origin = "https://shinbot.example"
    state = {"status": "idle"}

    async def runtime(method, path, **kwargs):
        if method == "GET":
            return dict(state)
        if path.endswith("/respond"):
            state["status"] = "cancelled"
            return {"ok": True}
        state.update(id="runtime-id", provider="openai", status="pending")
        return dict(state)

    monkeypatch.setattr(core.coordinator, "request", runtime)
    result = await core.provider_login.start("openai")
    code = shlex.split(result["command"])[-1]
    with pytest.raises(ValueError):
        await core.provider_login.bridge_start("wrong", "http://127.0.0.1:4567/auth/callback")
    await core.provider_login.bridge_start(code, "http://127.0.0.1:4567/auth/callback")
    assert (await core.provider_login.progress())["id"] == result["id"]
    with pytest.raises(ValueError):
        await core.provider_login.bridge_start(code, "http://127.0.0.1:4567/auth/callback")
    await core.provider_login.respond({"id": result["id"], "cancel": True})
    assert (await core.provider_login.progress())["status"] == "cancelled"
    with pytest.raises(ValueError):
        core.provider_login.require_bridge(code)


@pytest.mark.parametrize("url", ["https://host/auth/callback", "http://localhost:1234/auth/callback", "http://127.0.0.1:1234/other"])
def test_chatgpt_redirect_contract(url):
    with pytest.raises(ValueError):
        loopback_callback(url)


def test_schedule_preview_resolves_local_time_and_rejects_dst_gap(monkeypatch):
    monkeypatch.setattr("shinbot.jobs.time.time", lambda: 1700000000)
    result = schedule_preview({"kind": "once", "local_time": "2027-02-01T09:00", "timezone": "Asia/Kolkata"})
    assert result["spec"] == "2027-02-01T09:00:00+05:30"
    with pytest.raises(ValueError, match="skipped"):
        schedule_preview({"kind": "once", "local_time": "2027-03-14T02:30", "timezone": "America/New_York"})
    result = schedule_preview({"kind": "once", "local_time": "2027-11-07T01:30", "timezone": "America/New_York"})
    assert "first occurrence" in result["note"] and result["spec"].endswith("-04:00")


async def test_cancelled_github_request_cannot_save_after_validation(application, monkeypatch):
    github = application.state.core.github
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"login": "fixture-owner"}, {}

    monkeypatch.setattr(integrations, "request_json", request)
    pair = github.start()
    pending = asyncio.create_task(github.complete(shlex.split(pair["command"])[-1], "fixture-secret"))
    await entered.wait()
    github.cancel(pair["id"])
    release.set()
    with pytest.raises(ValueError, match="cancelled or replaced"):
        await pending
    assert github.account()["connected"] is False
    assert github.progress(pair["id"])["status"] == "cancelled"


async def test_cancel_during_helper_start_cancels_the_created_runtime_login(application, monkeypatch):
    login = application.state.core.provider_login
    application.state.core.config.origin = "https://shinbot.example"
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def runtime(method, path="", payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"status": "idle"}
        if path == "":
            entered.set()
            await release.wait()
            return {"id": "created-runtime-login", "status": "pending"}
        return {"ok": True}

    monkeypatch.setattr(login, "runtime", runtime)
    bridge = await login.start("openai")
    starting = asyncio.create_task(login.bridge_start(shlex.split(bridge["command"])[-1], "http://127.0.0.1:8765/auth/callback"))
    await entered.wait()
    cancelling = asyncio.create_task(login.respond({"id": bridge["id"], "cancel": True}))
    release.set()
    await asyncio.gather(starting, cancelling)
    assert calls[-1] == ("POST", "/respond", {"id": "created-runtime-login", "cancel": True})
    assert login.bridge["status"] == "cancelled"
