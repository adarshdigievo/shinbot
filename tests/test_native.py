import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shinbot import native
from shinbot.services import computer


async def test_native_computer_requires_consent(tmp_path, monkeypatch):
    monkeypatch.setattr(computer, "ROOT", tmp_path / "workspace")
    monkeypatch.setattr(computer, "DATA", tmp_path / "data")
    monkeypatch.setattr(computer.sys, "platform", "darwin")
    monkeypatch.delenv("SHINBOT_DEV_EXECUTION", raising=False)
    monkeypatch.delenv("SHINBOT_NATIVE_EXECUTION", raising=False)
    try:
        with pytest.raises(RuntimeError, match="explicit native execution consent"):
            async with computer.lifespan(computer.app):
                pass
    finally:
        computer.journal.db.close()
    monkeypatch.setenv("SHINBOT_NATIVE_EXECUTION", "1")
    async with computer.lifespan(computer.app):
        assert computer.identity() == {}
        assert "SHINBOT_NATIVE_EXECUTION" not in computer.child_env()


def test_stop_disables_restart_even_if_service_is_already_unloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "directory", lambda: tmp_path)
    monkeypatch.setattr(native.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(native, "service_target", lambda: "gui/test/dev.shinbot.native")
    plist = tmp_path / "Library/LaunchAgents/dev.shinbot.native.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("stale")
    with patch.object(native.subprocess, "run") as run:
        run.return_value.returncode = 1
        native.stop()
    assert not plist.exists()


async def test_bot_reports_native_permissions(application, monkeypatch):
    monkeypatch.setenv("SHINBOT_NATIVE_EXECUTION", "1")
    monkeypatch.setenv("SHINBOT_WORKSPACE", "/Users/test/.shinbot/native/workspace")
    core = application.state.core
    assert "not a container sandbox" in core.instructions()
    assert core.updates.snapshot()["installation"] == "native"
    assert core.updates.snapshot()["update_command"] is None
    from shinbot.self_knowledge import help_topic

    assert "native stop" in help_topic("storage")["backup"]
    assert "native start" in help_topic("updates")["apply"]


@pytest.fixture
def native_config(tmp_path, monkeypatch):
    config = {
        "python": "/test/python",
        "node": "/test/node",
        "runtime_dir": "/test/runtime",
        "ports": {"control": 8765, "runtime": 8081, "computer": 8082, "browser": 8083},
        "tokens": {name: name * 40 for name in ("runtime", "computer", "browser")},
        "child_path": "/usr/bin",
        "skills_cli": "/test/skills",
    }
    monkeypatch.setattr(native, "configuration", lambda: (tmp_path, config))
    (tmp_path / "config.json").write_text(json.dumps(config))
    return tmp_path, config


@pytest.mark.parametrize("enabled", [False, True])
def test_native_supervises_services_with_scoped_credentials(native_config, monkeypatch, enabled):
    root, config = native_config
    config["telegram"] = {"enabled": enabled, "bot_token": "private-bot-token", "owner_id": "123456"}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "inherited-secret")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "untrusted-owner")
    monkeypatch.setattr(native.signal, "signal", lambda *args: None)
    launched, stopped = [], []

    class Process:
        def __init__(self, args, **kwargs):
            self.pid = len(launched) + 1
            self.args, self.env = args, kwargs["env"]
            launched.append(self)

        def poll(self):
            return None

        def terminate(self):
            stopped.append(self)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(native.subprocess, "Popen", Process)

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(native.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        native.supervise()
    assert [process.args[-1] if "node" in process.args[0] else process.args[3] for process in launched] == [
        "/test/runtime/dist/server.js",
        "shinbot.services.computer:app",
        "shinbot.services.browser:app",
        "shinbot.app:create_app",
    ]
    runtime, computer, browser, control = launched
    for process in launched[:3]:
        assert "TELEGRAM_BOT_TOKEN" not in process.env and "TELEGRAM_OWNER_ID" not in process.env
    # Each service holds only the credentials it uses, as in the container deployment.
    tokens = ("SHINBOT_SERVICE_TOKEN", "SHINBOT_COMPUTER_TOKEN", "SHINBOT_BROWSER_TOKEN")
    assert [name for name in tokens if name in runtime.env] == ["SHINBOT_SERVICE_TOKEN"]
    assert all(name in control.env for name in tokens)
    for process, name in ((computer, "computer"), (browser, "browser")):
        assert not [key for key in tokens if key in process.env]
        assert process.env["SHINBOT_EXECUTOR_TOKEN"] == name * 40
    assert not [key for process in launched for key in process.env if "CHANNEL" in key]
    if enabled:
        # A connector set up with the old host command is handed to the gateway to adopt.
        assert control.env["TELEGRAM_OWNER_ID"] == "123456"
        assert control.env["TELEGRAM_BOT_TOKEN"] == "private-bot-token"
    else:
        assert "TELEGRAM_OWNER_ID" not in control.env and "TELEGRAM_BOT_TOKEN" not in control.env
    assert stopped[0] is control
    assert not (root / "processes.json").exists()


def test_desktop_enable_restores_previous_configuration_on_failure(native_config, monkeypatch):
    root, config = native_config
    from shinbot import desktop_host

    (root / "computer-data").mkdir()
    (root / "computer-data/receipt").write_text("completed")
    binary = root / "container"
    binary.write_text("test")
    binary.chmod(0o700)
    previous = json.loads((root / "config.json").read_text())
    calls = []
    monkeypatch.setattr(native, "idle_for_backend_change", lambda *a: None)
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(native, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(native, "start", lambda: calls.append("start"))

    def fail_copy(*a, **kw):
        raise RuntimeError("test copy failure")

    monkeypatch.setattr(desktop_host, "offline_copy", fail_copy)
    with pytest.raises(RuntimeError, match="copy failure"):
        native.desktop_enable(runtime=binary, image="test:desktop")
    assert json.loads((root / "config.json").read_text()) == previous
    backup = next((root / "backups").iterdir())
    assert (backup / "computer-data/receipt").read_text() == "completed"
    assert (root / "computer-data/receipt").read_text() == "completed"
    assert calls == ["stop", "stop", "start"]


def test_desktop_disable_keeps_new_plugin_data_and_receipts(native_config, monkeypatch):
    root, config = native_config
    from shinbot import desktop_host

    config.update(computer_consent="macos-account", desktop={"enabled": True})
    (root / "config.json").write_text(json.dumps(config))
    (root / "computer-data").mkdir()
    (root / "computer-data/receipt").write_text("old")
    calls = []
    monkeypatch.setattr(native, "idle_for_backend_change", lambda *a: None)
    monkeypatch.setattr(native, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(native, "start", lambda: calls.append("start"))

    def copy(_root, _config, backup):
        data = backup / "desktop-data/computer"
        data.mkdir(parents=True)
        (data / "receipt").write_text("new")
        (data / "plugin").write_text("installed in Linux")

    monkeypatch.setattr(desktop_host, "offline_copy", copy)
    native.desktop_disable()
    assert (root / "computer-data/receipt").read_text() == "new"
    assert (root / "computer-data/plugin").read_text() == "installed in Linux"
    backup = next((root / "backups").iterdir())
    assert (backup / "computer-data/receipt").read_text() == "old"
    assert json.loads((root / "config.json").read_text())["desktop"]["enabled"] is False
    assert calls == ["stop", "start"]


def test_desktop_services_use_discovered_guest_address(native_config):
    root, config = native_config
    config["desktop"] = {"enabled": True}
    (root / "desktop-endpoints.json").write_text(
        json.dumps({"browser": "http://192.168.64.3:8083", "computer": "http://192.168.64.3:8082"})
    )
    env = native.service_environment(root, config)
    assert env["SHINBOT_BROWSER_URL"] == "http://192.168.64.3:8083"
    assert env["SHINBOT_NATIVE_EXECUTION"] == "0"
    assert env["SHINBOT_WORKSPACE"] == "/workspace"
    assert native.child_environment("desktop", root, config)["SHINBOT_NATIVE_HOME"] == str(root)


async def test_desktop_native_updates_and_backup_guidance(application, monkeypatch):
    from shinbot.self_knowledge import help_topic

    monkeypatch.setenv("SHINBOT_NATIVE_EXECUTION", "0")
    monkeypatch.setenv("SHINBOT_INSTALLATION", "native")
    monkeypatch.setenv("SHINBOT_DESKTOP", "1")
    state = application.state.core.updates.snapshot()
    assert state["installation"] == "native"
    assert state["update_command"] is None
    assert "desktop-backup" in help_topic("storage")["backup"]
    assert "Apple Container" in help_topic("storage")["services"]
    assert "desktop image" in help_topic("updates")["apply"]


@pytest.mark.parametrize("fails", [False, True])
def test_desktop_backup_copies_workspace_and_restarts_on_failure(native_config, monkeypatch, fails):
    from shinbot import desktop_host

    root, config = native_config
    config["desktop"] = {"enabled": True}
    (root / "workspace").mkdir()
    (root / "workspace/work.txt").write_text("keep")
    calls = []
    monkeypatch.setattr(native, "idle_for_backend_change", lambda *a: None)
    monkeypatch.setattr(native, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(native, "start", lambda: calls.append("start"))

    def copy(_root, _config, destination):
        assert (destination / "workspace/work.txt").read_text() == "keep"
        if fails:
            raise RuntimeError("snapshot failed")
        (destination / "desktop-data").mkdir()

    monkeypatch.setattr(desktop_host, "offline_copy", copy)
    if fails:
        with pytest.raises(RuntimeError, match="snapshot failed"):
            native.desktop_backup()
    else:
        native.desktop_backup()
    destination = next((root / "backups").iterdir())
    assert (destination / "complete").exists() is not fails
    assert calls == ["stop", "start"]
