"""Install, uninstall and reinstall a wheel against the owned test deployment."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from integration_support import COMPOSE, HOME, ROOT, URL, checked, connect, install_echo_plugin, ready, wait


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def main():
    wheel = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    if wheel is None:
        command("uv", "build", "--wheel", "--out-dir", str(HOME / "dist"), cwd=ROOT)
        wheel = next((HOME / "dist").glob("*.whl"))
    c = connect()
    settings = checked(c.get("/api/settings"))
    settings.update(provider="custom-fixture", model="fixture", browser_auto=True)
    checked(c.put("/api/settings", json=settings))
    bot = checked(c.post("/api/bots", json={"profile": {"name": "Reinstall helper", "browser": "private"}}))
    note = checked(c.post("/api/notes", json={"title": "Reinstall note", "body": "Keep this note"}))
    memory = checked(c.post("/api/memories", json={"text": "I prefer short replies", "bot_id": bot["id"]}))
    job = checked(
        c.post(
            "/api/jobs",
            json={
                "name": "Reinstall routine",
                "prompt": "hello",
                "kind": "interval",
                "spec": "86400",
                "timezone": "UTC",
                "bot_id": bot["id"],
            },
        )
    )
    task = checked(
        c.post("/api/tasks", json={"content": "fixture-cookie-set:reinstall-cookie", "bot_id": bot["id"]})
    )
    wait(c, task["id"])
    install_echo_plugin(c)
    saved_plugins = checked(c.get("/api/plugins"))["installed"]
    command(
        *COMPOSE,
        "exec",
        "-T",
        "--user",
        "1001:1001",
        "computer",
        "python",
        "-c",
        "from pathlib import Path; Path('/workspace/reinstall.txt').write_text('Keep this file')",
    )
    cli_config = {"url": URL, "token": c.headers["Authorization"].removeprefix("Bearer ")}
    config = HOME / "client.json"
    config.write_text(json.dumps(cli_config))
    config.chmod(0o600)

    env = {**os.environ, "SHINBOT_HOME": str(HOME)}
    for name in ["SHINBOT_COMPOSE_FILE", "PYTHONPATH", "VIRTUAL_ENV"]:
        env.pop(name, None)
    installations = Path(tempfile.mkdtemp(prefix="reinstall-", dir=HOME))
    first, second = installations / "first-python", installations / "reinstalled-python"
    for target in [first, second]:
        command("uv", "venv", "--python", sys.executable, str(target))
    command("uv", "pip", "install", "--python", str(first / "bin/python"), str(wheel))
    command(str(first / "bin/shinbot"), "start", "--no-open-browser", env=env, cwd=HOME)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [HOME / "deploy.env", config]}
    command(str(first / "bin/shinbot"), "stop", env=env, cwd=HOME)
    command("uv", "pip", "uninstall", "--python", str(first / "bin/python"), "shinbot")
    absent = subprocess.run([str(first / "bin/python"), "-I", "-c", "import shinbot"], capture_output=True)
    assert absent.returncode != 0, "The original wheel must actually be uninstalled"
    shutil.rmtree(first)
    command("uv", "pip", "install", "--python", str(second / "bin/python"), str(wheel))
    unrelated = HOME / "unrelated-directory"
    unrelated.mkdir(exist_ok=True)
    # Uses the release Compose file shipped inside the installed wheel, from another directory.
    command(str(second / "bin/shinbot"), "start", "--no-open-browser", env=env, cwd=unrelated)
    ready(c)
    assert before == {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [HOME / "deploy.env", config]
    }
    c = connect()
    assert checked(c.get("/api/settings")) == settings
    assert any(n["id"] == note["id"] and n["body"] == "Keep this note" for n in checked(c.get("/api/notes")))
    assert any(m["id"] == memory["id"] for m in checked(c.get("/api/memories", params={"bot_id": bot["id"]})))
    assert any(j["id"] == job["id"] and j["enabled"] for j in checked(c.get("/api/jobs")))
    assert checked(c.get("/api/tasks/" + task["id"]))["bot_id"] == bot["id"]
    assert checked(c.get("/api/plugins"))["installed"] == saved_plugins
    workspace = command(*COMPOSE, "exec", "-T", "computer", "cat", "/workspace/reinstall.txt")
    assert workspace.stdout == "Keep this file"
    cli_notes = command(str(second / "bin/shinbot"), "notes", "list", env=env, cwd=unrelated)
    assert "Keep this note" in cli_notes.stdout
    print(
        "PASS pip-compatible wheel uninstall/reinstall in a new environment preserves account, CLI login, settings, bots, conversations, notes, memories, jobs and plugin versions",
        flush=True,
    )

    # Reapply the explicit local model fixture after testing the unmodified packaged launcher.
    command(*COMPOSE, "up", "-d")
    ready(c)
    task = checked(c.post("/api/tasks", json={"content": "fixture-cookie-read", "bot_id": bot["id"]}))
    result = wait(c, task["id"])
    action = next(a for a in result["actions"] if a["tool"] == "browser" and a["status"] == "completed")
    assert "reinstall-cookie" in json.loads(action["result"])["content"][0]["text"]
    print(
        "PASS private browser profile and cookies survive package uninstall/reinstall and service restart",
        flush=True,
    )


if __name__ == "__main__":
    main()
