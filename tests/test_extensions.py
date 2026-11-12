import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from shinbot.db import packed, uid
from shinbot.extensions import MCPServer, connection_package, skill_package, skills_command
from shinbot.plugins import PluginManifest
from shinbot.services import computer
from shinbot.services.worker import mcp_operation

ROOT = Path(__file__).resolve().parents[1]
SKILL = "---\nname: helpful-notes\ndescription: Write useful notes with source links.\nlicense: MIT\n---\nRead references/style.md before writing.\n"
FILES = {
    "SKILL.md": SKILL,
    "references/style.md": "Use plain language.",
    "LICENSE": (ROOT / "LICENSE").read_text(),
}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(computer, "ROOT", workspace)
    monkeypatch.setattr(computer, "DATA", tmp_path / "data")
    monkeypatch.setenv(
        "SHINBOT_CHILD_PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"]
    )
    monkeypatch.setenv("SHINBOT_SKILLS_CLI", str(ROOT / "computer/node_modules/skills/bin/cli.mjs"))
    return workspace


def fixture_server(path):
    path.write_text("""import os
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("Fixture")
@mcp.tool()
def echo(text: str = "ok") -> dict:
    return {"text": text, "configured": os.getenv("FIXTURE_TOKEN") == "private-fixture-token", "leaked": "SHINBOT_SERVICE_TOKEN" in os.environ}
mcp.run()
""")
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-B", str(path)],
        "env": {"FIXTURE_TOKEN": "private-fixture-token"},
    }


async def test_stdio_connection_discovery_credentials_and_schema_pin(runner, monkeypatch):
    monkeypatch.setenv("SHINBOT_SERVICE_TOKEN", "must-not-leak")
    server = fixture_server(runner / "fixture.py")
    discovered = await computer.discover({"server": server})
    assert [t["name"] for t in discovered["tools"]] == ["echo"]
    files, settings = connection_package("fixture-mcp", "Fixture", "1.0.0", server, discovered["tools"])
    assert b"private-fixture-token" not in files["shinbot-plugin.json"]
    manifest = PluginManifest.model_validate_json(files["shinbot-plugin.json"])
    result = await computer.mcp_call("fixture", runner, manifest, "echo", {"text": "hello"}, settings)
    data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert data == {"text": "hello", "configured": True, "leaked": False}
    manifest.tools[0]["input_schema"]["properties"]["text"]["type"] = "integer"
    with pytest.raises(computer.MCPRejected, match="MCP connection failed"):
        await computer.mcp_call("fixture", runner, manifest, "echo", {}, settings)


async def test_mcp_setup_rejection_reaches_owner_and_agent_without_uncertainty(
    runner, application, monkeypatch
):
    from test_actions import action as make_action

    core = application.state.core
    monkeypatch.setenv("SHINBOT_EXECUTOR_TOKEN", core.config.computer_token)
    script = runner / "broken.py"
    script.write_text("raise SystemExit(1)\n")
    server = {"transport": "stdio", "command": sys.executable, "args": [str(script)]}
    files, _ = connection_package(
        "broken-mcp", "Broken MCP", "1.0.0", server, [{"name": "echo", "input_schema": {"type": "object"}}]
    )
    core.plugins.stage(files)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=computer.app, raise_app_exceptions=False),
        base_url="http://computer",
        headers={"Authorization": "Bearer " + core.config.computer_token},
    ) as client:
        discovery = await client.post("/plugins/discover", json={"server": server})
        assert discovery.status_code == 400
        assert "MCP connection failed" in discovery.json()["detail"]

        async def remote(service, path, body=None, **kwargs):
            response = await client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(core, "remote", remote)
        task = core.tasks.create("Activate my MCP server")
        payload = make_action(
            core, task, "plugins", {"operation": "activate", "id": "broken-mcp", "version": "1.0.0"}
        )
        assert (await core.action(payload))["status"] == "pending"
        core.approvals.decide(payload["id"], True)
        await core.action(payload)
        await asyncio.gather(*core.running_actions.values())
        result = await core.action(payload)
        assert result["status"] == "failed" and "MCP connection failed" in result["error"]
        assert core.tasks.detail(task["id"])["status"] != "uncertain"
        assert core.db.one("SELECT status FROM plugins WHERE id='broken-mcp'")["status"] == "staged"
        assert (await core.action(payload))["status"] == "failed"


