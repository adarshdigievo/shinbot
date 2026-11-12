"""Verify persisted browser cookie boundaries using real Playwright contexts in Docker."""

import json
import subprocess
import uuid

from integration_support import COMPOSE, checked, connect, eventually, fixture_settings, ready, wait

c = connect()
ready(c)
fixture_settings(c)
market = c.get("/api/templates").json()
template = next(t for m in market for t in m["templates"] if t["kind"] == "group")
result = c.post("/api/templates/install", json={"template": template, "request_id": uuid.uuid4().hex})
result.raise_for_status()
group = result.json()
private = c.post(
    "/api/bots",
    json={
        "profile": {"name": "Private browser test", "instructions": "Say hello briefly", "browser": "private"}
    },
).json()
profiles = c.get("/api/bots").json()["bots"]
group_ids = [b["browser_id"] for b in profiles if b["id"] in group["bot_ids"]]
assert group_ids[0] == group_ids[1]
# Real production profile switch API; source verifies persistent directory is derived from the pinned bot config.
for bot in [private["id"], *group["bot_ids"], "default"]:
    r = c.post("/api/browser/select", json={"bot_id": bot})
    r.raise_for_status()
    expected = next(b["browser_id"] for b in profiles if b["id"] == bot) if bot != "default" else "shared"
    assert r.json()["browser_id"] == expected
print("PASS owner can select private, group and shared browser profiles", flush=True)


def run(bot, prompt):
    response = c.post("/api/tasks", json={"content": prompt, "bot_id": bot})
    response.raise_for_status()
    task = wait(c, response.json()["id"])
    action = next(a for a in task["actions"] if a["tool"] == "browser" and a["status"] == "completed")
    return json.loads(action["result"])["content"][0]["text"]


other = c.post("/api/bots", json={"profile": {"name": "Other private browser", "browser": "private"}}).json()
assert "private-A" in run(private["id"], "fixture-cookie-set:private-A")
assert "missing" in run(other["id"], "fixture-cookie-read")
assert "private-A" in run(private["id"], "fixture-cookie-read")
assert "group-G" in run(group["bot_ids"][0], "fixture-cookie-set:group-G")
assert "group-G" in run(group["bot_ids"][1], "fixture-cookie-read")
assert "missing" in run("default", "fixture-cookie-read")
print(
    "PASS real production private isolation, group cookie sharing and persistence across switches", flush=True
)
subprocess.run([*COMPOSE, "restart", "browser"], check=True, stdout=subprocess.DEVNULL)
eventually(lambda: checked(c.get("/api/browser")), "browser restart")
assert "private-A" in run(private["id"], "fixture-cookie-read")
print("PASS private profile cookies survive browser service restart", flush=True)
# Agent action must select the same pinned profile, not whichever browser the owner last viewed.
r = c.post("/api/tasks", json={"content": "fixture-browser", "bot_id": private["id"]})
r.raise_for_status()
task = wait(c, r.json()["id"])
assert task["bot_id"] == private["id"] and task["browser_id"] == private["browser_id"]
assert c.get("/api/browser").json()["browser_id"] == private["browser_id"]
print("PASS agent browser action uses the task’s pinned profile", flush=True)
print("PASS bot and group template import", flush=True)
