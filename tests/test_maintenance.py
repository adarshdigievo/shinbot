"""Recovery contracts: no destructive work before validation, and restart-safe fallback."""

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from shinbot import cli, maintenance
from shinbot.volume_admin import VOLUMES


def volumes(path, content=b"before"):
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(VOLUMES):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        member = tarfile.TarInfo("control-data/value")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))


@pytest.fixture
def installation(monkeypatch, tmp_path):
    monkeypatch.setenv("SHINBOT_HOME", str(tmp_path / "home"))
    cli.write_private(
        cli.home() / "deploy.env", "SHINBOT_PROJECT=test\nTELEGRAM_OWNER_ID=123\nTELEGRAM_BOT_TOKEN=private\n"
    )
    config = {
        "services": {
            name: {"image": "sha256:old-" + name, "environment": {}} for name in maintenance.SERVICES
        },
        "volumes": {},
    }
    release = {"version": "previous", "configuration": config}
    maintenance.write_json(cli.home() / "release.json", release)
    events = []
    monkeypatch.setattr(cli, "docker", lambda: ["docker"])
    monkeypatch.setattr(cli, "running_services", lambda: list(maintenance.CORE))
    monkeypatch.setattr(maintenance, "live_release", lambda: release)

    def compose(*args, **kwargs):
        events.append(("compose", args))
        return subprocess.CompletedProcess(args, 0, json.dumps(config), "")

    def volume_operation(operation, path, configuration=None):
        events.append((operation, Path(path)))
        if operation == "backup":
            volumes(path)

    monkeypatch.setattr(cli, "compose", compose)
    monkeypatch.setattr(cli, "volume_operation", volume_operation)
    monkeypatch.setattr(
        maintenance.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", "")
    )
    return events, release


def test_backup_includes_private_host_settings_and_validates_every_payload(installation, tmp_path):
    archive = tmp_path / "complete.tar.gz"
    maintenance.backup(archive)
    assert archive.stat().st_mode & 0o777 == 0o600
    with maintenance.unpack(archive) as (data, root, manifest):
        assert manifest["format"] == 2
        assert "TELEGRAM_BOT_TOKEN=private" in (root / "deploy.env").read_text()
        assert not (root / "client.json").exists()
        with tarfile.open(data) as tar:
            assert tar.extractfile("control-data/value").read() == b"before"
    damaged = tmp_path / "damaged.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(damaged, "w:gz") as out:
        for member in src.getmembers():
            content = src.extractfile(member).read()
            if member.name == "deploy.env":
                content += b"changed"
                member.size = len(content)
            out.addfile(member, io.BytesIO(content))
    events, _ = installation
    events.clear()
    with pytest.raises(ValueError, match="checksum"):
        maintenance.restore(damaged)
    assert events == []


def test_backup_failure_restarts_only_previously_running_services(installation, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "volume_operation", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        maintenance.backup(tmp_path / "backup.tar.gz")
    assert installation[0][-1] == ("compose", ("--profile", "telegram", "start", *maintenance.CORE))
    assert not (tmp_path / "backup.tar.gz").exists()


def test_legacy_backup_still_supported_and_traversal_rejected(tmp_path):
    archive = tmp_path / "legacy.tar.gz"
    volumes(archive)
    with maintenance.unpack(archive) as (source, root, manifest):
        assert source == archive and root is None and manifest["format"] == 1
    with tarfile.open(archive, "w:gz") as out:
        member = tarfile.TarInfo("../deploy.env")
        out.addfile(member, io.BytesIO())
    with pytest.raises(ValueError, match="invalid volume path"):
        with maintenance.unpack(archive):
            pass


def test_restore_keeps_destination_identity_and_restores_connector_settings(installation, tmp_path):
    archive = tmp_path / "backup.tar.gz"
    maintenance.backup(archive)
    cli.write_private(
        cli.home() / "deploy.env",
        "SHINBOT_PROJECT=destination\nSHINBOT_DOCKER_ENGINE=new-engine\nSHINBOT_SERVICE_TOKEN=new-service-token\nTELEGRAM_OWNER_ID=999\n",
    )
    cli.write_private(cli.home() / "client.json", '{"token":"expired"}')
    maintenance.restore(archive)
    saved = cli.deployment()
    assert saved["SHINBOT_PROJECT"] == "destination"
    assert saved["SHINBOT_DOCKER_ENGINE"] == "new-engine"
    assert saved["SHINBOT_SERVICE_TOKEN"] == "new-service-token"
    assert saved["TELEGRAM_OWNER_ID"] == "123"
    assert saved["TELEGRAM_BOT_TOKEN"] == "private"
    assert not (cli.home() / "client.json").exists()
    assert list((cli.home() / "backups").glob("before-restore-*.tar.gz"))


def test_update_download_failure_never_stops_services(installation, monkeypatch):
    original = cli.compose

    def compose(*args, **kwargs):
        if "pull" in args:
            raise RuntimeError("offline")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "compose", compose)
    with pytest.raises(RuntimeError, match="offline"):
        maintenance.update()
    assert not any("stop" in args for kind, args in installation[0] if kind == "compose")
    assert not (cli.home() / "recovery.json").exists()


