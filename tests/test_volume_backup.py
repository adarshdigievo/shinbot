"""Backups of live volumes: unarchivable files are left out, and failures say why."""

import os
import socket
import subprocess
import sys
import tarfile

import pytest

from shinbot import volume_admin
from shinbot.services.common import EXPIRED, RETAINED, Journal


@pytest.fixture
def volumes(monkeypatch, tmp_path):
    root = tmp_path / "volumes"
    for name in volume_admin.VOLUMES:
        (root / name).mkdir(parents=True)
    (root / "workspace/notes.txt").write_text("kept")
    (root / "workspace/link").symlink_to("/usr/bin/python3")
    os.link(root / "workspace/notes.txt", root / "workspace/hard")
    os.mkfifo(root / "workspace/pipe")
    listener = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(root / "browser-data")  # Socket paths are short; pytest's directories are not.
    listener.bind("agent.sock")
    real, written = volume_admin.Path, volume_admin.Archive.gettarinfo

    def mounted(value):
        # The module addresses the mounted volumes by absolute path inside its container.
        return root / str(value)[9:] if str(value).startswith("/volumes") else real(value)

    def service_owned(self, *args, **kwargs):
        member = written(self, *args, **kwargs)
        if member is not None:
            member.uid = member.gid = 1001  # Whoever runs the tests stands in for the workspace user.
        return member

    monkeypatch.setattr(volume_admin, "Path", mounted)
    monkeypatch.setattr(volume_admin.Archive, "gettarinfo", service_owned)
    monkeypatch.setenv("SHINBOT_BACKUP_UID", str(os.getuid()))
    monkeypatch.setenv("SHINBOT_BACKUP_GID", str(os.getgid()))
    yield root
    listener.close()


def test_backup_leaves_out_pipes_and_sockets_and_says_so(volumes, tmp_path, capsys):
    target = tmp_path / "backup.tar.gz"
    volume_admin.backup(str(target))
    with tarfile.open(target) as archive:
        names = set(archive.getnames())
        volume_admin.validate_archive(archive)  # What was written can be restored.
    assert {"workspace/notes.txt", "workspace/link", "workspace/hard"} <= names
    assert not {"workspace/pipe", "browser-data/agent.sock"} & names
    printed = capsys.readouterr().out
    assert "Left out 2" in printed and "workspace/pipe" in printed and "browser-data/agent.sock" in printed
    assert target.stat().st_mode & 0o777 == 0o600


def test_entry_limit_guards_foreign_archives_and_not_this_installations_own(volumes, tmp_path, monkeypatch):
    monkeypatch.setattr(volume_admin, "ENTRY_LIMIT", 5)
    with pytest.raises(ValueError, match=r"contains \d+ entries.*may hold 5"):
        volume_admin.backup(str(tmp_path / "foreign.tar.gz"))
    assert not list(tmp_path.glob("foreign*"))
    monkeypatch.setenv("SHINBOT_BACKUP_TRUSTED", "1")
    volume_admin.backup(str(tmp_path / "own.tar.gz"))
    with tarfile.open(tmp_path / "own.tar.gz") as archive:
        assert len(volume_admin.validate_archive(archive, None)) > 5
        with pytest.raises(ValueError, match="entries"):
            volume_admin.validate_archive(archive, 5)


def test_validation_names_the_file_and_the_command_reports_it_without_a_traceback(tmp_path):
    source = tmp_path / "bad.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for name in sorted(volume_admin.VOLUMES):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        member = tarfile.TarInfo("workspace/theirs")
        member.uid = member.gid = 501
        archive.addfile(member)
    with tarfile.open(source) as archive, pytest.raises(ValueError, match="workspace/theirs belongs to user 501"):
        volume_admin.validate_archive(archive)
    result = subprocess.run(
        [sys.executable, "-m", "shinbot.volume_admin", "restore", str(source)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Restore stopped: workspace/theirs belongs to user 501, group 501; "
        "ShinBot volumes only hold files owned by its services (0, 1000, 1001)."
    )


def test_journal_drops_old_bulky_output_but_never_the_receipt(tmp_path):
    journal = Journal(tmp_path)
    big, small = {"output": "x" * 5000}, {"file": {"digest": "abc", "name": "report.csv"}}
    for key, result in (("old-big", big), ("old-small", small), ("new-big", big)):
        payload = {"id": key, "epoch": "e", "args": {}}
        assert journal.claim(payload) is None
        journal.finish(key, result)
    assert journal.claim({"id": "old-running", "epoch": "e", "args": {}}) is None
    journal.uncertain("old-running")
    journal.db.execute("UPDATE effects SET created=created-? WHERE id LIKE 'old-%'", (RETAINED + 60,))
    journal.db.close()
    journal = Journal(tmp_path)  # Trimming runs when the service starts.
    assert journal.lookup({"id": "new-big", "epoch": "e", "args": {}}) == big
    assert journal.lookup({"id": "old-small", "epoch": "e", "args": {}}) == small
    # The expired action is still known: a replay gets a notice and is never executed again.
    assert journal.lookup({"id": "old-big", "epoch": "e", "args": {}}) == EXPIRED
    assert journal.receipt("old-big")["status"] == "completed"
    assert journal.receipt("old-running")["status"] == "uncertain"
    assert journal.db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 4
    # A long-running service trims once a day as it claims new work.
    journal.db.execute("UPDATE effects SET created=created-? WHERE id='new-big'", (RETAINED + 60,))
    journal.trimmed -= 90000
    assert journal.claim({"id": "later", "epoch": "e", "args": {}}) is None
    assert journal.lookup({"id": "new-big", "epoch": "e", "args": {}}) == EXPIRED
