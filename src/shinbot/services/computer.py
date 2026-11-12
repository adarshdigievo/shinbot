import asyncio
import base64
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from shinbot.plugins import validate_package
from shinbot.results import redact_secrets

from . import desktop
from .common import Journal, service_auth, workspace_path
from .worker import FileRejected, validate_file_request

ROOT = Path(os.getenv("SHINBOT_WORKSPACE", "/workspace"))
DATA = Path(os.getenv("SHINBOT_EXECUTOR_DATA", "/data"))
processes = {}
cancelled = set()
journal = None


@asynccontextmanager
async def lifespan(app):
    global journal
    ROOT.mkdir(parents=True, exist_ok=True)
    journal = Journal(DATA)
    if sys.platform == "linux" and os.getuid() == 0 and os.getenv("SHINBOT_SHARED_WORKSPACE") != "1":
        os.chown(
            ROOT,
            int(os.getenv("SHINBOT_WORKSPACE_UID", "1001")),
            int(os.getenv("SHINBOT_WORKSPACE_GID", "1001")),
        )
    elif (
        os.getuid() != 0
        and os.getenv("SHINBOT_DEV_EXECUTION") != "1"
        and os.getenv("SHINBOT_NATIVE_EXECUTION") != "1"
    ):
        raise RuntimeError("Computer requires an isolated Linux runner or explicit native execution consent.")
    yield
    for group in list(processes.values()):
        for process in list(group):
            await terminate(process)
    journal.db.close()


app = FastAPI(
    lifespan=lifespan, dependencies=[Depends(service_auth)], docs_url=None, redoc_url=None, openapi_url=None
)


def child_env():
    return {
        "PATH": os.getenv("SHINBOT_CHILD_PATH", "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(ROOT),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_CACHE_DIR": str(ROOT / ".cache/uv"),
        "SHINBOT_SKILLS_CLI": os.getenv("SHINBOT_SKILLS_CLI", "/opt/skills/node_modules/skills/bin/cli.mjs"),
        **(
            {"DISPLAY": ":99", "XDG_RUNTIME_DIR": str(Path(os.environ["SHINBOT_DESKTOP_STATE"]) / "session")}
            if desktop.enabled()
            else {}
        ),
    }


def identity():
    return (
        {
            "user": int(os.getenv("SHINBOT_WORKSPACE_UID", "1001")),
            "group": int(os.getenv("SHINBOT_WORKSPACE_GID", "1001")),
            "extra_groups": [],
        }
        if sys.platform == "linux" and os.getuid() == 0
        else {}
    )


async def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    except ProcessLookupError:
        pass


async def command(task, command_text, timeout, cwd=None):
    if not isinstance(command_text, str) or len(command_text) > 20000:
        raise ValueError("Invalid command.")
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command_text,
        cwd=cwd or ROOT,
        env=child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        **identity(),
    )
    processes.setdefault(task, set()).add(process)
    output = bytearray()

    async def read():
        while data := await process.stdout.read(8192):
            output.extend(data)
            if len(output) > 100000:
                del output[:-100000]
        await process.wait()

    try:
        if task in cancelled:
            raise ValueError("Command cancelled before execution.")
        await asyncio.wait_for(read(), max(1, min(int(timeout), 300)))
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await terminate(process)
        if task in cancelled:
            raise ValueError("Command cancelled.")
        return {"output": output.decode(errors="replace"), "exit_code": process.returncode, "timed_out": True}
    finally:
        await terminate(process)
        processes.get(task, set()).discard(process)
    return {"output": output.decode(errors="replace"), "exit_code": process.returncode}


class MCPRejected(ValueError):
    """The worker confirmed that it did not dispatch the requested MCP tool."""


@app.exception_handler(MCPRejected)
async def rejected_mcp_setup(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)[:1000]})


