import time


def test_authentication_and_host(client):
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/", headers={"host": "attacker.invalid"}).status_code == 400
    assert (
        client.post("/auth/login", json={"bootstrap": "wrong", "password": "long-test-password"}).status_code
        == 401
    )


def test_claim_session_csrf_logout(client, application):
    reply = client.post(
        "/auth/login",
        json={"bootstrap": application.state.core.auth.bootstrap, "password": "test-password-with-length"},
    )
    assert reply.status_code == 200
    assert "httponly" in reply.headers["set-cookie"].lower()
    assert "token" not in reply.json()
    assert client.get("/api/settings").status_code == 200
    assert client.post("/api/tasks", json={"content": "hello"}).status_code == 403
    headers = {"Origin": "http://testserver", "X-Shinbot-CSRF": "1"}
    assert client.post("/api/tasks", json={"content": "hello"}, headers=headers).status_code == 200
    assert client.post("/api/logout", json={}, headers=headers).status_code == 200
    assert client.get("/api/settings").status_code == 401


def test_no_login_csrf(client):
    assert (
        client.post(
            "/auth/login", json={"password": "foo"}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )


def test_followup_deduplication_and_conflicting_payload(owner):
    task = owner.post("/api/tasks", json={"content": "initial"}).json()["id"]
    one = owner.post(f"/api/tasks/{task}/messages", json={"content": "next", "request_id": "same"})
    two = owner.post(f"/api/tasks/{task}/messages", json={"content": "next", "request_id": "same"})
    assert one.json()["id"] == two.json()["id"]
    assert (
        owner.post(
            f"/api/tasks/{task}/messages", json={"content": "changed", "request_id": "same"}
        ).status_code
        == 400
    )
    assert len(owner.get(f"/api/tasks/{task}").json()["messages"]) == 2


def test_notes_optimistic_edit_search_delete(owner):
    note = owner.post("/api/notes", json={"title": "Travel", "body": "Visit Kyoto"}).json()
    edited = owner.post("/api/notes", json={**note, "body": "Visit Kyoto in autumn"})
    assert edited.status_code == 200
    assert owner.post("/api/notes", json={**note, "body": "stale edit"}).status_code == 400
    assert len(owner.get("/api/notes?q=Kyoto").json()) == 1
    assert owner.get("/api/notes?q=%22%20OR%20*").status_code == 200
    assert owner.delete(f"/api/notes/{note['id']}").status_code == 200
    assert owner.get("/api/notes?q=Kyoto").json() == []


def test_memory_forget_removes_content(owner, application):
    memory = owner.post("/api/memories", json={"text": "I prefer trains"}).json()
    owner.delete(f"/api/memories/{memory['id']}")
    assert owner.get("/api/memories").json() == []
    assert "trains" not in application.state.core.notebook.context()
    assert (
        application.state.core.db.one("SELECT text FROM memories WHERE id=?", (memory["id"],))["text"] == ""
    )


def test_internal_endpoints_not_owner_authority(owner):
    assert owner.post("/internal/admit", json={}).status_code == 401
    assert owner.post("/internal/actions", json={}).status_code == 401


def test_revocation_invalidates_all_sessions(owner):
    assert owner.post("/api/sessions/revoke", json={}).status_code == 200
    assert owner.get("/api/settings").status_code == 401


def test_task_permissions_are_snapshotted(owner, application):
    initial = owner.post("/api/tasks", json={"content": "task"}).json()["id"]
    preferences = owner.get("/api/settings").json()
    preferences["computer_auto"] = True
    owner.put("/api/settings", json=preferences)
    later = owner.post("/api/tasks", json={"content": "new task"}).json()["id"]
    assert not owner.get(f"/api/tasks/{initial}").json()["grants"]["computer"]
    assert owner.get(f"/api/tasks/{later}").json()["grants"]["computer"]


def test_expired_approval_is_not_accepted(owner, application):
    core = application.state.core
    task = core.tasks.create("test")
    core.db.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "old",
            task["id"],
            core.db.setting("epoch"),
            "run_command",
            "{}",
            "hash",
            "pending",
            None,
            None,
            time.time() - 100,
            time.time() - 1,
        ),
    )
    assert owner.post("/api/actions/old/decision", json={"approved": True}).status_code == 409
