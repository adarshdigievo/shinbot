import base64
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import httpx
import pytest

from shinbot.plugins import OFFICIAL, Plugins, validate_package

ROOT = Path(__file__).resolve().parents[1]
GITHUB = ROOT / "marketplaces/official/plugins/github"


def package(path=GITHUB):
    manifest = json.loads((path / "shinbot-plugin.json").read_text())
    return {name: (path / name).read_bytes() for name in ["shinbot-plugin.json", *manifest["files"]]}


@pytest.fixture
def github(monkeypatch):
    module = types.ModuleType("github_plugin")
    exec(compile((GITHUB / "server.py").read_text(), "github/server.py", "exec"), module.__dict__)
    monkeypatch.setenv("SHINBOT_PLUGIN_GITHUB_TOKEN", "private-test-token")
    monkeypatch.setenv("SHINBOT_PLUGIN_GITHUB_REPOSITORIES", "example/project")
    return module


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )


def test_github_reads_are_scoped_bounded_and_paginated(github, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == "Bearer private-test-token"
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "tester", "html_url": "https://github.com/tester"})
        if request.url.path == "/search/issues":
            assert "repo:example/project is:pr state:open bug" == request.url.params["q"]
            assert request.url.params["page"] == "2"
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "items": [
                        {
                            "number": 4,
                            "body": "x" * 13000,
                            "repository_url": "https://api.github.com/repos/example/project",
                            "pull_request": {},
                        },
                        {"number": 8, "repository_url": "https://api.github.com/repos/other/private"},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": 5,
                "content": base64.b64encode(b"hello").decode(),
                "path": "README.md",
                "sha": "abc",
                "html_url": "https://github.com/example/project/blob/main/README.md",
            },
        )

    transport(monkeypatch, handler)
    assert github.connection_status()["repositories"] == ["example/project"]
    result = github.search_items("example/project", "bug", "pr", "open", 2)
    assert len(result["items"]) == 1 and result["items"][0]["body"]["truncated"]
    assert github.read_file("example/project", "README.md", "branch/one")["content"]["text"] == "hello"
    assert requests[-1].url.params["ref"] == "branch/one"
    count = len(requests)
    for call in [
        lambda: github.get_repository("other/private"),
        lambda: github.read_file("example/project", "../secret"),
        lambda: github.search_items("example/project", "bug OR repo:other/private"),
        lambda: github.search_items("example/project", page=0),
    ]:
        with pytest.raises(ValueError):
            call()
    assert len(requests) == count


def test_github_writes_send_exact_reviewed_inputs(github, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            201 if request.method == "POST" else 200,
            json={
                "id": 8,
                "number": 3,
                "html_url": "https://github.com/example/project/issues/3",
                "draft": True,
            },
        )

    transport(monkeypatch, handler)
    github.create_issue("example/project", "An issue", "Exact body\nSecond line")
    assert json.loads(requests[-1].content) == {"title": "An issue", "body": "Exact body\nSecond line"}
    github.comment("example/project", 3, "My comment")
    assert requests[-1].url.path.endswith("/issues/3/comments")
    github.update_issue("example/project", 3, "closed")
    assert requests[-1].method == "PATCH"
    github.create_pull_request("example/project", "Change", "feature", "main", "Review this")
    assert json.loads(requests[-1].content) == {
        "title": "Change",
        "head": "feature",
        "base": "main",
        "body": "Review this",
        "draft": True,
    }
    with pytest.raises(ValueError):
        github.comment("example/project", 3, " ")


@pytest.mark.parametrize("status", [301, 401, 403, 404, 409, 422, 429])
def test_github_errors_are_helpful_and_do_not_leak_credentials(github, monkeypatch, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Retry-After": "10"} if status == 429 else {},
            json={"message": "private-test-token"},
        )

    transport(monkeypatch, handler)
    with pytest.raises(ValueError) as failure:
        github.create_issue("example/project", "Test")
    assert "private-test-token" not in str(failure.value)
    assert "GitHub" in str(failure.value)
    assert len(requests) == 1, "Writes must not retry or forward credentials on redirects"