async def worker(task, payload, cwd=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "shinbot.services.worker",
        cwd=cwd or ROOT,
        env=child_env(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        **identity(),
    )
    processes.setdefault(task, set()).add(process)
    try:
        if task in cancelled:
            raise ValueError("Worker cancelled before execution.")

        async def communicate():
            process.stdin.write(json.dumps(payload).encode())
            await process.stdin.drain()
            process.stdin.close()
            output = bytearray()
            limit = (
                14_000_000
                if payload.get("operation") == "file" and payload.get("tool") == "attach_file"
                else 12_500_000
                if payload.get("operation") == "plugin_package"
                else 500000
            )
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError("Worker output exceeds the size limit.")
            await process.wait()
            return output

        timeout = (
            payload["timeout"]
            if payload.get("operation") == "plugin_hook"
            else 360
            if payload.get("operation") == "projects"
            else 120
        )
        output = await asyncio.wait_for(communicate(), timeout)
        result = json.loads(output)
        if result.get("error"):
            if payload.get("operation") == "file" and result.get("file_rejected") is True:
                raise FileRejected(result["error"])
            if payload.get("operation") == "mcp" and result.get("mcp_rejected") is True:
                raise MCPRejected(result["error"])
            raise ValueError(result["error"])
        return result["result"]
    finally:
        await terminate(process)
        processes.get(task, set()).discard(process)


async def mcp_call(task, directory, manifest, name=None, arguments=None, environment=None, cwd=None):
    environment = environment or {}
    declared = {field.key for field in manifest.configuration}
    if set(environment) - declared:
        raise MCPRejected("Undeclared plugin settings.")
    return await worker(
        task,
        {
            "operation": "mcp",
            "directory": str(directory),
            "manifest": manifest.model_dump(),
            "name": name,
            "arguments": arguments,
            "environment": environment,
        },
        cwd,
    )


@app.get("/health")
async def health():
    return {"ok": True, "isolated_identity": bool(identity())}


@app.post("/plugins/stage")
async def stage(payload: dict):
    files = {n: base64.b64decode(v, validate=True) for n, v in payload["files"].items()}
    manifest, digest = validate_package(files)
    if digest != payload["digest"]:
        raise HTTPException(400, "Artifact digest mismatch.")
    directory = DATA / "plugins" / manifest.id / manifest.version
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    directory.chmod(0o755)
    # Executable plugin source is read-only to the workspace identity.
    for parent in [DATA / "plugins", DATA / "plugins" / manifest.id]:
        parent.chmod(0o755)
    # Keep journals protected while allowing path traversal to public plugin source.
    DATA.chmod(0o711)
    for name, data in files.items():
        file = directory / name
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if file.exists():
            if file.read_bytes() != data:
                raise HTTPException(409, "Immutable package contents changed.")
        else:
            file.write_bytes(data)
        file.chmod(0o444)
    for folder in directory.rglob("*"):
        if folder.is_dir():
            folder.chmod(0o755)
    marker = directory / ".digest"
    if marker.exists():
        if marker.read_text() != digest:
            raise HTTPException(409, "Immutable package digest changed.")
    else:
        marker.write_text(digest)
    marker.chmod(0o444)
    if payload.get("hooks_only") or manifest.kind != "mcp" and not manifest.tools:
        return {"ok": True}
    return await mcp_call(
        payload.get("task_id", "activation:" + manifest.id),
        directory,
        manifest,
        environment=payload.get("environment"),
    )


@app.post("/plugins/hook")
async def plugin_hook(payload: dict):
    directory = workspace_path(DATA / "plugins", f"{payload['plugin_id']}/{payload['version']}")
    files = {"shinbot-plugin.json": (directory / "shinbot-plugin.json").read_bytes()}
    raw = json.loads(files["shinbot-plugin.json"])
    files.update({name: (directory / name).read_bytes() for name in raw["files"]})
    manifest, digest = validate_package(files)
    if digest != payload["digest"] or digest != (directory / ".digest").read_text():
        raise HTTPException(409, "Immutable hook package changed.")
    hook = next((h for h in manifest.hooks if h.event == payload["event"]), None)
    if not hook:
        raise HTTPException(400, "Hook event not declared by this plugin.")
    environment = payload.get("environment", {})
    if set(environment) - {field.key for field in manifest.configuration}:
        raise HTTPException(400, "Undeclared plugin settings.")
    # Credentials are supplied at execution time and never written to the journal.
    journal_payload = {key: value for key, value in payload.items() if key != "environment"}
    prior = journal.claim(journal_payload)
    if prior is not None:
        return prior
    try:
        result = await worker(
            payload["task_id"],
            {
                "operation": "plugin_hook",
                "directory": str(directory),
                "plugin_id": manifest.id,
                "entrypoint": hook.entrypoint,
                "event": hook.event,
                "timeout": hook.timeout,
                "environment": environment,
                "context": payload["context"],
            },
        )
        result = redact_secrets(
            result, [environment[f.key] for f in manifest.configuration if f.secret and f.key in environment]
        )
        journal.finish(payload["id"], result)
        return result
    except BaseException:
        journal.uncertain(payload["id"])
        raise HTTPException(
            409, "Plugin hook was interrupted or failed. Inspect its state before retrying with a new event."
        ) from None


@app.post("/plugins/package")
async def package_plugin(payload: dict):
    try:
        cwd = workspace_path(ROOT, payload["workspace_path"]) if payload.get("workspace_path") else ROOT
        if not cwd.is_dir():
            raise ValueError("Task worktree is missing. Check Projects before continuing.")
        return await worker(payload["task_id"], {"operation": "plugin_package", "path": payload["path"]}, cwd)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)[:1000]) from exc


