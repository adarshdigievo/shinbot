"""Unprivileged, short-lived Computer operations. No service credentials are inherited."""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path


class FileRejected(ValueError):
    """The file operation stopped before changing file contents."""


def open_parent(root, name, create=False):
    if not isinstance(name, str) or name.startswith("/") or "\x00" in name:
        raise ValueError("Use a relative workspace path.")
    parts = name.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise ValueError("Path traversal is not allowed.")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def validate_file_request(tool, args):
    name = args.get("path", ".")
    if (
        not isinstance(name, str)
        or "\x00" in name
        or name.startswith("/")
        or (
            not (tool == "list_files" and name == ".")
            and any(part in {"", ".", ".."} for part in name.split("/"))
        )
    ):
        raise ValueError(
            "Use a relative workspace path such as plugins/example/server.py, without /workspace or parent traversal."
        )
    if tool == "write_file":
        if not isinstance(args.get("content"), str):
            raise ValueError("File content must be UTF-8 text.")
        if len(args["content"].encode()) > 200000:
            raise ValueError("File exceeds 200 KB.")


def file_operation(root, tool, args):
    descriptor = None
    try:
        validate_file_request(tool, args)
        name = args.get("path", ".")
        if tool == "list_files" and name == ".":
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        else:
            parent, leaf = open_parent(root, name, create=tool == "write_file")
            try:
                flags = os.O_NOFOLLOW | os.O_NONBLOCK
                flags |= os.O_WRONLY | os.O_CREAT if tool == "write_file" else os.O_RDONLY
                if tool == "list_files":
                    flags |= os.O_DIRECTORY
                descriptor = os.open(leaf, flags, mode=0o600, dir_fd=parent)
            finally:
                os.close(parent)
        import stat

        mode = os.fstat(descriptor).st_mode
        if tool != "list_files" and not stat.S_ISREG(mode):
            raise ValueError("Only regular workspace files are supported.")
    except (OSError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise FileRejected(
            f"File contents were not changed: {exc}. Check the path and try a new action."
        ) from exc
    try:
        if tool == "list_files":
            return {
                "files": [
                    {
                        "name": name,
                        "directory": stat.S_ISDIR(
                            os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
                        ),
                    }
                    for name in sorted(os.listdir(descriptor))[:200]
                ]
            }
        if tool == "read_file":
            data = os.read(descriptor, 200001)
            if len(data) > 200000:
                raise ValueError("File exceeds 200 KB; use a command to read a section.")
            return {"text": data.decode("utf8")}
        if tool == "attach_file":
            from shinbot.attachments import MAX_FILE_BYTES, file_name

            display_name = file_name(args.get("name") or Path(name).name)
            with os.fdopen(os.dup(descriptor), "rb") as source:
                data = source.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise FileRejected("Attachments can be up to 10 MB. Split or compress this file first.")
            return {"data": base64.b64encode(data).decode(), "name": display_name}
        data = args["content"].encode()
        os.ftruncate(descriptor, 0)
        while data:
            count = os.write(descriptor, data)
            data = data[count:]
        os.fsync(descriptor)
        return {"path": name, "bytes": os.fstat(descriptor).st_size}
    finally:
        os.close(descriptor)


def comparable_schema(value):
    if isinstance(value, list):
        return [comparable_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            {name: comparable_schema(schema) for name, schema in item.items()}
            if key in {"properties", "$defs", "patternProperties"}
            else comparable_schema(item)
        )
        for key, item in value.items()
        if key not in {"title", "description", "$schema"}
    }


def plugin_package(root, directory):
    from shinbot.plugins import PluginManifest, validate_package

    validate_file_request("read_file", {"path": directory})
    manifest_text = file_operation(root, "read_file", {"path": directory + "/shinbot-plugin.json"})["text"]
    manifest = PluginManifest.model_validate_json(manifest_text)
    if len(manifest.files) > 100:
        raise ValueError("A plugin may contain at most 101 files including its manifest.")
    files = {"shinbot-plugin.json": manifest_text}
    size = len(manifest_text.encode())
    for name in manifest.files:
        text = file_operation(root, "read_file", {"path": directory + "/" + name})["text"]
        size += len(text.encode())
        if size > 2_000_000:
            raise ValueError("Plugin exceeds the 2 MB limit.")
        files[name] = text
    validate_package({name: text.encode() for name, text in files.items()})
    return {"files": files}


