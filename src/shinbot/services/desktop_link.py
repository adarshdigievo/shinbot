"""Desktop browser shortcuts use the managed browser without service credentials."""

import asyncio
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SOCKET = Path("/run/shinbot-desktop/browser-launch.sock")
MAX_REQUEST = 9000


def validate_url(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 8000 or any(ord(c) < 32 for c in value):
        raise ValueError("Enter an HTTP or HTTPS website address.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Open an HTTP or HTTPS link without credentials.")
    return value


async def serve(path, uid, gid, open_browser):
    """Only the desktop identity can ask to open a browser; ownership is checked by the callback."""
    path = Path(path)
    path.unlink(missing_ok=True)

    async def handle(reader, writer):
        try:
            peer = writer.get_extra_info("socket")
            _, peer_uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer_uid not in {0, uid}:
                raise ValueError("This desktop user cannot open the managed browser.")
            raw = await asyncio.wait_for(reader.readline(), 5)
            if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                raise ValueError("The browser request is too large or incomplete.")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"url"}:
                raise ValueError("The desktop browser request is invalid.")
            await asyncio.wait_for(open_browser(validate_url(payload["url"])), 60)
            response = {"ok": True}
        except (ValueError, UnicodeError):
            response = {"ok": False, "error": "Use an HTTP or HTTPS link without credentials."}
        except Exception as exc:
            # FastAPI's errors here are authored by ShinBot; never forward upstream exception text.
            from fastapi import HTTPException

            message = (
                str(exc.detail)[:400]
                if isinstance(exc, HTTPException)
                else "Could not open the browser. Check ShinBot’s Computer view and try again."
            )
            response = {"ok": False, "error": message}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path), limit=MAX_REQUEST + 1)
    os.chown(path, 0, gid)
    path.chmod(0o660)
    return server


def request_open(url=None, path=SOCKET):
    payload = json.dumps({"url": validate_url(url)}, ensure_ascii=False).encode() + b"\n"
    if len(payload) > MAX_REQUEST:
        raise ValueError("The website address is too long.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(65)
        client.connect(str(path))
        client.sendall(payload)
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(4096)
            if not chunk or len(response) + len(chunk) > 4096:
                raise ValueError("The browser service did not return a valid response.")
            response.extend(chunk)
    value = json.loads(response)
    if not value.get("ok"):
        raise ValueError(value.get("error", "Could not open the browser."))


def main():
    try:
        if len(sys.argv) > 2:
            raise ValueError("Open one website at a time.")
        request_open(sys.argv[1] if len(sys.argv) == 2 else None)
    except (OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "The browser service is unavailable. Check ShinBot’s Computer view and try again."
        )
        print(message, file=sys.stderr)
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "ShinBot Browser", message], check=False)
        elif shutil.which("gdbus"):
            subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.Notifications",
                    "--object-path",
                    "/org/freedesktop/Notifications",
                    "--method",
                    "org.freedesktop.Notifications.Notify",
                    "ShinBot",
                    "0",
                    "web-browser",
                    "Browser could not open",
                    message,
                    "[]",
                    "{}",
                    "10000",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
