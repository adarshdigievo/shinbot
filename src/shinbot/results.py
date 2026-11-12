"""Keep transient image payloads out of duplicate action journals."""

import copy
import json
import time
from collections import OrderedDict


def redact_secrets(value, secrets):
    """Remove literal credential echoes before persisting a plugin result."""
    if isinstance(value, str):
        for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact_secrets(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact_secrets(key, secrets): redact_secrets(item, secrets) for key, item in value.items()}
    return value


def without_images(value):
    if isinstance(value, list):
        return [without_images(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "image" and "data" in value:
        return {
            "type": "text",
            "text": "[Screenshot omitted from replay. Observe again for the current page.]",
        }
    return {key: without_images(item) for key, item in value.items()}


class RecentResults:
    def __init__(self, limit=8_000_000, ttl=120):
        self.limit, self.ttl = limit, ttl
        self.items = OrderedDict()
        self.bytes = 0

    def prune(self):
        now = time.monotonic()
        while self.items and (self.bytes > self.limit or next(iter(self.items.values()))[0] < now):
            _, (_, size, _) = self.items.popitem(last=False)
            self.bytes -= size

    def put(self, key, result):
        self.prune()
        safe = without_images(result)
        if result != safe:
            size = len(json.dumps(result).encode())
            old = self.items.pop(key, None)
            if old:
                self.bytes -= old[1]
            if size <= self.limit:
                self.items[key] = (time.monotonic() + self.ttl, size, copy.deepcopy(result))
                self.bytes += size
                self.prune()
        return safe

    def get(self, key, fallback):
        self.prune()
        return self.items[key][2] if key in self.items else fallback