@app.post("/plugins/discover")
async def discover(payload: dict):
    from shinbot.extensions import MCPServer

    server = MCPServer.model_validate(payload["server"]).model_dump()
    environment = {}
    for group in ("env", "headers"):
        for index, (key, value) in enumerate(server[group].items()):
            setting = f"SHINBOT_PLUGIN_MCP_{group.upper()}_{index}"
            environment[setting] = value
            server[group][key] = setting
    return await worker(
        payload.get("task_id", "owner-setup"),
        {
            "operation": "mcp",
            "discover": True,
            "manifest": {"server": server},
            "environment": environment,
        },
    )


@app.post("/skills/install")
async def skills_install(payload: dict):
    return await worker(
        payload.get("task_id", "owner-setup"), {"operation": "skills_install", "command": payload["command"]}
    )


@app.post("/execute")
async def execute(payload: dict):
    async with desktop.access():
        return await execute_workspace(payload)


async def execute_workspace(payload: dict):
    environment = payload.pop("environment", {})
    from ..results import redact_secrets

    secret_keys = payload.pop("secret_keys", [])
    secret_values = []
    if payload["task_id"] in cancelled:
        raise HTTPException(423, "Task cancelled before execution.")
    prior = journal.lookup(payload)
    if prior is not None:
        return prior
    if payload["tool"] in {"read_file", "write_file", "list_files", "attach_file"}:
        try:
            validate_file_request(payload["tool"], payload["args"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        cwd = workspace_path(ROOT, payload["workspace_path"]) if payload.get("workspace_path") else ROOT
        if not cwd.is_dir():
            raise ValueError("Task worktree is missing. Check Projects before continuing.")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    prior = journal.claim(payload)
    if prior is not None:
        return prior
    try:
        args, tool = payload["args"], payload["tool"]
        if tool == "run_command":
            result = await command(payload["task_id"], args["command"], args.get("timeout", 120), cwd)
        elif tool in {"read_file", "write_file", "list_files", "attach_file"}:
            result = await worker(payload["task_id"], {"operation": "file", "tool": tool, "args": args}, cwd)
            if tool == "attach_file":
                from ..attachments import FileStore

                result = {
                    "file": FileStore(DATA / "attachments").put(
                        base64.b64decode(result["data"], validate=True), result["name"]
                    )
                }
        elif tool.startswith("plugin:"):
            _, plugin_id, version, name = tool.split(":", 3)
            directory = workspace_path(DATA / "plugins", f"{plugin_id}/{version}")
            files = {"shinbot-plugin.json": (directory / "shinbot-plugin.json").read_bytes()}
            manifest_data = json.loads(files["shinbot-plugin.json"])
            files.update({name: (directory / name).read_bytes() for name in manifest_data["files"]})
            manifest, digest = validate_package(files)
            if digest != (directory / ".digest").read_text():
                raise ValueError("Immutable plugin was modified.")
            secret_values = [
                environment[key]
                for key in set(secret_keys) | {field.key for field in manifest.configuration if field.secret}
                if key in environment
            ]
            result = await mcp_call(payload["task_id"], directory, manifest, name, args, environment, cwd)
            if result.get("_meta", {}).get("shinbot_outcome") == "uncertain":
                raise HTTPException(
                    409, "Plugin outcome is uncertain. Inspect the destination before trying again."
                )
        else:
            raise ValueError("Unknown Computer tool.")
        result = redact_secrets(result, secret_values)
        journal.finish(payload["id"], result)
        return result
    except (FileRejected, MCPRejected) as exc:
        message = redact_secrets(str(exc), secret_values)[:1000]
        journal.reject(payload["id"], message)
        raise HTTPException(400, message) from exc
    except ValueError as exc:
        journal.uncertain(payload["id"])
        raise HTTPException(422, redact_secrets(str(exc), secret_values)[:1000]) from None
    except BaseException:
        journal.uncertain(payload["id"])
        raise


@app.get("/actions/{action_id}")
async def action_receipt(action_id: str):
    return journal.receipt(action_id)


@app.get("/files/{digest}")
async def attachment_file(digest: str):
    from ..attachments import FileStore

    try:
        return FileStore(DATA / "attachments").export(digest)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Attachment not found.") from exc


@app.post("/projects")
async def projects(payload: dict):
    if payload.get("operation") not in {"browse", "inspect", "create", "archive", "changes", "files", "read"}:
        raise HTTPException(400, "Unknown project operation.")
    try:
        return await worker("projects", {"operation": "projects", "args": payload})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/cancel")
async def cancel(payload: dict):
    cancelled.add(payload["task_id"])
    for process in list(processes.get(payload["task_id"], set())):
        await terminate(process)
    return {"cancelled": True}
