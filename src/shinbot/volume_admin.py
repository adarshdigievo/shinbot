"""Offline volume operations, run only by the host administrator in a temporary container."""

import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

VOLUMES = {"control-data", "runtime-data", "computer-data", "browser-data", "workspace"}


def service_filter(member, destination):
    # Absolute symlinks are data (for example venv/bin/python), never extraction destinations.
    candidate = member.replace(linkname=".") if member.issym() else member
    safe = tarfile.data_filter(candidate, destination)
    mode = (member.mode & 0o755) | 0o700 if member.isdir() else safe.mode
    return safe.replace(
        uid=member.uid, gid=member.gid, uname=None, gname=None, mode=mode, linkname=member.linkname
    )


def validate_archive(archive):
    members = archive.getmembers()
    if len(members) > 500000:
        raise ValueError("Backup contains too many entries.")
    names = set()
    links = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] not in VOLUMES
            or "\x00" in member.name
        ):
            raise ValueError("Backup contains an invalid volume path.")
        if len(path.parts) == 1 and not member.isdir():
            raise ValueError("Volume roots must be directories.")
        if member.name in names:
            raise ValueError("Backup contains duplicate paths.")
        names.add(member.name)
        if member.issym() or member.islnk():
            links.add(path)
        if member.uid not in {0, 1000, 1001} or member.gid not in {0, 1000, 1001}:
            raise ValueError("Backup contains an unsupported service identity.")
    with tempfile.TemporaryDirectory() as empty:
        for member in members:
            path = PurePosixPath(member.name)
            if any(parent in links for parent in path.parents):
                raise ValueError("Backup cannot extract a file through a link.")
            if member.islnk():
                target = PurePosixPath(member.linkname)
                if (
                    target.is_absolute()
                    or ".." in target.parts
                    or target.parts[0] != path.parts[0]
                    or target in links
                    or member.linkname not in names
                ):
                    raise ValueError("Backup contains an unsafe hard link.")
            service_filter(member, empty)
    return members


def backup(destination):
    target = Path(destination)
    if target.exists():
        raise ValueError("Backup already exists; choose another filename.")
    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        # Level 6 is under half the time of the default 9 for about the same size.
        with tarfile.open(temporary, "w:gz", compresslevel=6) as archive:
            for name in sorted(VOLUMES):

                def persistent(member):
                    # Chromium singleton links name an old process, socket and temporary directory.
                    if member.name.startswith("browser-data/") and Path(member.name).name in {
                        "SingletonLock",
                        "SingletonSocket",
                        "SingletonCookie",
                        "DevToolsActivePort",
                    }:
                        return None
                    return member

                archive.add(Path("/volumes") / name, arcname=name, filter=persistent)
        with tarfile.open(temporary, "r:gz") as archive:
            validate_archive(archive)
        temporary.chmod(0o600)
        temporary.replace(target)
        os.chown(target, int(os.getenv("SHINBOT_BACKUP_UID", "0")), int(os.getenv("SHINBOT_BACKUP_GID", "0")))
    finally:
        temporary.unlink(missing_ok=True)


def restore(source):
    with tarfile.open(source, "r:gz") as archive:
        validate_archive(archive)
        names = {PurePosixPath(m.name).parts[0] for m in archive.getmembers()}
        if names != VOLUMES:
            raise ValueError("Backup must contain all five data volumes.")
        for name in VOLUMES:
            root = Path("/volumes") / name
            for item in root.iterdir():
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        archive.extractall("/volumes", filter=service_filter)
    db = sqlite3.connect("/volumes/control-data/shinbot.sqlite")
    with db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('epoch',?)", (json.dumps(uuid.uuid4().hex),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('restore_hold','true')")
        db.execute("UPDATE schedules SET enabled=0")
        db.execute("UPDATE tasks SET status='paused' WHERE status IN ('queued','running','waiting')")
        db.execute(
            "UPDATE actions SET status='uncertain',error='Restored action: verify the destination before resolving.' WHERE status IN ('executing','retrying')"
        )
        db.execute(
            "UPDATE actions SET status='denied',error='Restored work was stopped before execution.' WHERE status IN ('pending','approved')"
        )
        db.execute("UPDATE outbox SET status='uncertain' WHERE status IN ('pending','sending')")
        db.execute("DELETE FROM sessions")
    db.close()
    os.chown("/volumes/control-data/shinbot.sqlite", 1000, 1000)


if __name__ == "__main__":
    os.umask(0o077)
    {"backup": backup, "restore": restore}[sys.argv[1]](sys.argv[2])
    print(f"{sys.argv[1].capitalize()} completed.")