@pytest.mark.parametrize("transport,path", [("http", "/mcp"), ("sse", "/sse")])
async def test_remote_mcp_transports_use_real_protocol(tmp_path, transport, path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    source = tmp_path / "remote.py"
    source.write_text(f"""from mcp.server.fastmcp import FastMCP
mcp = FastMCP("Remote fixture", host="127.0.0.1", port={port}, stateless_http=True, json_response=True)
@mcp.tool()
def echo(text: str) -> str:
    return text
mcp.run(transport={"streamable-http" if transport == "http" else "sse"!r})
""")
    process = subprocess.Popen(
        [sys.executable, str(source)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)
        server = MCPServer(transport=transport, url=f"http://127.0.0.1:{port}{path}").model_dump()
        listed = await mcp_operation({"manifest": {"server": server}, "discover": True})
        result = await mcp_operation(
            {
                "manifest": {"server": server, "tools": listed["tools"]},
                "name": "echo",
                "arguments": {"text": "real transport"},
            }
        )
        assert result["content"][0]["text"] == "real transport"
    finally:
        process.terminate()
        process.wait(timeout=10)


async def test_published_skills_cli_installs_files_without_running_scripts(runner):
    source = runner / "local-skill"
    source.mkdir()
    for name, text in FILES.items():
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    (source / "danger.py").write_text("raise RuntimeError('Skill scripts must not run at install')")
    result = await computer.skills_install({"command": "npx skills add ./local-skill --skill helpful-notes"})
    assert len(result["skills"]) == 1
    copied = result["skills"][0]
    assert copied["SKILL.md"] == SKILL
    assert copied["references/style.md"] == FILES["references/style.md"]
    assert "danger.py" in copied
    assert not list(runner.glob(".skills-install-*"))
    files = skill_package({name: text.encode() for name, text in copied.items()}, "MIT", "local fixture")
    assert files["SKILL.md"].decode() == SKILL


@pytest.mark.parametrize(
    "command",
    [
        "npx other add x",
        "npx skills@latest add x",
        "npx skills add x --global",
        "npx skills add x ; touch /tmp/no",
        "npx skills add --help",
    ],
)
def test_skills_command_rejects_unsupported_execution(command):
    with pytest.raises(ValueError):
        skills_command(command)


def task_row(core, bot):
    return core.db.one("SELECT * FROM tasks WHERE id=?", (core.tasks.create("Test", bot_id=bot)["id"],))


def action(core, task, operation, **args):
    return {
        "id": uid(),
        "epoch": core.db.setting("epoch"),
        "task_id": task["id"],
        "tool": "plugins",
        "args": packed({"operation": operation, **args}),
    }


async def test_skill_bot_scope_pinning_revocation_and_references(application, owner):
    core = application.state.core
    a = core.bots.save({"name": "A"})["id"]
    b = core.bots.save({"name": "B"})["id"]
    previous = task_row(core, a)
    response = owner.post(
        "/api/plugins/skills/import",
        json={"files": FILES, "license": "MIT", "access": {"mode": "bots", "bots": [a]}},
    )
    assert response.status_code == 200
    plugin = response.json()["installed"][0]
    core.plugins.activate(plugin["id"], plugin["version"])
    first, other = task_row(core, a), task_row(core, b)
    ref = f"plugin:{plugin['id']}:{plugin['version']}"
    assert ref in json.loads(first["extensions"]) and ref not in json.loads(other["extensions"])
    assert "helpful-notes" in core.instructions(first) and "helpful-notes" not in core.instructions(other)
    call = action(
        core, first, "read_skill", id=plugin["id"], version=plugin["version"], path="references/style.md"
    )
    assert (await core.perform(call, first))["text"] == "Use plain language."
    for row in (previous, other):
        with pytest.raises(ValueError, match="not selected"):
            await core.perform(call, row)
    assert (
        owner.put(f"/api/plugins/{plugin['id']}/access", json={"mode": "shared", "bots": []}).status_code
        == 200
    )
    assert ref in json.loads(task_row(core, b)["extensions"])
    owner.put(f"/api/plugins/{plugin['id']}/access", json={"mode": "bots", "bots": [b]})
    with pytest.raises(ValueError, match="not selected"):
        await core.perform(call, first)
    assert (
        owner.put(
            f"/api/plugins/{plugin['id']}/access", json={"mode": "bots", "bots": ["missing"]}
        ).status_code
        == 400
    )


async def test_mcp_owner_setup_and_agent_scope_revocation(application, owner, runner, monkeypatch):
    core = application.state.core
    a = core.bots.save({"name": "A"})["id"]
    b = core.bots.save({"name": "B"})["id"]

    async def remote(service, path, body=None, **kwargs):
        if path == "/plugins/discover":
            return await computer.discover(body)
        if path == "/plugins/stage":
            return await computer.stage(body)
        return {}

    monkeypatch.setattr(core, "remote", remote)
    response = owner.post(
        "/api/plugins/mcp/connect",
        json={
            "id": "fixture",
            "name": "Fixture",
            "server": fixture_server(runner / "fixture.py"),
            "access": {"mode": "bots", "bots": [a]},
        },
    )
    assert response.status_code == 200, response.text
    assert "private-fixture-token" not in response.text
    assert owner.post("/api/plugins/fixture/1.0.0/activate", json={}).status_code == 200
    first, other = task_row(core, a), task_row(core, b)
    payload = {
        "id": uid(),
        "epoch": core.db.setting("epoch"),
        "task_id": first["id"],
        "tool": "plugin:fixture:1.0.0:echo",
        "args": {"text": "hello"},
    }
    assert (await core.action(payload))["status"] == "pending"
    core.plugins.set_access("fixture", {"mode": "bots", "bots": [b]})
    assert (await core.action(payload))["status"] == "blocked"
    payload.update(id=uid(), task_id=other["id"])
    with pytest.raises(ValueError, match="not selected"):
        await core.action(payload)


def test_template_library_roundtrip_and_permissions(application, owner):
    core = application.state.core
    template = core.bots.export("default")
    template["id"] = "saved-helper"
    assert owner.post("/api/templates/save", json=template).status_code == 200
    local = next(m for m in owner.get("/api/templates").json() if m["id"] == "local")
    assert local["templates"] == [template]
    assert "token" not in packed(template).lower()
    assert core.approval_required("bots", {"operation": "save_template"}, {"extensions": True})
    for operation in ("connect_mcp", "install_skills", "set_access"):
        assert core.approval_required(
            "plugins", {"operation": operation}, {"extensions": True, "computer": True}
        )
    owner.headers.pop("Authorization")
    assert owner.post("/api/plugins/mcp/connect", json={}).status_code == 401


async def test_agent_cannot_put_mcp_credentials_in_action_journal(application):
    core = application.state.core
    task = task_row(core, "default")
    payload = {
        "id": uid(),
        "task_id": task["id"],
        "epoch": core.db.setting("epoch"),
        "tool": "plugins",
        "args": {"operation": "connect_mcp", "server": {"headers": {"Authorization": "private-token"}}},
    }
    with pytest.raises(ValueError, match="Plugins settings"):
        await core.action(payload)
    assert core.db.one("SELECT * FROM actions WHERE id=?", (payload["id"],)) is None


async def test_agent_extension_management_respects_scope_and_mandatory_mcp_approval(application):
    core = application.state.core
    a, b = core.bots.save({"name": "A"})["id"], core.bots.save({"name": "B"})["id"]
    task = task_row(core, a)
    files, _ = connection_package(
        "b-only",
        "B tools",
        "1.0.0",
        {"transport": "http", "url": "https://example.com/mcp"},
        [{"name": "delete", "input_schema": {"type": "object"}}],
    )
    core.plugins.stage(files)
    core.plugins.set_access("b-only", {"mode": "bots", "bots": [b]})
    core.plugins.activate("b-only", "1.0.0")
    revised = json.loads(files["shinbot-plugin.json"])
    revised["version"] = "1.0.1"
    files["shinbot-plugin.json"] = packed(revised).encode()
    with pytest.raises(ValueError, match="other bots"):
        await core.perform(action(core, task, "stage", files={k: v.decode() for k, v in files.items()}), task)
    with pytest.raises(ValueError, match="other bots"):
        await core.perform(action(core, task, "activate", id="b-only", version="1.0.0"), task)
    assert core.approval_required("plugin:b-only:1.0.0:delete", {}, {"computer": True})
    assert core.approval_required(
        "plugins",
        {"operation": "activate", "id": "b-only", "version": "1.0.0"},
        {"computer": True, "extensions": True},
    )
    await core.perform(
        action(core, task, "install", id="github", version="1.0.0", marketplace="official"), task
    )
    assert core.plugins.access("github") == {"mode": "bots", "bots": [a]}


def test_standard_yaml_frontmatter_and_full_length_skill_names():
    text = (
        "---\nname: "
        + "a" * 64
        + "\ndescription: >-\n  Folded line one.\n  Folded line two.\nmetadata:\n  author: someone\n---\nInstructions."
    )
    files = skill_package({"SKILL.md": text.encode(), "LICENSE": (ROOT / "LICENSE").read_bytes()}, "MIT")
    manifest = json.loads(files["shinbot-plugin.json"])
    assert manifest["description"] == "Folded line one. Folded line two."
    assert len(manifest["id"]) <= 64 and manifest["name"] == "a" * 64
    text = '---\nname: quoted\ndescription: "Uses: colons" # a comment\n---\nInstructions.'
    manifest = json.loads(
        skill_package({"SKILL.md": text.encode(), "LICENSE": (ROOT / "LICENSE").read_bytes()}, "MIT")[
            "shinbot-plugin.json"
        ]
    )
    assert manifest["description"] == "Uses: colons"


async def test_capability_discovery_survives_browser_outage(application, monkeypatch):
    import httpx

    core = application.state.core
    task = task_row(core, "default")

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("private network diagnostics")

    monkeypatch.setattr(core, "remote", unavailable)
    payload = action(core, task, "search")
    payload.update(tool="status", args="{}")
    result = await core.perform(payload, task)
    assert result["browser"]["available"] is False
    assert "MCP connections" in result["capabilities"]
    assert "private network diagnostics" not in packed(result)


async def test_agent_reinstallation_preserves_existing_shared_access(application, monkeypatch):
    core = application.state.core
    bot = core.bots.save({"name": "A"})["id"]
    task = task_row(core, bot)
    server = {"transport": "http", "url": "https://example.com/mcp"}
    tools = [{"name": "search", "input_schema": {"type": "object"}}]
    files, _ = connection_package("shared-tools", "Shared tools", "1.0.0", server, tools)
    core.plugins.stage(files)
    core.import_skills([FILES], "MIT", "./local-skills", {"mode": "shared", "bots": []})

    async def remote(service, path, body=None, **kwargs):
        return {"tools": tools} if path == "/plugins/discover" else {"skills": [FILES], "source": "Fixture"}

    monkeypatch.setattr(core, "remote", remote)
    await core.perform(
        action(
            core, task, "connect_mcp", id="shared-tools", name="Shared tools", version="1.0.1", server=server
        ),
        task,
    )
    await core.perform(
        action(core, task, "install_skills", command="npx skills add ./local-skills", license="MIT"), task
    )
    assert core.plugins.access("shared-tools") == {"mode": "shared", "bots": []}
    assert core.plugins.access("skill-helpful-notes") == {"mode": "shared", "bots": []}
    with pytest.raises(ValueError, match="Stage the extension"):
        core.approval_required(
            "plugins",
            {"operation": "activate", "id": "missing", "version": "1.0.0"},
            {"computer": True, "extensions": True},
        )
