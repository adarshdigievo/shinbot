import time


def test_chat_search_requires_login_and_validates_input(client, owner):
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    assert client.get("/api/tasks/search?q=hello").status_code == 401


def test_search_finds_old_message_and_answer_preserves_owner_and_literal_query(owner, application):
    core = application.state.core
    bot = core.bots.save({"name": "Travel planner"})
    old = core.tasks.create("Keep the hotel voucher 100%_confirmed.", title="Winter trip", bot_id=bot["id"])
    core.db.execute("UPDATE tasks SET updated=1,status='completed' WHERE id=?", (old["id"],))
    core.db.execute(
        "UPDATE messages SET answer='The cabin has a mountain view.' WHERE task_id=?", (old["id"],)
    )
    for index in range(205):
        core.tasks.create("A newer unrelated chat", title=f"Newer {index}")
    assert old["id"] not in {task["id"] for task in owner.get("/api/tasks").json()}
    found = owner.get("/api/tasks/search", params={"q": "mountain view"}).json()["items"]
    assert [row["id"] for row in found] == [old["id"]]
    assert found[0]["bot_id"] == bot["id"] and found[0]["bot_name"] == "Travel planner"
    assert found[0]["match"] == "answer" and "mountain view" in found[0]["snippet"]
    assert (
        owner.get("/api/tasks/search", params={"q": "100%_confirmed"}).json()["items"][0]["id"] == old["id"]
    )
    assert owner.get("/api/tasks/search", params={"q": "100%_unconfirmed"}).json()["items"] == []
    assert owner.get("/api/tasks/search", params={"q": "winter", "bot_id": "default"}).json()["items"] == []
    assert (
        owner.get("/api/tasks/search", params={"q": "WINTER", "bot_id": bot["id"]}).json()["items"][0]["id"]
        == old["id"]
    )
    core.bots.archive(bot["id"])
    assert (
        owner.get("/api/tasks/search", params={"q": "voucher"}).json()["items"][0]["bot_name"]
        == "Travel planner"
    )
    assert owner.get("/api/tasks/search", params={"q": "' OR 1=1 --"}).json()["items"] == []
    assert owner.get("/api/tasks/search", params={"q": "x" * 201}).status_code == 422
    assert owner.get("/api/tasks/search?offset=-1").status_code == 422


def test_search_paginates_chats_once_and_returns_only_chat_fields(owner, application):
    core = application.state.core
    for index in range(32):
        task = core.tasks.create("Budget review", title=f"Budget {index}")
        core.tasks.message(task["id"], "Budget follow-up", str(index))
    first = owner.get("/api/tasks/search?q=budget").json()
    second = owner.get("/api/tasks/search?q=budget&offset=30").json()
    assert len(first["items"]) == 30 and first["has_more"]
    assert len(second["items"]) == 2 and not second["has_more"]
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    assert set(first["items"][0]) == {
        "id",
        "title",
        "status",
        "bot_id",
        "updated",
        "bot_name",
        "message_id",
        "snippet",
        "match",
    }


def test_chat_details_link_current_and_historical_board_runs_only_when_enabled(owner, application):
    core = application.state.core
    task = core.tasks.create("A previous board run")
    current = core.tasks.create("A current board run")
    card = core.board.save({"title": "Review release", "goal": "Check the changes"})
    core.db.execute("UPDATE work_cards SET task_id=? WHERE id=?", (current["id"], card["id"]))
    core.db.execute(
        "INSERT INTO work_runs(task_id,card_id,created) VALUES (?,?,?)", (task["id"], card["id"], time.time())
    )
    core.db.event("warning", "Waiting for a connection", task["id"])
    assert owner.get("/api/tasks/" + task["id"]).json()["board_cards"] == []
    core.plugins.stage_from_catalog("official", "kanban", "1.0.0")
    core.plugins.activate("kanban", "1.0.0")
    for key in (task["id"], current["id"]):
        assert [c["id"] for c in owner.get("/api/tasks/" + key).json()["board_cards"]] == [card["id"]]
    assert owner.get("/api/tasks/" + task["id"]).json()["events"][0]["title"] == "Waiting for a connection"