async def mcp_operation(payload):
    from contextlib import AsyncExitStack
    from datetime import timedelta

    import httpx
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    manifest = payload["manifest"]
    server = manifest.get("server")
    if server:
        from shinbot.extensions import MCPServer

        server = MCPServer.model_validate(server).model_dump()
    environment = payload.get("environment", {})
    async with AsyncExitStack() as stack:
        if server and server["transport"] != "stdio":
            headers = {key: environment.get(value, "") for key, value in server["headers"].items()}

            def http_client_factory(headers=None, timeout=None, auth=None):
                return httpx.AsyncClient(
                    headers=headers, timeout=timeout or 30, auth=auth, follow_redirects=False, trust_env=False
                )

            if server["transport"] == "http":
                streams = await stack.enter_async_context(
                    streamablehttp_client(
                        server["url"], headers=headers, timeout=30, httpx_client_factory=http_client_factory
                    )
                )
            else:
                streams = await stack.enter_async_context(
                    sse_client(
                        server["url"], headers=headers, timeout=30, httpx_client_factory=http_client_factory
                    )
                )
            read, write = streams[:2]
        else:
            params = StdioServerParameters(
                command=server["command"] if server else sys.executable,
                args=server["args"]
                if server
                else ["-B", str(Path(payload["directory"]) / manifest["entrypoint"])],
                env={
                    **os.environ,
                    **(
                        {key: environment.get(value, "") for key, value in server["env"].items()}
                        if server
                        else environment
                    ),
                },
                cwd=os.getcwd(),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=45))
        )
        await session.initialize()
        actual, cursor = [], None
        for _ in range(10):
            page = await session.list_tools(cursor=cursor)
            actual.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        if cursor or len(actual) > 100:
            raise ValueError(
                "This server exposes more than 100 tools. Configure a smaller tool set on the server."
            )
        if payload.get("discover"):
            return {
                "tools": [
                    {
                        "name": t.name,
                        "description": (t.description or t.name)[:1000],
                        "input_schema": t.inputSchema,
                        "requires_confirmation": True,
                    }
                    for t in actual
                ]
            }
        declared = {t["name"]: t for t in manifest["tools"]}
        if {t.name for t in actual} != set(declared):
            raise ValueError("MCP tools changed. Reconnect the server in Plugins to review its new tools.")
        for tool in actual:
            if comparable_schema(tool.inputSchema) != comparable_schema(declared[tool.name]["input_schema"]):
                raise ValueError("MCP tool schema changed. Reconnect the server in Plugins.")
        if payload.get("name") is None:
            return {"validated": True, "tools": sorted(declared)}
        if payload["name"] not in declared:
            raise ValueError("Undeclared MCP tool.")
        payload["_mcp_dispatched"] = True
        try:
            return (await session.call_tool(payload["name"], payload.get("arguments", {}))).model_dump(
                mode="json", exclude_none=True, by_alias=True
            )
        except Exception:
            return {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": "The MCP server did not confirm the result. Inspect the destination before trying again.",
                    }
                ],
                "_meta": {"shinbot_outcome": "uncertain"},
            }


async def install_skills(payload):
    import subprocess
    import tempfile

    from shinbot.extensions import skills_command

    source, names = skills_command(payload["command"])
    if source.startswith("file:") or "ext::" in source:
        raise ValueError("Use a public repository or a workspace directory.")
    if source.startswith(("./", "../", "/")):
        candidate = (Path.cwd() / source).resolve()
        if not candidate.is_relative_to(Path.cwd()):
            raise ValueError("Local skills must be inside the Computer workspace.")
        source = str(candidate)
    cli = os.environ.get("SHINBOT_SKILLS_CLI", "/opt/skills/node_modules/skills/bin/cli.mjs")
    if not Path(cli).is_file():
        raise ValueError("The Skills CLI is missing. Rebuild the Computer image with shinbot start --build.")
    with tempfile.TemporaryDirectory(prefix=".skills-install-", dir=Path.cwd()) as directory:
        result = subprocess.run(
            ["node", cli, "add", source, "--agent", "codex", "--copy", "--yes", "--skill", *(names or ["*"])],
            cwd=directory,
            env={**os.environ, "DISABLE_TELEMETRY": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        if result.returncode:
            raise ValueError(
                "Skills installation failed. Check the source and skill name, and use a public repository or a workspace directory."
            )
        roots = list((Path(directory) / ".agents" / "skills").glob("*/SKILL.md"))
        if not roots or len(roots) > 20:
            raise ValueError("Select between 1 and 20 skills using --skill NAME.")
        packages = []
        total = 0
        for entry in roots:
            files = {}
            for file in entry.parent.rglob("*"):
                if file.is_symlink():
                    raise ValueError("Skill files must not be symbolic links.")
                if file.is_file():
                    total += file.stat().st_size
                    if total > 350000 or len(files) >= 90:
                        raise ValueError(
                            "Select fewer skills. Import is limited to 350 KB and 90 files per skill."
                        )
                    try:
                        files[file.relative_to(entry.parent).as_posix()] = file.read_text()
                    except UnicodeDecodeError:
                        raise ValueError(
                            "This skill contains binary files. Import a text-only skill package; binary assets are not supported yet."
                        ) from None
            packages.append(files)
        return {"skills": packages, "source": payload["command"]}


if __name__ == "__main__":
    try:
        from .plugin_hooks import run as plugin_hook
        from .projects import operate as project_operation

        payload = json.load(sys.stdin)
        result = (
            plugin_hook(payload)
            if payload["operation"] == "plugin_hook"
            else asyncio.run(mcp_operation(payload))
            if payload["operation"] == "mcp"
            else asyncio.run(install_skills(payload))
            if payload["operation"] == "skills_install"
            else project_operation(Path.cwd(), payload["args"])
            if payload["operation"] == "projects"
            else plugin_package(Path.cwd(), payload["path"])
            if payload["operation"] == "plugin_package"
            else file_operation(Path.cwd(), payload["tool"], payload["args"])
        )
        print(json.dumps({"result": result}))
    except Exception as exc:
        if payload.get("_mcp_dispatched"):
            print(
                json.dumps(
                    {
                        "result": {
                            "isError": True,
                            "content": [
                                {
                                    "type": "text",
                                    "text": "MCP connection closed without a confirmed outcome. Inspect the destination before repeating the action.",
                                }
                            ],
                            "_meta": {"shinbot_outcome": "uncertain"},
                        }
                    }
                )
            )
            sys.exit(0)

        def describe(error):
            return (
                "; ".join(describe(child) for child in error.exceptions)
                if isinstance(error, BaseExceptionGroup)
                else str(error)
            )

        message = (
            "MCP connection failed. Check the server command, URL, credentials and network. "
            "If the server changed, reconnect it in Plugins."
            if payload.get("operation") == "mcp"
            else describe(exc)[:1000]
        )
        print(
            json.dumps(
                {
                    "error": message,
                    "file_rejected": isinstance(exc, FileRejected),
                    "mcp_rejected": payload.get("operation") == "mcp",
                }
            )
        )
        sys.exit(1)
