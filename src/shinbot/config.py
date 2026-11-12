import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data: Path = field(default_factory=lambda: Path(os.getenv("SHINBOT_DATA", "~/.shinbot")).expanduser())
    runtime_url: str = field(
        default_factory=lambda: os.getenv("SHINBOT_RUNTIME_URL", "http://127.0.0.1:8081")
    )
    computer_url: str = field(
        default_factory=lambda: os.getenv("SHINBOT_COMPUTER_URL", "http://127.0.0.1:8082")
    )
    browser_url: str = field(
        default_factory=lambda: os.getenv("SHINBOT_BROWSER_URL", "http://127.0.0.1:8083")
    )
    origin: str = field(
        default_factory=lambda: os.getenv("SHINBOT_ORIGIN", "http://localhost:8765").rstrip("/")
    )
    service_token: str = field(default_factory=lambda: os.getenv("SHINBOT_SERVICE_TOKEN", ""))
    computer_token: str = field(default_factory=lambda: os.getenv("SHINBOT_COMPUTER_TOKEN", ""))
    browser_token: str = field(default_factory=lambda: os.getenv("SHINBOT_BROWSER_TOKEN", ""))
    channel_token: str = field(default_factory=lambda: os.getenv("SHINBOT_CHANNEL_TOKEN", ""))
    safe_mode: bool = field(default_factory=lambda: os.getenv("SHINBOT_SAFE_MODE", "0") == "1")
    coordinate: bool = True

    def prepare(self):
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("notes", "plugins", "marketplaces", "artifacts"):
            (self.data / name).mkdir(exist_ok=True, mode=0o700)
        return self


def secret_file(path: Path) -> str:
    import tempfile

    descriptor, temporary = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as out:
            value = secrets.token_urlsafe(32)
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        result = path.read_text().strip()
        if len(result) < 32:
            raise ValueError(f"Invalid secret file {path.name}. Stop ShinBot and restore it from a backup.")
        return result
    finally:
        os.unlink(temporary)
