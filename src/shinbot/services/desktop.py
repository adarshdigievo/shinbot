"""Desktop display and cross-process takeover guard for the optional Linux runner."""

import asyncio
import base64
import fcntl
import json
import os
import re
import struct
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import HTTPException


def enabled():
    return os.getenv("SHINBOT_DESKTOP") == "1"


def application_env():
    return {key: value for key, value in os.environ.items() if not key.startswith(("SHINBOT_", "TELEGRAM_"))}


def user_options():
    return (
        {
            "user": int(os.getenv("SHINBOT_WORKSPACE_UID", "1001")),
            "group": int(os.getenv("SHINBOT_WORKSPACE_GID", "1001")),
            "extra_groups": [],
        }
        if os.getuid() == 0
        else {}
    )


def state_path():
    return Path(os.environ["SHINBOT_DESKTOP_STATE"])


def owner():
    if not enabled():
        return None
    path = state_path() / "owner.json"
    return json.loads(path.read_text()) if path.exists() else None


def set_owner(value):
    if not enabled():
        return
    root = state_path()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "owner.json"
    temp = root / "owner.tmp"
    temp.write_text(json.dumps(value))
    temp.replace(path)


@asynccontextmanager
async def access(*, human=False):
    if not enabled():
        yield
        return
    root = state_path()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "access.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(423, "The computer is finishing an action. Try again shortly.") from None
        try:
            if not human and owner():
                raise HTTPException(423, "You have control of the computer. Hand it back to resume the bot.")
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


async def run(*args, input=None):
    process = await asyncio.create_subprocess_exec(
        *args,
        env=application_env(),
        **user_options(),
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input), 15)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError("Desktop operation failed: " + stderr.decode(errors="replace")[:300])
    return stdout


async def capture():
    with tempfile.TemporaryDirectory(prefix="shinbot-screen-") as temp:
        if os.getuid() == 0:
            os.chown(
                temp,
                int(os.getenv("SHINBOT_WORKSPACE_UID", "1001")),
                int(os.getenv("SHINBOT_WORKSPACE_GID", "1001")),
            )
        path = Path(temp) / "screen.png"
        await run("scrot", "--overwrite", str(path))
        return path.read_bytes()


async def observe():
    pixels = await capture()
    width, height = struct.unpack(">II", pixels[16:24])
    return {
        "content": [
            {
                "type": "text",
                "text": f"Linux desktop, {width} × {height}. Coordinates refer to this image. The workspace is /workspace.",
            },
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(pixels).decode()},
        ]
    }


def validate(args):
    operation = args.get("operation")
    if operation in {"observe", "screenshot"}:
        return
    if operation == "launch" and args.get("application") in {"files", "terminal", "browser"}:
        return
    if operation == "click":
        if all(
            isinstance(args.get(k), int) and not isinstance(args[k], bool) and 0 <= args[k] < limit
            for k, limit in (("x", 1280), ("y", 800))
        ):
            if args.get("button", "left") in {"left", "right", "middle"}:
                return
    if (
        operation == "type"
        and isinstance(args.get("text"), str)
        and len(args["text"]) <= 10000
        and "\0" not in args["text"]
    ):
        return
    if (
        operation == "press"
        and isinstance(args.get("key"), str)
        and re.fullmatch(r"[A-Za-z0-9_+]{1,80}", args["key"])
    ):
        return
    if operation == "scroll" and args.get("direction") in {"up", "down"}:
        if isinstance(args.get("amount", 3), int) and 1 <= args.get("amount", 3) <= 20:
            return
    raise ValueError("Choose observe, screenshot, launch, click, type, press or scroll with valid arguments.")


async def perform(args):
    match args["operation"]:
        case "click":
            await run(
                "xdotool",
                "mousemove",
                str(args["x"]),
                str(args["y"]),
                "click",
                str({"left": 1, "middle": 2, "right": 3}[args.get("button", "left")]),
            )
        case "type":
            await run("xdotool", "type", "--clearmodifiers", "--file", "-", input=args["text"].encode())
        case "press":
            await run("xdotool", "key", "--clearmodifiers", args["key"])
        case "scroll":
            await run(
                "xdotool",
                "click",
                "--repeat",
                str(args.get("amount", 3)),
                "--delay",
                "80",
                "4" if args["direction"] == "up" else "5",
            )
        case "launch":
            command = {
                "files": ["thunar", "/workspace"],
                "terminal": ["xfce4-terminal", "--working-directory=/workspace"],
            }[args["application"]]
            # Desktop applications outlive the requesting HTTP operation.
            process = await asyncio.create_subprocess_exec(
                *command,
                env=application_env(),
                **user_options(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            asyncio.create_task(process.wait())