def test_github_timeout_does_not_repeat_a_write(github, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("secret request details", request=request)

    transport(monkeypatch, handler)
    result = github.create_issue("example/project", "Test")
    assert result.isError and result.meta["shinbot_outcome"] == "uncertain"
    assert "may have completed" in result.content[0].text
    assert len(requests) == 1


def test_plugin_configuration_is_private_persistent_and_scoped(owner, application):
    core = application.state.core
    core.plugins.stage_from_catalog("official", "github", "1.0.0")
    path = "/api/plugins/github/1.0.0/configuration"
    values = {
        "SHINBOT_PLUGIN_GITHUB_TOKEN": "private-test-token",
        "SHINBOT_PLUGIN_GITHUB_REPOSITORIES": "example/project",
    }
    response = owner.put(path, json=values)
    assert response.status_code == 200 and "private-test-token" not in response.text
    assert "private-test-token" not in owner.get("/api/plugins").text
    assert "private-test-token" not in owner.get(path).text
    revised = package()
    manifest = json.loads(revised["shinbot-plugin.json"])
    manifest["version"] = "1.0.1"
    manifest["configuration"][0]["secret"] = False
    revised["shinbot-plugin.json"] = json.dumps(manifest).encode()
    core.plugins.stage(revised, marketplace="official")
    exposed = owner.get("/api/plugins/github/1.0.1/configuration")
    assert "private-test-token" not in exposed.text
    assert exposed.json()["fields"][0]["secret"]
    assert owner.put(path, json={"PATH": "/wrong"}).status_code == 400
    stored = core.plugins.vault.path("plugin:github:official")
    assert b"private-test-token" not in stored.read_bytes()
    assert stored.stat().st_mode & 0o777 == 0o600
    restarted = Plugins(core.db, core.config.data / "plugins")
    assert restarted.configuration("github", "1.0.0", environment=True) == values
    assert owner.put(path, json={"SHINBOT_PLUGIN_GITHUB_REPOSITORIES": "example/other"}).status_code == 200
    assert (
        restarted.configuration("github", "1.0.0", environment=True)["SHINBOT_PLUGIN_GITHUB_TOKEN"]
        == values["SHINBOT_PLUGIN_GITHUB_TOKEN"]
    )
    owner.put(path, json={"SHINBOT_PLUGIN_GITHUB_TOKEN": None})
    with pytest.raises(ValueError, match="GitHub token"):
        restarted.configuration("github", "1.0.0", environment=True)
    owner.headers.pop("Authorization")
    assert owner.get(path).status_code == 401
    assert owner.put(path, json=values).status_code == 401


def test_github_write_requires_approval_even_with_computer_permission(application):
    core = application.state.core
    core.plugins.stage_from_catalog("official", "github", "1.0.0")
    assert core.approval_required("plugin:github:1.0.0:create_issue", {}, {"computer": True})
    assert not core.approval_required("plugin:github:1.0.0:get_issue", {}, {"computer": True})


def test_official_packages_ship_and_bundled_catalog_updates(application):
    core = application.state.core
    catalog = next(m for m in core.plugins.catalog() if m["source"] == OFFICIAL)
    assert {p["id"] for p in catalog["plugins"]} == {
        "telegram",
        "github",
        "kanban",
        "serpapi",
        "projects",
        "calendar",
        "research",
    }
    for entry in catalog["plugins"]:
        staged = core.plugins.stage_from_catalog(catalog["id"], entry["id"], entry["version"])
        assert staged["status"] == "staged"
        source = ROOT / "marketplaces/official" / entry["path"]
        assert validate_package(package(source))[1] == staged["digest"]
    with pytest.raises(ValueError, match="telegram-stop"):
        core.plugins.disable("telegram")
    core.db.execute(
        "UPDATE marketplaces SET catalog='{}',revision='bundled-old' WHERE id=?", (catalog["id"],)
    )
    refreshed = Plugins(core.db, core.config.data / "plugins")
    assert next(m for m in refreshed.catalog() if m["source"] == OFFICIAL)["plugins"] == catalog["plugins"]


def test_real_mcp_worker_validates_github_manifest_and_missing_credentials(tmp_path):
    manifest = json.loads((GITHUB / "shinbot-plugin.json").read_text())
    payload = {"operation": "mcp", "directory": str(GITHUB), "manifest": manifest}
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(data):
        result = subprocess.run(
            [sys.executable, "-I", "-m", "shinbot.services.worker"],
            input=json.dumps(data),
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=environment,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)["result"]

    assert len(run(payload)["tools"]) == 10
    result = run(
        {
            **payload,
            "name": "connection_status",
            "arguments": {},
            "environment": {"SHINBOT_PLUGIN_GITHUB_REPOSITORIES": "example/project"},
        }
    )
    assert result["isError"] and "Set a GitHub token" in json.dumps(result)
    (tmp_path / "uncertain.py").write_text(
        "import runpy\n"
        f"plugin=runpy.run_path({str(GITHUB / 'server.py')!r})\n"
        "def fail(*args, **kwargs): raise plugin['UncertainWrite']('Check GitHub before retrying.')\n"
        "plugin['create_issue'].__wrapped__.__globals__['request']=fail\n"
        "plugin['mcp'].run()\n"
    )
    uncertain = run(
        {
            **payload,
            "directory": str(tmp_path),
            "manifest": {**manifest, "entrypoint": "uncertain.py"},
            "name": "create_issue",
            "arguments": {"repo": "example/project", "title": "Test"},
            "environment": {"SHINBOT_PLUGIN_GITHUB_REPOSITORIES": "example/project"},
        }
    )
    assert uncertain["isError"] and uncertain["_meta"]["shinbot_outcome"] == "uncertain"


async def test_plugin_settings_reach_only_the_declared_process(tmp_path, monkeypatch):
    from shinbot.plugins import PluginManifest
    from shinbot.services import computer

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(computer, "ROOT", workspace)
    monkeypatch.setenv("SHINBOT_EXECUTOR_TOKEN", "control-secret")
    monkeypatch.setenv("OTHER_PLUGIN_SECRET", "unrelated-secret")
    (tmp_path / "server.py").write_text("""import os
from mcp.server.fastmcp import FastMCP
m = FastMCP("Credentials test")
@m.tool()
def check() -> dict:
    return {"configured": os.getenv("SHINBOT_PLUGIN_TEST_TOKEN") == "private-test-token",
            "service_hidden": "SHINBOT_EXECUTOR_TOKEN" not in os.environ,
            "unrelated_hidden": "OTHER_PLUGIN_SECRET" not in os.environ}
if __name__ == "__main__": m.run()
""")
    manifest = PluginManifest.model_validate(
        {
            "id": "credential-test",
            "name": "Test",
            "version": "1.0.0",
            "description": "Credential isolation test",
            "kind": "mcp",
            "license": "MIT",
            "files": ["server.py", "LICENSE"],
            "entrypoint": "server.py",
            "configuration": [{"key": "SHINBOT_PLUGIN_TEST_TOKEN", "label": "Token", "secret": True}],
            "tools": [{"name": "check", "input_schema": {"type": "object", "properties": {}}}],
        }
    )
    result = await computer.mcp_call(
        "credential-test",
        tmp_path,
        manifest,
        "check",
        {},
        {"SHINBOT_PLUGIN_TEST_TOKEN": "private-test-token"},
    )
    assert not result.get("isError"), result
    assert json.loads(result["content"][0]["text"]) == {
        "configured": True,
        "service_hidden": True,
        "unrelated_hidden": True,
    }
    with pytest.raises(ValueError, match="Undeclared"):
        await computer.mcp_call("credential-test", tmp_path, manifest, "check", {}, {"PATH": "/wrong"})


async def test_credentials_are_excluded_from_executor_receipts(tmp_path, monkeypatch):
    from shinbot.services import computer
    from shinbot.services.common import Journal

    monkeypatch.setattr(computer, "DATA", tmp_path)
    monkeypatch.setattr(computer, "ROOT", tmp_path)
    journal = Journal(tmp_path)
    monkeypatch.setattr(computer, "journal", journal)
    directory = tmp_path / "plugins/github/1.0.0"
    directory.mkdir(parents=True)
    files = package()
    _, digest = validate_package(files)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    (directory / ".digest").write_text(digest)
    calls = []

    async def execute(*args):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(computer, "mcp_call", execute)
    payload = {
        "id": "secret-action",
        "epoch": "test",
        "task_id": "secret-task",
        "tool": "plugin:github:1.0.0:get_repository",
        "args": {"repo": "example/project"},
        "environment": {"SHINBOT_PLUGIN_GITHUB_TOKEN": "private-test-token"},
    }
    try:
        assert await computer.execute(dict(payload)) == {"ok": True}
        assert calls[0][5] == payload["environment"]
        assert calls[0][6] == tmp_path
        assert await computer.execute(
            {**payload, "environment": {"SHINBOT_PLUGIN_GITHUB_TOKEN": "rotated-token"}}
        ) == {"ok": True}
        assert len(calls) == 1
        assert "private-test-token" not in str(
            [dict(row) for row in journal.db.execute("SELECT * FROM effects")]
        )

        async def rejected(*args):
            calls.append(args)
            raise computer.MCPRejected("Tool was not sent: private-test-token")

        monkeypatch.setattr(computer, "mcp_call", rejected)
        from fastapi import HTTPException

        for _ in range(2):
            with pytest.raises(HTTPException) as error:
                await computer.execute({**payload, "id": "rejected-action"})
            assert error.value.status_code == 400
            assert "private-test-token" not in error.value.detail
        assert len(calls) == 2
        assert journal.receipt("rejected-action")["status"] == "rejected"

        async def ambiguous(*args):
            return {"isError": True, "_meta": {"shinbot_outcome": "uncertain"}}

        monkeypatch.setattr(computer, "mcp_call", ambiguous)
        with pytest.raises(HTTPException) as error:
            await computer.execute({**payload, "id": "ambiguous-action"})
        assert error.value.status_code == 409
        assert (
            journal.db.execute("SELECT status FROM effects WHERE id='ambiguous-action'").fetchone()[0]
            == "uncertain"
        )
    finally:
        journal.db.close()


def test_other_source_cannot_inherit_marketplace_credentials(application):
    plugins = application.state.core.plugins
    plugins.stage(package(), marketplace="official")
    plugins.configuration(
        "github",
        "1.0.0",
        {
            "SHINBOT_PLUGIN_GITHUB_TOKEN": "owner-secret",
            "SHINBOT_PLUGIN_GITHUB_REPOSITORIES": "example/project",
        },
    )
    malicious = package()
    manifest = json.loads(malicious["shinbot-plugin.json"])
    manifest["version"] = "1.0.1"
    malicious["shinbot-plugin.json"] = json.dumps(manifest).encode()
    with pytest.raises(ValueError, match="belongs to a marketplace"):
        plugins.stage(malicious)
    plugins.stage(malicious, marketplace="community-fixture")
    assert not any(field["configured"] for field in plugins.configuration("github", "1.0.1")["fields"])
    with pytest.raises(ValueError, match="Connection"):
        plugins.configuration("github", "1.0.1", environment=True)
    assert (
        plugins.configuration("github", "1.0.0", environment=True)["SHINBOT_PLUGIN_GITHUB_TOKEN"]
        == "owner-secret"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b"x" * 2000001),
    ],
)
def test_github_ambiguous_write_results_are_marked_uncertain(github, monkeypatch, response):
    transport(monkeypatch, lambda request: response)
    result = github.create_issue("example/project", "Test")
    assert result.isError and result.meta == {"shinbot_outcome": "uncertain"}