def test_failed_update_restores_original_data_and_release(installation, monkeypatch):
    outcomes = iter([False, True])

    def health(_):
        if not next(outcomes):
            raise RuntimeError("runtime is broken")

    monkeypatch.setattr(maintenance, "healthy", health)
    with pytest.raises(cli.typer.Exit):
        maintenance.update()
    events, release = installation
    assert any(kind == "restore" for kind, _ in events)
    state = maintenance.recovery_state()
    assert not state["pending"] and state["rolled_back"]
    assert json.loads((cli.home() / "release.json").read_text()) == release
    assert (
        json.loads((cli.home() / "rollback-compose.json").read_text())["services"]["control"]["image"]
        == "sha256:old-control"
    )
    count = len(events)
    maintenance.rollback()
    assert len(events) == count


def test_interrupted_update_can_resume_rollback_without_network(installation, monkeypatch):
    def interrupted(_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(maintenance, "healthy", interrupted)
    with pytest.raises(KeyboardInterrupt):
        maintenance.update()
    assert maintenance.recovery_state()["pending"]
    with pytest.raises(cli.typer.BadParameter, match="interrupted"):
        maintenance.check_pending()
    monkeypatch.setattr(maintenance, "healthy", lambda _: None)
    maintenance.rollback()
    assert maintenance.recovery_state()["rolled_back"]


def test_manual_rollback_preserves_post_update_work(installation, monkeypatch):
    monkeypatch.setattr(maintenance, "healthy", lambda _: None)
    maintenance.update()
    assert not maintenance.recovery_state()["pending"]
    assert not (cli.home() / "rollback-compose.json").exists()
    maintenance.rollback()
    assert list((cli.home() / "backups").glob("before-rollback-*.tar.gz"))
    assert maintenance.recovery_state()["rolled_back"]


def test_lock_rejects_overlapping_installation_commands(installation):
    with maintenance.locked(), pytest.raises(cli.typer.BadParameter, match="Another installation"):
        with maintenance.locked():
            pass


def test_compose_snapshot_preserves_literal_dollars(tmp_path):
    file = tmp_path / "compose.json"
    maintenance.compose_document(file, {"services": {"control": {"environment": {"KEY": "secret$VALUE"}}}})
    assert json.loads(file.read_text())["services"]["control"]["environment"]["KEY"] == "secret$$VALUE"


def test_docker_child_keeps_maintenance_lock_if_launcher_exits(installation):
    import sys

    with maintenance.locked():
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.3)"], **maintenance.child_lock()
        )
    try:
        with pytest.raises(cli.typer.BadParameter, match="Another installation"):
            with maintenance.locked():
                pass
    finally:
        child.wait(timeout=5)
    with maintenance.locked():
        pass


def test_update_checkpoint_write_failure_restarts_original_containers(installation, monkeypatch):
    original = maintenance.write_json

    def full_disk(path, value):
        if path.name == "recovery.json":
            raise OSError("disk full")
        return original(path, value)

    monkeypatch.setattr(maintenance, "write_json", full_disk)
    with pytest.raises(OSError, match="disk full"):
        maintenance.update()
    assert installation[0][-1] == ("compose", ("--profile", "telegram", "start", *maintenance.CORE))
    assert not (cli.home() / "rollback-compose.json").exists()


def test_manual_rollback_works_when_current_gateway_container_is_missing(installation, monkeypatch):
    monkeypatch.setattr(maintenance, "healthy", lambda _: None)
    maintenance.update()

    def broken_current_release():
        raise cli.typer.BadParameter("Gateway container is missing")

    monkeypatch.setattr(maintenance, "live_release", broken_current_release)
    monkeypatch.setattr(cli, "running_services", lambda: ["runtime", "computer", "browser"])
    maintenance.rollback()
    assert maintenance.recovery_state()["rolled_back"]
    assert list((cli.home() / "backups").glob("before-rollback-*.tar.gz"))
