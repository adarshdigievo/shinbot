"""Small, cached drafting suggestions, independent of agent actions."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict

import httpx


class Suggestions:
    def __init__(self, core):
        self.core = core
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, bot_id, task_id=None, provider=None, model=None):
        core = self.core
        task = core.tasks.detail(task_id) if task_id else None
        bot = core.bots.get(task["bot_id"] if task else bot_id)
        preferences = core.db.setting("preferences")
        if task and (task["status"] != "completed" or task.get("progress", {}).get("compacting")):
            return {"suggestion": None}
        profile = bot["profile"]
        provider = (
            task["provider"] if task else provider or profile.get("provider") or preferences["provider"]
        )
        model = task["model"] if task else model or profile.get("model") or preferences["model"]
        if not provider or not model or core.config.safe_mode or core.db.setting("restore_hold", False):
            return {"suggestion": None}
        turns = [
            {"user": message["content"][:1500], "assistant": (message["answer"] or "")[-3000:]}
            for message in sorted(
                (task or {}).get("messages", []), key=lambda m: m.get("started") or m["created"]
            )[-2:]
            if message["status"] == "completed"
        ]
        context = {
            "bot": bot["name"],
            "description": profile.get("description", "")[:500],
            "starters": profile.get("starters", [])[:3],
            "recent_turns": turns,
        }
        payload = {
            "provider": provider,
            "model": model,
            "context": json.dumps(context, ensure_ascii=False)[:10000],
        }
        key = hashlib.sha256(json.dumps([bot["id"], task_id, payload], sort_keys=True).encode()).hexdigest()
        async with self.lock:
            now = time.time()
            cached = self.cache.get(key)
            if cached and cached[0] > now:
                return {"suggestion": cached[1]}
            total = core.db.one(
                "SELECT COALESCE(SUM(tokens),0) AS used FROM usage_ledger WHERE created>=?", (now - 86400,)
            )["used"]
            cap = core.db.setting("preferences")["daily_tokens"]
            if cap and total >= cap:
                return {"suggestion": None}
            # Do not spend more on optional suggestions after a capped run.
            if task and task["max_tokens"] and task["tokens"] >= task["max_tokens"]:
                return {"suggestion": None}
            suggestion = None
            try:
                result = await core.coordinator.request(
                    "POST", "/v1/prompt-suggestion", json=payload, timeout=18
                )
                tokens, cost = max(0, int(result.get("tokens", 0))), max(0, float(result.get("cost", 0)))
                if tokens or cost:
                    core.db.execute(
                        "INSERT INTO usage_ledger(task_id,tokens,cost,created) VALUES (?,?,?,?)",
                        ("suggestion:" + key, tokens, cost, time.time()),
                    )
                candidate = result.get("suggestion")
                if (
                    isinstance(candidate, str)
                    and 4 <= len(candidate.strip()) <= 240
                    and "\n" not in candidate
                ):
                    suggestion = candidate.strip()
            except (httpx.HTTPError, ValueError, TypeError):
                pass
            self.cache[key] = (time.time() + (600 if suggestion else 60), suggestion)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return {"suggestion": suggestion}
