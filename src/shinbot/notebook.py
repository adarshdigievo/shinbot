import hashlib
import json
import re
import time
from pathlib import Path

from .db import uid


class Notebook:
    def __init__(self, db, directory: Path):
        self.db, self.directory = db, directory
        self.export_all()

    def export_all(self):
        for note in self.db.all("SELECT * FROM notes"):
            self.export(note)

    @staticmethod
    def valid_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("Invalid note ID.")

    def export(self, note):
        self.valid_id(note["id"])
        path = self.directory / f"{note['id']}.md"
        temp = path.with_suffix(".tmp")
        temp.write_text(f"# {note['title']}\n\n{note['body']}\n")
        temp.replace(path)

    def save(self, title, body, note_id=None, revision=None):
        if not title.strip() or len(title) > 200 or len(body) > 200000:
            raise ValueError("A note needs a title and at most 200,000 characters.")
        note_id = note_id or uid()
        self.valid_id(note_id)
        with self.db.transaction() as db:
            old = db.execute("SELECT revision FROM notes WHERE id=?", (note_id,)).fetchone()
            if old and old["revision"] != revision:
                raise ValueError("This note changed. Reload it before saving.")
            version = old["revision"] + 1 if old else 1
            db.execute(
                "INSERT INTO notes VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title,body=excluded.body,revision=excluded.revision,updated=excluded.updated",
                (note_id, title, body, version, time.time()),
            )
            db.execute("DELETE FROM note_search WHERE id=?", (note_id,))
            db.execute("INSERT INTO note_search VALUES (?,?,?)", (note_id, title, body))
        note = self.db.one("SELECT * FROM notes WHERE id=?", (note_id,))
        self.export(note)
        return note

    def search(self, query=""):
        if not query.strip():
            return self.db.all("SELECT * FROM notes ORDER BY updated DESC LIMIT 100")
        # Quote search terms; never allow user input to become an FTS expression.
        terms = " OR ".join('"' + t.replace('"', '""') + '"' for t in query.split()[:20])
        return self.db.all(
            "SELECT n.* FROM notes n JOIN note_search s ON n.id=s.id "
            "WHERE note_search MATCH ? ORDER BY rank LIMIT 20",
            (terms,),
        )

    def delete(self, note_id):
        self.valid_id(note_id)
        with self.db.transaction() as db:
            db.execute("DELETE FROM notes WHERE id=?", (note_id,))
            db.execute("DELETE FROM note_search WHERE id=?", (note_id,))
        (self.directory / f"{note_id}.md").unlink(missing_ok=True)

    def remember(self, text, source="owner", bot_id="default"):
        if not text.strip() or len(text) > 4000:
            raise ValueError("Memory must contain 1–4,000 characters.")
        fingerprint = hashlib.sha256(text.strip().casefold().encode()).hexdigest()
        if source != "owner" and self.db.one(
            "SELECT 1 FROM forgotten_bot_memories WHERE hash=? AND bot_id=?", (fingerprint, bot_id)
        ):
            raise ValueError("This memory was forgotten by the owner.")
        existing = self.db.one(
            "SELECT * FROM memories WHERE text=? AND bot_id=? AND deleted=0", (text, bot_id)
        )
        if existing:
            return existing
        key = uid()
        self.db.execute(
            "INSERT INTO memories(id,text,source,created,deleted,bot_id) VALUES (?,?,?,?,0,?)",
            (key, text, source, time.time(), bot_id),
        )
        return self.db.one("SELECT * FROM memories WHERE id=?", (key,))

    def memories(self, query="", bot_id="default"):
        return self.db.all(
            "SELECT * FROM memories WHERE deleted=0 AND bot_id=? AND text LIKE ? ORDER BY created DESC LIMIT 30",
            (bot_id, f"%{query[:200]}%"),
        )

    def forget(self, memory_id, bot_id="default"):
        row = self.db.one(
            "SELECT text FROM memories WHERE id=? AND bot_id=? AND deleted=0", (memory_id, bot_id)
        )
        if row:
            self.db.execute(
                "INSERT OR IGNORE INTO forgotten_bot_memories VALUES (?,?)",
                (hashlib.sha256(row["text"].strip().casefold().encode()).hexdigest(), bot_id),
            )
        self.db.execute("UPDATE memories SET text='',deleted=1 WHERE id=? AND bot_id=?", (memory_id, bot_id))

    def context(self, bot_id="default"):
        return json.dumps(self.memories(bot_id=bot_id), ensure_ascii=False)[:8000]
