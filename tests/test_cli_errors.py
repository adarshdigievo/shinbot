import json

import httpx
import pytest
from typer.testing import CliRunner

from shinbot import cli


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (502, b"<html>private proxy debug output</html>", "HTTP 502"),
        (422, b'[{"detail":"private proxy debug output"}]', "HTTP 422"),
        (401, b'{"detail":"Sign in to ShinBot."}', "Sign in to ShinBot."),
        (200, b"<html>private proxy debug output</html>", "invalid response"),
    ],
)
def test_cli_reports_server_errors_without_crashing(monkeypatch, tmp_path, status, body, expected):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    monkeypatch.setattr(
        cli.httpx,
        "request",
        lambda *args, **kwargs: httpx.Response(
            status, content=body, request=httpx.Request("GET", "http://localhost:8765/api/notes")
        ),
    )
    result = CliRunner().invoke(cli.app, ["notes", "list"])
    assert result.exit_code == 1
    assert expected in result.output
    assert "private proxy debug output" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_login_connection_failure_preserves_existing_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    saved = tmp_path / "client.json"
    saved.write_text('{"url":"https://existing.example","token":"existing-token"}')
    before = saved.read_bytes()

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("private transport diagnostic")

    monkeypatch.setattr(cli.httpx, "post", unavailable)
    result = CliRunner().invoke(cli.app, ["login"], input="private-login-password\n")
    assert result.exit_code == 1
    assert "Cannot reach ShinBot" in result.output
    assert "private-login-password" not in result.output
    assert "private transport diagnostic" not in result.output
    assert saved.read_bytes() == before
    assert isinstance(result.exception, SystemExit)


def test_login_rejects_malformed_success_without_saving_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    monkeypatch.setattr(
        cli.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(200, json={"unexpected": "private response output"}),
    )
    result = CliRunner().invoke(cli.app, ["login"], input="private-login-password\n")
    assert result.exit_code == 1
    assert "invalid response" in result.output
    assert "private-login-password" not in result.output
    assert "private response output" not in result.output
    assert not (tmp_path / "client.json").exists()
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("explicit", [False, True])
def test_login_uses_saved_installation_address_unless_overridden(monkeypatch, tmp_path, explicit):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    (tmp_path / "deploy.env").write_text("SHINBOT_ORIGIN=http://localhost:19876\n")
    expected = "https://remote.example" if explicit else "http://localhost:19876"
    requested = []

    def login(url, **kwargs):
        requested.append(url)
        return httpx.Response(200, json={"token": "new-login-token"})

    monkeypatch.setattr(cli.httpx, "post", login)
    arguments = ["login", "--url", expected] if explicit else ["login"]
    result = CliRunner().invoke(cli.app, arguments, input="private-login-password\n")
    assert result.exit_code == 0, result.output
    assert requested == [expected + "/auth/login"]
    assert json.loads((tmp_path / "client.json").read_text()) == {
        "url": expected,
        "token": "new-login-token",
    }


def test_old_telegram_commands_point_to_the_app(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(cli, "compose", lambda *args, **kwargs: calls.append(args))
    for command in (["telegram-setup"], ["telegram-stop"], ["native", "telegram-setup"]):
        result = CliRunner().invoke(cli.app, command)
        assert result.exit_code == 0 and "Plugins → Telegram" in result.output
    assert calls == []
    assert "telegram" not in CliRunner().invoke(cli.app, ["--help"]).output.lower()


def test_start_retires_the_old_telegram_container(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    cli.write_private(tmp_path / "deploy.env", "SHINBOT_PROJECT=demo\n")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="abc123\n" if "ps" in command else "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.retire_telegram_service(["docker", "--context", "default"])
    assert commands[0][-4:] == [
        "--filter",
        "label=com.docker.compose.project=demo",
        "--filter",
        "label=com.docker.compose.service=telegram",
    ]
    assert commands[1] == ["docker", "--context", "default", "rm", "-f", "abc123"]


@pytest.mark.parametrize("operation", ["backup", "restore"])
@pytest.mark.parametrize("partly_stopped", [False, True])
def test_backup_restore_restart_only_what_was_running(monkeypatch, tmp_path, operation, partly_stopped):
    from types import SimpleNamespace

    from test_maintenance import volumes

    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path / "home"))
    cli.write_private(cli.home() / "deploy.env", "SHINBOT_PROJECT=test\n")
    release = {"version": "test", "configuration": {"services": {}}}
    monkeypatch.setattr(cli.maintenance, "remember_release", lambda: release)
    running = ["control", "runtime", "computer"] + ([] if partly_stopped else ["browser"])
    calls = []

    def compose(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            stdout=json.dumps({"services": {s: {} for s in running}})
            if "config" in args
            else "\n".join(running),
            returncode=0,
        )

    def volume_operation(operation, path, **kwargs):
        if operation == "backup":
            volumes(path)

    monkeypatch.setattr(cli, "compose", compose)
    monkeypatch.setattr(cli, "volume_operation", volume_operation)
    archive = tmp_path / "backup.tar.gz"
    args = [operation, str(archive)]
    if operation == "restore":
        volumes(archive)
        args.append("--replace")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    if operation == "backup":
        assert calls[-1] == ("start", *running)
    else:
        assert calls[-1] == ("up", "-d", "--no-build", "--pull", "never", *running)


def test_restore_reports_the_archive_problem_and_accepts_own_large_backups(monkeypatch, tmp_path):
    seen = []

    def restore(source, trusted=False):
        seen.append(trusted)
        if not trusted:
            raise ValueError("Backup contains 600000 entries; an archive from elsewhere may hold 500000.")

    monkeypatch.setattr(cli.maintenance, "restore", restore)
    refused = CliRunner().invoke(cli.app, ["restore", str(tmp_path / "big.tar.gz"), "--replace"])
    assert refused.exit_code != 0 and "600000 entries" in refused.output
    accepted = CliRunner().invoke(cli.app, ["restore", str(tmp_path / "big.tar.gz"), "--replace", "--trusted"])
    assert accepted.exit_code == 0 and seen == [False, True]