async def test_ambiguous_plugin_write_holds_the_task(application, monkeypatch):
    import asyncio

    from shinbot.db import uid

    core = application.state.core
    core.plugins.stage_from_catalog("official", "github", "1.0.0")
    core.plugins.configuration(
        "github",
        "1.0.0",
        {
            "SHINBOT_PLUGIN_GITHUB_TOKEN": "private-test-token",
            "SHINBOT_PLUGIN_GITHUB_REPOSITORIES": "example/project",
        },
    )
    core.plugins.activate("github", "1.0.0")
    task = core.tasks.create("Create an issue")
    payload = {
        "id": uid(),
        "task_id": task["id"],
        "epoch": core.db.setting("epoch"),
        "tool": "plugin:github:1.0.0:create_issue",
        "args": {"repo": "example/project", "title": "Test"},
    }
    calls = []

    async def remote(service, path, body=None, **kwargs):
        if service == "computer":
            calls.append(body)
            response = httpx.Response(409, request=httpx.Request("POST", "http://computer/execute"))
            raise httpx.HTTPStatusError("Uncertain", request=response.request, response=response)
        return {}

    monkeypatch.setattr(core, "remote", remote)
    assert (await core.action(payload))["status"] == "pending"
    core.approvals.decide(payload["id"], True)
    assert (await core.action(payload))["status"] == "executing"
    await asyncio.gather(*core.running_actions.values())
    assert core.tasks.detail(task["id"])["status"] == "uncertain"
    assert (await core.action(payload))["status"] == "blocked"
    assert len(calls) == 1


def test_local_package_cannot_claim_catalog_identity_before_installation(application):
    plugins = application.state.core.plugins
    with pytest.raises(ValueError, match="belongs to a marketplace"):
        plugins.stage(package())
    installed = plugins.stage_from_catalog("official", "github", "1.0.0")
    assert installed["origin"] == OFFICIAL
    assert plugins.configuration("github", "1.0.0")["origin"] == OFFICIAL
