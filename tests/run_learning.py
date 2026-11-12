"""Real runtime memory tool calls, opt-in permissions and bot isolation."""

from integration_support import checked, connect, fixture_settings, ready, wait


def main():
    c = connect()
    ready(c)
    settings = fixture_settings(c)
    bot = checked(c.post("/api/bots", json={"profile": {"name": "Learning checks"}}))["id"]
    settings["learn_preferences"] = False
    checked(c.put("/api/settings", json=settings))
    held = checked(c.post("/api/tasks", json={"content": "fixture-preference", "bot_id": bot}))
    wait(c, held["id"], "waiting")
    assert checked(c.get("/api/memories", params={"bot_id": bot})) == []
    checked(c.post("/api/tasks/" + held["id"] + "/cancel"))
    settings["learn_preferences"] = True
    checked(c.put("/api/settings", json=settings))
    task = checked(c.post("/api/tasks", json={"content": "fixture-preference", "bot_id": bot}))
    wait(c, task["id"])
    memories = checked(c.get("/api/memories", params={"bot_id": bot}))
    assert len(memories) == 1 and memories[0]["text"] == "Use metric units"
    again = checked(c.post("/api/tasks", json={"content": "fixture-memory-check", "bot_id": bot}))
    assert "Memory refreshed" in wait(c, again["id"])["messages"][-1]["answer"]
    other = checked(c.post("/api/bots", json={"profile": {"name": "Separate memory"}}))["id"]
    isolated = checked(c.post("/api/tasks", json={"content": "fixture-memory-check", "bot_id": other}))
    assert "Memory missing" in wait(c, isolated["id"])["messages"][-1]["answer"]
    checked(c.delete("/api/memories/" + memories[0]["id"], params={"bot_id": bot}))
    forgotten = checked(c.post("/api/tasks", json={"content": "fixture-memory-check", "bot_id": bot}))
    assert "Memory missing" in wait(c, forgotten["id"])["messages"][-1]["answer"]
    settings["learn_preferences"] = False
    checked(c.put("/api/settings", json=settings))
    print(
        "PASS preference opt-in, saved memory across conversations, bot isolation and owner forgetting",
        flush=True,
    )


if __name__ == "__main__":
    main()
