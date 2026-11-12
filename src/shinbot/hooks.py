"""Version-pinned plugin lifecycle calls with durable Computer receipts."""

import base64
import hashlib
import json

import httpx

from .db import packed


class HookBlocked(ValueError):
    pass


class Hooks:
    def __init__(self, core):
        self.core = core

    async def run(self, event, event_id, task, data):
        core, output = self.core, dict(data)
        task_id = task.get("task_id") or task["id"]
        selected = json.loads(task["extensions"])
        for reference in sorted(selected):
            if not reference.startswith("plugin:"):
                continue
            _, plugin, version = reference.split(":", 2)
            row = core.db.one("SELECT * FROM plugins WHERE id=? AND version=?", (plugin, version))
            if (
                not row
                or row["status"] not in {"active", "draining"}
                or not core.plugins.allowed(plugin, task["bot_id"])
            ):
                continue
            manifest = json.loads(row["manifest"])
            hook = next((h for h in manifest.get("hooks", []) if h["event"] == event), None)
            if not hook:
                continue
            epoch = core.db.setting("epoch")
            key = (
                "hook-" + hashlib.sha256(packed([epoch, event, event_id, row["digest"]]).encode()).hexdigest()
            )
            context = {"event_id": key, "task_id": task_id, "bot_id": task["bot_id"], "data": output}
            input_hash = hashlib.sha256(packed(context).encode()).hexdigest()
            saved = core.db.setting(key)
            if saved:
                if saved["input_hash"] != input_hash:
                    raise HookBlocked(f"{plugin}: lifecycle event inputs changed. Start a new request.")
            if saved and "result" in saved:
                result = saved["result"]
            else:
                core.db.set(key, {"input_hash": input_hash})
                try:
                    receipt = await core.remote("computer", f"/actions/{key}", method="GET", timeout=10)
                    if receipt["status"] == "completed":
                        result = receipt["result"]
                    elif receipt["status"] == "executing":
                        raise httpx.ConnectError("Waiting for the plugin hook to finish.")
                    elif receipt["status"] != "missing":
                        raise HookBlocked(
                            f"{plugin}: the {event} hook was interrupted. Inspect plugin state before starting a new request."
                        )
                    else:
                        _, files = core.plugins.package(plugin, version)
                        environment = core.plugins.configuration(plugin, version, environment=True)
                        # Restored Computer volumes can be empty. Stage immutable source without rerunning a tool.
                        await core.remote(
                            "computer",
                            "/plugins/stage",
                            {
                                "files": {n: base64.b64encode(v).decode() for n, v in files.items()},
                                "digest": row["digest"],
                                "task_id": task_id,
                                "environment": environment,
                                "hooks_only": True,
                            },
                        )
                        result = await core.remote(
                            "computer",
                            "/plugins/hook",
                            {
                                "id": key,
                                "epoch": epoch,
                                "task_id": task_id,
                                "plugin_id": plugin,
                                "version": version,
                                "digest": row["digest"],
                                "event": event,
                                "context": context,
                                "environment": environment,
                            },
                            timeout=hook.get("timeout", 10) + 10,
                        )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        receipt = await core.remote("computer", f"/actions/{key}", method="GET", timeout=10)
                        if receipt["status"] == "executing":
                            raise httpx.ConnectError("Waiting for the plugin hook to finish.") from None
                    if exc.response.status_code >= 500:
                        raise
                    raise HookBlocked(
                        f"{plugin}: the {event} hook failed. Inspect its state before starting a new request."
                    ) from None
                self.validate(event, result)
                core.db.set(key, {"input_hash": input_hash, "result": result})
            if result.get("block"):
                raise HookBlocked(f"{plugin}: {result['block']}")
            output.update({key: value for key, value in result.items() if key != "block"})
        return output

    @staticmethod
    def validate(event, result):
        allowed = {
            "before_chat": {"context", "block"},
            "before_tool": {"args", "block"},
            "after_tool": {"result"},
            "after_response": set(),
        }[event]
        if not isinstance(result, dict) or set(result) - allowed or len(packed(result)) > 200000:
            raise HookBlocked(f"Invalid output from {event} hook. Check its return fields and size.")
        for key in ("context", "block"):
            if key in result and (not isinstance(result[key], str) or len(result[key]) > 20000):
                raise HookBlocked(f"Hook {key} must be text up to 20,000 characters.")
        if "args" in result and not isinstance(result["args"], dict):
            raise HookBlocked("Hook tool arguments must be an object.")

    async def notify(self, event, event_id, task, data):
        try:
            return await self.run(event, event_id, task, data)
        except HookBlocked as exc:
            key = "hook-warning:" + event + ":" + event_id
            if not self.core.db.setting(key):
                self.core.db.event("plugin", str(exc), task.get("task_id") or task["id"])
                self.core.db.set(key, True)
            return data
