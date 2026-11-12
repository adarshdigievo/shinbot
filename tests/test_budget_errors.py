import pytest

from shinbot.tasks import LEGACY_BUDGET_ERROR, UNKNOWN_BUDGET_ERROR


@pytest.mark.parametrize(
    "saved,workspace,daily,board,card_limit,expected",
    [
        (0, 0, 0, False, 0, None),
        (0, 1000, 0, False, 0, "workspace_message"),
        (0, 0, 1000, False, 0, "workspace_daily"),
        (1000, 0, 0, False, 0, "saved_chat"),
        (1000, 1000, 0, False, 0, "saved_chat"),
        (1000, 3000, 0, False, 0, "saved_chat"),
        (3000, 1000, 0, False, 0, "workspace_message"),
        (1000, 1000, 1000, False, 0, "workspace_daily"),
        (1000, 0, 0, True, 1000, "card"),
        (1000, 0, 0, True, 3000, "saved_run"),
        (1000, 0, 0, True, 0, "saved_run"),
        (3000, 1000, 0, True, 3000, "workspace_message"),
        (1000, 1000, 0, True, 1000, "card"),
    ],
)
def test_admission_identifies_exhausted_budget(
    application, client, saved, workspace, daily, board, card_limit, expected
):
    core = application.state.core
    task = core.tasks.create("Check the task budget", source="board:unverified")
    key = task["id"]
    core.db.execute("UPDATE tasks SET max_tokens=? WHERE id=?", (saved, key))
    core.db.execute("UPDATE messages SET status='running' WHERE task_id=?", (key,))
    core.db.execute("INSERT INTO submission_budgets SELECT id,0 FROM messages WHERE task_id=?", (key,))
    if board:
        card = core.board.save({"title": "A real card", "goal": "Check its cap", "max_tokens": card_limit})
        core.db.execute("INSERT INTO work_runs(card_id,task_id,created) VALUES (?,?,0)", (card["id"], key))
    core.db.set(
        "preferences", {**core.db.setting("preferences"), "task_tokens": workspace, "daily_tokens": daily}
    )
    response = client.post(
        "/internal/admit",
        json={"task_id": key, "tokens": 1200, "cost": 0},
        headers={"Authorization": "Bearer " + core.config.service_token},
    )
    assert response.is_success
    result = response.json()
    assert result["budget_scope"] == expected
    assert result["allowed"] == (expected is None)


def test_old_budget_errors_display_without_card_claims_and_preserve_saved_data(application, owner):
    core = application.state.core
    key = core.tasks.create("An ordinary chat")["id"]
    core.db.execute("UPDATE tasks SET status='failed',error=? WHERE id=?", (LEGACY_BUDGET_ERROR, key))
    core.db.execute("UPDATE messages SET status='failed',error=? WHERE task_id=?", (LEGACY_BUDGET_ERROR, key))
    core.db.event("failed", LEGACY_BUDGET_ERROR, key)
    task = owner.get("/api/tasks/" + key).json()
    assert task["error"] == task["messages"][0]["error"] == task["events"][0]["title"] == UNKNOWN_BUDGET_ERROR
    assert core.db.one("SELECT error FROM tasks WHERE id=?", (key,))["error"] == LEGACY_BUDGET_ERROR
    assert core.db.one("SELECT error FROM messages WHERE task_id=?", (key,))["error"] == LEGACY_BUDGET_ERROR
