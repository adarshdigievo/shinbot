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


def test_telegram_setup_checks_token_and_preserves_settings_on_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    path = tmp_path / "deploy.env"
    path.write_text("SHINBOT_ORIGIN=http://localhost:8765\n")
    before = path.read_text()
    calls = []
    monkeypatch.setattr(cli, "compose", lambda *args: calls.append(args))
    original = httpx.Client
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"ok": False})), **kwargs
        ),
    )
    token = "12345:" + "x" * 35
    result = CliRunner().invoke(cli.app, ["telegram-setup"], input=token + "\n123456\n")
    assert result.exit_code != 0 and "rejected" in result.output
    assert token not in result.output and path.read_text() == before and calls == []


@pytest.mark.parametrize("after_rollback", [False, True])
def test_telegram_setup_writes_private_configuration_and_prints_link(monkeypatch, tmp_path, after_rollback):
    monkeypatch.setattr(cli.maintenance, "remember_release", lambda: None)
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    restored = tmp_path / "rollback-compose.json"
    if after_rollback:
        restored.write_text(
            json.dumps(
                {
                    "services": {
                        "control": {"environment": {"LITERAL": "keep$$literal"}},
                        "telegram": {"environment": {"TELEGRAM_BOT_TOKEN": "old"}},
                    }
                }
            )
        )
    path = tmp_path / "deploy.env"
    path.write_text("SHINBOT_ORIGIN=http://localhost:8765\n")
    calls = []
    monkeypatch.setattr(cli, "compose", lambda *args: calls.append(args))
    original = httpx.Client

    def handler(request):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"username": "test_shinbot"} if request.url.path.endswith("getMe") else {"url": ""},
            },
        )

    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    token = "12345:" + "x" * 35
    result = CliRunner().invoke(cli.app, ["telegram-setup", "--build"], input=token + "\n123456\n")
    assert result.exit_code == 0, result.output
    assert token not in result.output and "https://t.me/test_shinbot" in result.output
    assert path.stat().st_mode & 0o777 == 0o600
    assert "TELEGRAM_OWNER_ID=123456" in path.read_text()
    if after_rollback:
        services = json.loads(restored.read_text())["services"]
        assert services["telegram"]["environment"]["TELEGRAM_BOT_TOKEN"] == token
        assert services["control"]["environment"]["TELEGRAM_OWNER_ID"] == "123456"
        assert services["control"]["environment"]["LITERAL"] == "keep$$literal"
    assert calls == [
        ("exec", "-T", "control", "python", "-c", cli.TELEGRAM_SCAN),
        ("--profile", "telegram", "up", "-d", "--build", "control", "telegram"),
    ]


@pytest.mark.parametrize("operation", ["backup", "restore"])
@pytest.mark.parametrize("telegram_running", [False, True])
def test_backup_restore_preserve_stopped_telegram(monkeypatch, tmp_path, operation, telegram_running):
    from types import SimpleNamespace

    from test_maintenance import volumes

    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path / "home"))
    cli.write_private(cli.home() / "deploy.env", "SHINBOT_PROJECT=test\n")
    release = {"version": "test", "configuration": {"services": {}}}
    monkeypatch.setattr(cli.maintenance, "remember_release", lambda: release)
    running = ["control", "runtime", "computer", "browser"] + (["telegram"] if telegram_running else [])
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
        assert calls[-1] == ("--profile", "telegram", "start", *running)
    else:
        assert calls[-1] == ("--profile", "telegram", "up", "-d", "--no-build", "--pull", "never", *running)
