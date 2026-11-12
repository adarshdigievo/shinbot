"""Browser callbacks through the gateway, with a local helper for remote servers."""

import asyncio
import hashlib
import hmac
import secrets
import shlex
import time
from urllib.parse import urlparse


def loopback_callback(value):
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("Invalid local callback address.")
    parsed = urlparse(value)
    if (
        parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
        or parsed.path != "/auth/callback" or parsed.username or parsed.password
        or parsed.query or parsed.fragment or not parsed.port
    ):
        raise ValueError("Use a local HTTP listener at 127.0.0.1/auth/callback.")
    return value


class ProviderLogin:
    def __init__(self, core):
        self.core = core
        self.bridge = None
        self.redirect = None
        self.lock = asyncio.Lock()

    async def runtime(self, method, path="", payload=None):
        return await self.core.coordinator.request(method, "/v1/login" + path, **(
            {"json": payload} if payload is not None else {}
        ))

    async def progress(self):
        if self.bridge:
            bridge = self.bridge
            if bridge["expires"] <= time.time() and bridge["status"] == "pending":
                bridge["status"] = "expired"
            if not bridge.get("runtime_id"):
                return {"id": bridge["id"], "provider": "openai", "status": bridge["status"],
                        "command": bridge["command"] if bridge["status"] == "pending" else None}
            result = await self.runtime("GET")
            if result.get("id") == bridge["runtime_id"]:
                return {**result, "id": bridge["id"]}
            return {"id": bridge["id"], "provider": "openai", "status": "expired"}
        return await self.runtime("GET")

    async def start(self, provider):
        async with self.lock:
            if (await self.progress())["status"] == "pending":
                raise ValueError("A sign-in is already open. Resume or cancel it first.")
            self.bridge = None
            self.redirect = None
            payload = {"provider": provider}
            origin = urlparse(self.core.config.origin)
            if provider == "openai":
                if origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1"}:
                    self.redirect = f"http://127.0.0.1:{origin.port or 80}/auth/callback"
                    payload["redirect_uri"] = self.redirect
                else:
                    code = secrets.token_urlsafe(32)
                    self.bridge = {
                        "id": secrets.token_urlsafe(16), "digest": hashlib.sha256(code.encode()).hexdigest(),
                        "expires": time.time() + 600, "status": "pending",
                        "command": shlex.join([
                            "shinbot", "chatgpt-connect", "--url", self.core.config.origin, "--code", code,
                        ]),
                    }
                    return await self.progress()
            return await self.runtime("POST", payload=payload)

    def require_bridge(self, code):
        bridge = self.bridge
        if (
            not bridge or not isinstance(code, str) or len(code) > 100
            or bridge["expires"] <= time.time() or bridge["status"] != "pending"
            or not hmac.compare_digest(bridge["digest"], hashlib.sha256(code.encode()).hexdigest())
        ):
            raise ValueError("This sign-in request expired. Start again in ShinBot.")
        return bridge

    async def bridge_start(self, code, redirect):
        async with self.lock:
            bridge = self.require_bridge(code)
            if bridge.get("runtime_id"):
                raise ValueError("This helper is already connected.")
            redirect = loopback_callback(redirect)
            result = await self.runtime("POST", payload={"provider": "openai", "redirect_uri": redirect})
            bridge["runtime_id"] = result["id"]
            bridge["redirect"] = redirect
            return result

    async def bridge_progress(self, code):
        self.require_bridge(code)
        return await self.progress()

    async def bridge_callback(self, code, query):
        bridge = self.require_bridge(code)
        if not bridge.get("runtime_id"):
            raise ValueError("Start the local helper first.")
        return await self.callback(query, bridge["redirect"])

    async def callback(self, query, redirect=None):
        redirect = redirect or self.redirect
        if not redirect or len(query) > 20000:
            raise ValueError("No matching sign-in is waiting. Start again in ShinBot.")
        return await self.runtime("POST", "/callback", {"url": redirect + "?" + query})

    async def respond(self, payload):
        async with self.lock:
            if self.bridge:
                bridge = self.bridge
                if payload.get("id") != bridge["id"]:
                    raise ValueError("This sign-in was replaced. Start again.")
                if not payload.get("cancel"):
                    raise ValueError("Finish sign-in through the local helper.")
                bridge["status"] = "cancelled"
                if not bridge.get("runtime_id"):
                    return {"ok": True}
                payload = {"id": bridge["runtime_id"], "cancel": True}
            return await self.runtime("POST", "/respond", payload)
