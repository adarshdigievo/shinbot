"""Control-service credential storage. No secret-reading API is exposed to agents."""

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken


class Vault:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def cipher(self):
        path = self.directory / "key"
        if not path.exists():
            if any(self.directory.glob("*.vault")):
                raise ValueError(
                    "The secrets key is missing. Restore the key with its matching vault backup."
                )
            self.atomic(path, Fernet.generate_key())
        if path.is_symlink():
            raise ValueError("The secrets key must be a regular private file.")
        path.chmod(0o600)
        try:
            return Fernet(path.read_bytes())
        except ValueError:
            raise ValueError("The secrets key is invalid. Restore its matching backup.") from None

    def atomic(self, path, content):
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".vault-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def path(self, scope):
        return self.directory / (hashlib.sha256(scope.encode()).hexdigest() + ".vault")

    def read(self, scope):
        with self.locked():
            return self._read(scope)

    def _read(self, scope):
        path = self.path(scope)
        if not path.exists():
            return {}
        try:
            data = json.loads(self.cipher().decrypt(path.read_bytes()))
            if data["scope"] != scope:
                raise ValueError("scope mismatch")
            return data["values"]
        except (InvalidToken, KeyError, json.JSONDecodeError):
            raise ValueError(
                "The saved secrets could not be unlocked. Restore a matching key and vault backup."
            ) from None

    def update(self, scope, changes, legacy=None):
        with self.locked():
            values = self._read(scope)
            if legacy and legacy.exists():
                values = {**json.loads(legacy.read_text()), **values}
            for key, value in changes.items():
                if value is None:
                    values.pop(key, None)
                elif isinstance(value, str) and len(value) <= 10000 and "\x00" not in value:
                    values[key] = value
                else:
                    raise ValueError("Secrets must be text under 10,000 characters.")
            content = json.dumps({"scope": scope, "values": values}).encode()
            self.atomic(self.path(scope), self.cipher().encrypt(content))
            if legacy:
                legacy.unlink(missing_ok=True)
            return values
