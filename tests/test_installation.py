"""Upgrade behavior that must preserve owner choices and saved data."""

from shinbot.bots import OFFICIAL_TEMPLATES, Bots


def test_bundled_catalog_refresh_keeps_existing_bots_and_remote_catalog(application):
    core = application.state.core
    profile = core.bots.save({"name": "My edited helper", "instructions": "Keep this role"})
    original = core.bots.templates()[0]
    core.db.execute(
        "UPDATE template_markets SET templates='[]', revision='bundled-old' WHERE source=?",
        (OFFICIAL_TEMPLATES,),
    )
    upgraded = Bots(core.db)
    assert upgraded.templates()[0]["templates"] == original["templates"]
    assert upgraded.get(profile["id"])["profile"]["instructions"] == "Keep this role"

    # A repository refresh belongs to the owner; a packaged starter catalog cannot replace it.
    core.db.execute(
        "UPDATE template_markets SET templates='[]', revision=? WHERE source=?",
        ("a" * 40, OFFICIAL_TEMPLATES),
    )
    restarted = Bots(core.db)
    assert restarted.templates()[0]["templates"] == []
    assert restarted.templates()[0]["revision"] == "a" * 40
    assert restarted.get(profile["id"])["name"] == "My edited helper"


def test_saved_docker_context_is_used_after_the_default_changes(monkeypatch, tmp_path):
    import subprocess

    from shinbot import cli

    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    monkeypatch.delenv("SHINBOT_DOCKER_CONTEXT", raising=False)
    (tmp_path / "deploy.env").write_text("SHINBOT_DOCKER_CONTEXT=saved\nSHINBOT_DOCKER_ENGINE=original\n")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/docker")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, '{"ID":"original","Name":"host","DockerRootDir":"/data"}', ""
        )

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.docker() == ["docker", "--context", "saved"]
    assert all("--context" in call and call[2] == "saved" for call in calls)


def test_replaced_docker_engine_cannot_start_an_empty_installation(monkeypatch, tmp_path):
    import subprocess

    from typer.testing import CliRunner

    from shinbot import cli

    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    monkeypatch.delenv("SHINBOT_DOCKER_CONTEXT", raising=False)
    config = tmp_path / "deploy.env"
    config.write_text("SHINBOT_DOCKER_CONTEXT=saved\nSHINBOT_DOCKER_ENGINE=original\n")
    before = config.read_bytes()
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/docker")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, '{"ID":"different-engine","Name":"host","DockerRootDir":"/data"}', ""
        )

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = CliRunner().invoke(cli.app, ["start", "--no-open-browser"])
    assert result.exit_code != 0
    assert "differs from the saved installation" in result.output
    assert not any("up" in call for call in calls)
    assert config.read_bytes() == before


def test_missing_saved_volume_is_reported_without_creating_replacement(monkeypatch, tmp_path):
    import json
    import subprocess

    from typer.testing import CliRunner

    from shinbot import cli

    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    (tmp_path / "deploy.env").write_text("SHINBOT_PROJECT=shinbot\nSHINBOT_INITIALIZED=1\n")
    monkeypatch.setattr(cli, "docker", lambda: ["docker", "--context", "saved"])
    calls = []

    def compose(*args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"volumes": {"control-data": {"name": "shinbot_control-data"}}})
        )

    monkeypatch.setattr(cli, "compose", compose)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = CliRunner().invoke(cli.app, ["start", "--no-open-browser"])
    assert result.exit_code != 0
    assert "data volume is missing" in result.output
    assert not any("up" in call for call in calls)


def test_commands_before_first_start_offer_setup_without_traceback(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from shinbot import cli

    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path))
    for command in ["stop", "status", "setup-token"]:
        result = CliRunner().invoke(cli.app, [command])
        assert result.exit_code != 0
        assert "not been started" in result.output
        assert "shinbot start" in result.output
        assert isinstance(result.exception, SystemExit)


def test_interrupted_settings_replace_keeps_previous_credentials(monkeypatch, tmp_path):
    import pytest

    from shinbot import cli

    saved = tmp_path / "deploy.env"
    cli.write_private(saved, "SHINBOT_SERVICE_TOKEN=original-private-token\n")
    before = saved.read_bytes()

    def interrupted(*args):
        raise OSError("disk error before replacement")

    monkeypatch.setattr(cli.os, "replace", interrupted)
    with pytest.raises(OSError):
        cli.write_private(saved, "SHINBOT_SERVICE_TOKEN=replacement-token\n")
    assert saved.read_bytes() == before
    assert saved.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [saved]


def test_volatile_compatibility_ids_use_stable_host_storage_identity(monkeypatch):
    import json
    import subprocess

    from shinbot import cli

    sequence = iter(["request-1", "request-2", "request-3", "request-4", "request-5", "request-6"])
    storage = {"path": "/home/alex/.local/share/containers/storage"}

    def info(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"ID": next(sequence), "Name": "host", "DockerRootDir": storage["path"]}), ""
        )

    monkeypatch.setattr(cli.subprocess, "run", info)
    identity = cli.docker_engine(["docker", "--context", "saved"])
    assert cli.docker_engine(["docker", "--context", "saved"]) == identity
    storage["path"] = "/home/someone-else/.local/share/containers/storage"
    assert cli.docker_engine(["docker", "--context", "saved"]) != identity
