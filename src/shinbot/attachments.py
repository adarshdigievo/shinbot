"""Immutable file snapshots delivered through authenticated conversation routes."""

import base64
import hashlib
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path

from .db import uid

MAX_FILE_BYTES = 10_000_000


def file_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 180:
        raise ValueError("Give the attachment a filename of 1–180 characters.")
    if any(c in value for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Use a filename without folders or control characters.")
    if value in {".", ".."}:
        raise ValueError("Give the attachment a filename.")
    return value


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class FileStore:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid attachment identifier.")
        path = self.root / digest
        if path.is_symlink():
            raise ValueError("Attachment storage is invalid.")
        return path

    def put(self, data, name):
        name = file_name(name)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Attachments can be up to 10 MB. Split or compress this file first.")
        digest = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.path(digest)
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        preview = image_type(data)
        return {
            "digest": digest,
            "name": name,
            "size": len(data),
            "mime_type": preview or mimetypes.guess_type(name)[0] or "application/octet-stream",
            "preview": bool(preview),
        }

    def export(self, digest):
        with self.path(digest).open("rb") as source:
            data = source.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("The saved attachment is damaged. Create a new attachment.")
        return {"data": base64.b64encode(data).decode()}


def attachment_view(row):
    return {
        **{key: row[key] for key in ("id", "action_id", "name", "size", "mime_type", "created")},
        "preview": bool(row["preview"]),
        "url": f"/api/tasks/{row['task_id']}/attachments/{row['id']}",
    }


class Attachments:
    def __init__(self, db, root):
        self.db, self.files = db, FileStore(root)

    def existing(self, action_id):
        row = self.db.one("SELECT * FROM attachments WHERE action_id=?", (action_id,))
        return attachment_view(row) if row else None

    def publish(self, action, file, exported):
        if existing := self.existing(action["id"]):
            return existing
        encoded = exported.get("data", "")
        if not isinstance(encoded, str) or len(encoded) > (MAX_FILE_BYTES + 2) // 3 * 4:
            raise ValueError("Attachment exceeds 10 MB.")
        data = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(data).hexdigest() != file["digest"]:
            raise ValueError("Attachment transfer could not be verified. Try again.")
        saved = self.files.put(data, file["name"])
        self.db.execute(
            "INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uid(),
                action["id"],
                action["task_id"],
                saved["digest"],
                saved["name"],
                saved["mime_type"],
                saved["size"],
                int(saved["preview"]),
                time.time(),
            ),
        )
        return self.existing(action["id"])
