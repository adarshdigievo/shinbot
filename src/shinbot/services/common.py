import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException, Request

from shinbot.db import packed
from shinbot.results import RecentResults


def service_auth(request: Request):
    token = os.environ.get("SHINBOT_EXECUTOR_TOKEN", "")
    if len(token) < 32 or not hmac.compare_digest(
        request.headers.get("authorization", ""), f"Bearer {token}"
    ):
        raise HTTPException(401, "Unauthorized")


# A receipt is kept for good so a replay can never run an action twice; only old bulky output is dropped.
RETAINED = 30 * 86400
EXPIRED = {
    "isError": True,
    "content": [
        {
            "type": "text",
            "text": "This action completed earlier and its saved output has expired. "
            "Inspect the destination instead of repeating it.",
        }
    ],
}


class Journal:
    def __init__(self, directory: Path):
        self.recent_results = RecentResults()
        os.umask(0o077)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "actions.sqlite", isolation_level=None)
        for file in directory.glob("actions.sqlite*"):
            file.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS effects(id TEXT PRIMARY KEY,hash TEXT,status TEXT,result TEXT,epoch TEXT,created REAL)"
        )
        self.db.execute("UPDATE effects SET status='uncertain' WHERE status='executing'")
        self.trim()

    def trim(self):
        self.trimmed = time.time()
        # Small results, such as attachment references, stay exact.
        self.db.execute(
            "UPDATE effects SET result=? WHERE status='completed' AND created<? AND length(result)>2000",
            (packed(EXPIRED), self.trimmed - RETAINED),
        )

    def lookup(self, payload):
        digest = hashlib.sha256(packed(payload).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM effects WHERE id=?", (payload["id"],)).fetchone()
        if row:
            if row["hash"] != digest:
                raise HTTPException(409, "Action inputs changed.")
            if row["status"] == "completed":
                return self.recent_results.get(payload["id"], json.loads(row["result"]))
            if row["status"] == "rejected":
                raise HTTPException(400, json.loads(row["result"])["error"])
            raise HTTPException(
                409, "An earlier action may have completed. Inspect the destination before repeating it."
            )
        return None

    def receipt(self, key):
        row = self.db.execute("SELECT status,result FROM effects WHERE id=?", (key,)).fetchone()
        if not row:
            return {"status": "missing"}
        return {
            "status": row["status"],
            **({"error": json.loads(row["result"])["error"]} if row["status"] == "rejected" else {}),
            "result": self.recent_results.get(key, json.loads(row["result"]))
            if row["status"] == "completed"
            else None,
        }

    def claim(self, payload):
        prior = self.lookup(payload)
        if prior is not None:
            return prior
        digest = hashlib.sha256(packed(payload).encode()).hexdigest()
        if time.time() - self.trimmed > 86400:
            self.trim()
        self.db.execute(
            "INSERT INTO effects VALUES (?,?,'executing',NULL,?,?)",
            (payload["id"], digest, payload["epoch"], time.time()),
        )
        return None

    def finish(self, key, result):
        self.db.execute(
            "UPDATE effects SET status='completed',result=? WHERE id=?",
            (packed(self.recent_results.put(key, result)), key),
        )

    def uncertain(self, key):
        self.db.execute("UPDATE effects SET status='uncertain' WHERE id=?", (key,))

    def reject(self, key, error):
        self.db.execute(
            "UPDATE effects SET status='rejected',result=? WHERE id=?", (packed({"error": error}), key)
        )


def workspace_path(root: Path, name: str):
    if not isinstance(name, str) or "\x00" in name:
        raise ValueError("Invalid path.")
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("Path leaves the workspace.")
    return candidate
