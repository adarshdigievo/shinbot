"""Exercise Projects in an isolated Docker deployment with real Git, Pi and browser UI."""

import json
import subprocess
import uuid

from integration_support import (
    COMPOSE,
    ROOT,
    STATE,
    URL,
    approve,
    checked,
    connect,
    eventually,
    fixture_settings,
    ready,
    wait,
)
from playwright.sync_api import expect, sync_playwright

OUT = ROOT / "test-results/projects"
OUT.mkdir(parents=True, exist_ok=True)


def main():
    client = connect()
    ready(client)
    fixture_settings(client, computer_auto=True)
    repo = "acorn-app"
    code = f"""
import pathlib,subprocess
p=pathlib.Path('/workspace/{repo}');p.mkdir()
def git(*args):return subprocess.run(['git','-C',str(p),*args],check=True,capture_output=True)
git('init','-b','main')
(p/'app.py').write_text("print('Hello from the project')\\n")
git('add','app.py')
git('-c','user.name=Fixture','-c','user.email=fixture@example.test','-c','commit.gpgsign=false','commit','-m','Fixture')
"""
    subprocess.run(
        [*COMPOSE, "exec", "-T", "--user", "1001:1001", "computer", "python", "-c", code], check=True
    )
    password = json.loads(STATE.read_text())["password"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, color_scheme="light")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(URL)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Your bots")).to_be_visible()
        expect(page.locator('[data-view="projects"]')).to_have_count(0)
        expect(page.locator('[data-view="board"]')).to_have_count(0)
        page.locator('[data-view="plugins"]').click()
        page.locator('[data-install="projects"]').click()
        page.get_by_role("dialog", name="Set up Projects", exact=True).get_by_role(
            "button", name="Enable plugin", exact=True
        ).click()
        expect(page.locator('[data-view="projects"]')).to_be_visible()
        expect(page.locator('[data-view="board"]')).to_have_count(0)
        page.locator('[data-view="projects"]').click()
        expect(page.get_by_role("heading", name="Choose a repository to begin")).to_be_visible()
        page.screenshot(path=str(OUT / "projects-empty.png"), full_page=True)
        page.locator("#add-project").click()
        page.get_by_label("Folder in Computer").fill(repo)
        page.get_by_role("button", name="Open folder", exact=True).click()
        page.get_by_role("button", name="Use this repository", exact=True).click()
        expect(page.locator(".project-heading")).to_contain_text(repo)
        project = next(x for x in checked(client.get("/api/projects"))["projects"] if x["path"] == repo)
        page.locator("#new-project-task").click()
        page.get_by_label("Task name", exact=True).fill("Account settings")
        page.get_by_label("What should the bot do?").fill("fixture-project:account-settings:18")
        page.get_by_role("button", name="Create worktree and start").click()
        expect(page.locator(".project-task-title")).to_contain_text("Account settings")
        first = checked(client.get(f"/api/projects/{project['id']}/workspaces"))[0]
        second = checked(
            client.post(
                f"/api/projects/{project['id']}/tasks",
                json={
                    "prompt": "fixture-project:email-preferences:18",
                    "title": "Email preferences",
                    "request_id": uuid.uuid4().hex,
                },
            )
        )
        # Auto mode pauses both file-writing commands until the owner approves them.
        tasks = [first["task_id"], second["id"]]
        for task_id in tasks:
            wait(client, task_id, "waiting")
        for task_id in tasks:
            approve(client, task_id, "run_command")
        eventually(
            lambda: all(
                any(
                    action["status"] == "executing"
                    for action in checked(client.get("/api/tasks/" + key))["actions"]
                )
                for key in tasks
            ),
            "two concurrent worktree tasks",
        )
        page.locator("#refresh-project").click()
        expect(page.locator(".project-task")).to_have_count(2)
        page.screenshot(path=str(OUT / "projects-parallel-light.png"), full_page=True)
        # Restart control while both Computer commands run. Their effects must stay singular.
        subprocess.run([*COMPOSE, "restart", "control"], check=True, capture_output=True)
        ready(client)
        wait(client, first["task_id"], timeout=100)
        wait(client, second["id"], timeout=100)
        for workspace, label in [
            (first["id"], "account-settings"),
            (second["workspace_id"], "email-preferences"),
        ]:
            text = checked(
                client.get(f"/api/project-workspaces/{workspace}/read", params={"file": "feature.txt"})
            )["text"]
            assert text.splitlines() == [label], text
        # Delegation is approved in chat even with automatic Computer access enabled.
        reviewer = checked(client.post("/api/bots", json={"profile": {"name": "Reviewer"}}))["id"]
        delegated = checked(
            client.post(
                f"/api/projects/{project['id']}/tasks",
                json={
                    "prompt": "fixture-delegate:" + reviewer,
                    "title": "Independent review",
                    "request_id": uuid.uuid4().hex,
                },
            )
        )
        held = wait(client, delegated["id"], "waiting")
        approval = next(a for a in held["actions"] if a["status"] == "pending")
        assert approval["tool"] == "plugin:projects:1.1.0:delegate_task"
        checked(client.post("/api/actions/" + approval["id"] + "/decision", json={"approved": True}))
        child = eventually(
            lambda: next(
                (
                    w
                    for w in checked(client.get(f"/api/projects/{project['id']}/workspaces"))
                    if w["parent_task_id"] == delegated["id"] and w["task_id"]
                ),
                None,
            ),
            "delegated child worktree",
        )
        approve(client, child["task_id"], "run_command")
        wait(client, child["task_id"])
        result = eventually(
            lambda: next(
                (
                    m
                    for m in checked(client.get("/api/tasks/" + delegated["id"]))["messages"]
                    if m["request_id"].startswith("delegation-result:")
                ),
                None,
            ),
            "child result delivery",
        )
        assert "completed" in result["content"]
        page.reload()
        page.locator('[data-view="projects"]').click()
        page.locator('[data-project="' + project["id"] + '"]').click()
        page.locator(".project-tasks").get_by_role("button", name="Account settings", exact=True).click()
        expect(page.locator("#changes-panel")).to_contain_text("feature.txt")
        page.get_by_role("tab", name="Files", exact=True).click()
        page.locator('[data-worktree-file="feature.txt"]').click()
        expect(page.locator("#worktree-file-content")).to_have_text("account-settings\n")
        page.screenshot(path=str(OUT / "projects-files-light.png"), full_page=True)
        page.get_by_role("button", name="Close worktree details", exact=True).click()
        page.locator(".page").evaluate("element => element.scrollTop = 0")
        page.emulate_media(color_scheme="dark")
        page.screenshot(path=str(OUT / "projects-dark.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#navigation-panel")).to_be_hidden()
        page.locator(".page").evaluate("element => element.scrollTop = 0")
        page.screenshot(path=str(OUT / "projects-mobile-dark.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Mobile horizontal overflow"
        )
        page.emulate_media(color_scheme="light")
        page.screenshot(path=str(OUT / "projects-mobile-light.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1050})
        # Archived task chats remain directly accessible from Projects.
        clean = checked(
            client.post(
                f"/api/projects/{project['id']}/tasks",
                json={
                    "prompt": "Explain the repository without changing files.",
                    "title": "Repository overview",
                    "request_id": uuid.uuid4().hex,
                },
            )
        )
        wait(client, clean["id"])
        checked(client.post(f"/api/project-workspaces/{clean['workspace_id']}/archive", json={}))
        page.locator("#refresh-project").click()
        archived = page.locator(".project-task").filter(
            has=page.get_by_role("button", name="Repository overview", exact=True)
        )
        archived.get_by_role("button", name="Open chat").click()
        expect(page.locator("#task-title")).to_have_text("Repository overview")
        page.screenshot(path=str(OUT / "projects-archived-chat.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print(
        "PASS optional plugin navigation, repository picker, two concurrent worktrees, control restart without duplicate writes, approved delegation and returned results, files, archived chats, light/dark/mobile browser flows",
        flush=True,
    )


if __name__ == "__main__":
    main()
