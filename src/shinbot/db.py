import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid():
    return uuid.uuid4().hex


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),
 task_id TEXT NOT NULL REFERENCES tasks(id),digest TEXT NOT NULL,name TEXT NOT NULL,mime_type TEXT NOT NULL,
 size INTEGER NOT NULL,preview INTEGER NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS attachments_task ON attachments(task_id,created);
CREATE TABLE IF NOT EXISTS action_reviews(action_id TEXT PRIMARY KEY,context TEXT,reason TEXT NOT NULL DEFAULT '',once_only INTEGER NOT NULL DEFAULT 0,approved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS github_items(id TEXT PRIMARY KEY,repo TEXT NOT NULL,number INTEGER NOT NULL,payload TEXT NOT NULL,synced REAL NOT NULL);
CREATE TABLE IF NOT EXISTS github_item_state(id TEXT PRIMARY KEY,seen TEXT NOT NULL DEFAULT '',snoozed REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS issue_links(id TEXT PRIMARY KEY,item_id TEXT NOT NULL,kind TEXT NOT NULL,target_id TEXT NOT NULL,request_hash TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_pending(state TEXT PRIMARY KEY,session_hash TEXT NOT NULL,verifier TEXT NOT NULL,expires REAL NOT NULL);

CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL UNIQUE,branch TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS project_workspaces(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
 request_id TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,base_commit TEXT NOT NULL,branch TEXT NOT NULL,path TEXT NOT NULL,
 state TEXT NOT NULL,created REAL NOT NULL,task_id TEXT,parent_task_id TEXT,prompt TEXT NOT NULL,bot_id TEXT NOT NULL,title TEXT NOT NULL,
 notified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS learning_proposals(task_id TEXT PRIMARY KEY,bot_id TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS work_cards(id TEXT PRIMARY KEY,title TEXT NOT NULL,goal TEXT NOT NULL,bot_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'backlog',revision INTEGER NOT NULL DEFAULT 1,task_id TEXT,
 runs INTEGER NOT NULL DEFAULT 0,tokens INTEGER NOT NULL DEFAULT 0,max_runs INTEGER NOT NULL,max_tokens INTEGER NOT NULL,
 auto_continue INTEGER NOT NULL DEFAULT 1,summary TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',next_step TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS work_cards_ready ON work_cards(status,updated);
CREATE TABLE IF NOT EXISTS work_runs(task_id TEXT PRIMARY KEY,card_id TEXT NOT NULL REFERENCES work_cards(id),
 outcome TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',next_step TEXT NOT NULL DEFAULT '',created REAL NOT NULL,finished REAL);
CREATE TABLE IF NOT EXISTS bots(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,name TEXT NOT NULL,profile TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bot_groups(id TEXT PRIMARY KEY,name TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS template_markets(id TEXT PRIMARY KEY,source TEXT UNIQUE NOT NULL,revision TEXT NOT NULL,templates TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS template_installs(request_id TEXT PRIMARY KEY,hash TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_aborts(task_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS action_retries(action_id TEXT PRIMARY KEY,next_attempt REAL NOT NULL,attempts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS forgotten_memories(hash TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS forgotten_bot_memories(hash TEXT NOT NULL,bot_id TEXT NOT NULL,PRIMARY KEY(hash,bot_id));
CREATE TABLE IF NOT EXISTS task_requests(request_id TEXT PRIMARY KEY,hash TEXT NOT NULL,task_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,tokens INTEGER NOT NULL,cost REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS submission_budgets(message_id TEXT PRIMARY KEY,baseline INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,expires REAL NOT NULL,kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,title TEXT NOT NULL,status TEXT NOT NULL,
 provider TEXT NOT NULL,model TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
 conversation_id INTEGER,grants TEXT NOT NULL,extensions TEXT NOT NULL,error TEXT,source TEXT NOT NULL,
 tokens INTEGER NOT NULL DEFAULT 0,cost REAL NOT NULL DEFAULT 0,max_tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
 request_id TEXT NOT NULL,payload TEXT NOT NULL,hash TEXT NOT NULL,status TEXT NOT NULL,
 submission_id INTEGER,answer TEXT,error TEXT,created REAL NOT NULL,UNIQUE(task_id,request_id));
CREATE INDEX IF NOT EXISTS messages_pending ON messages(status,created);
CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,
 revision INTEGER NOT NULL,updated REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS note_search USING fts5(id UNINDEXED,title,body);
CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,
 created REAL NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,name TEXT NOT NULL,prompt TEXT NOT NULL,
 kind TEXT NOT NULL,spec TEXT NOT NULL,timezone TEXT NOT NULL,enabled INTEGER NOT NULL,
 revision INTEGER NOT NULL,next_run REAL,grants TEXT NOT NULL,lateness INTEGER NOT NULL DEFAULT 3600,
 notify INTEGER NOT NULL DEFAULT 1,last_result TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS occurrences(id TEXT PRIMARY KEY,schedule_id TEXT NOT NULL,revision INTEGER NOT NULL,
 intended REAL NOT NULL,task_id TEXT,status TEXT NOT NULL,UNIQUE(schedule_id,revision,intended));
CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,epoch TEXT NOT NULL,
 tool TEXT NOT NULL,args TEXT NOT NULL,hash TEXT NOT NULL,status TEXT NOT NULL,
 result TEXT,error TEXT,created REAL NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approval_rules(id TEXT PRIMARY KEY,scope TEXT NOT NULL,task_id TEXT NOT NULL,
 rule_key TEXT NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,action_id TEXT NOT NULL DEFAULT '',UNIQUE(scope,task_id,rule_key));
CREATE TABLE IF NOT EXISTS marketplaces(id TEXT PRIMARY KEY,source TEXT NOT NULL UNIQUE,revision TEXT,
 catalog TEXT NOT NULL DEFAULT '{}',updated REAL,error TEXT);
CREATE TABLE IF NOT EXISTS plugins(id TEXT NOT NULL,version TEXT NOT NULL,digest TEXT NOT NULL,
 manifest TEXT NOT NULL,path TEXT NOT NULL,status TEXT NOT NULL,marketplace TEXT,created REAL NOT NULL,
 PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
 title TEXT NOT NULL,task_id TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notification_reads(id TEXT PRIMARY KEY,revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channel_updates(channel TEXT NOT NULL,update_id TEXT NOT NULL,task_id TEXT NOT NULL,
 PRIMARY KEY(channel,update_id));
CREATE TABLE IF NOT EXISTS channel_chats(channel TEXT NOT NULL,chat_id TEXT NOT NULL,task_id TEXT NOT NULL,
 PRIMARY KEY(channel,chat_id));
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,channel TEXT NOT NULL,body TEXT NOT NULL,
 status TEXT NOT NULL,created REAL NOT NULL,task_id TEXT,claim_until REAL);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO forgotten_bot_memories SELECT hash, 'default' FROM forgotten_memories"
            )
            for table, columns in {
                "messages": {
                    "finished": "REAL",
                    "started": "REAL",
                    "mode": "TEXT NOT NULL DEFAULT 'queue'",
                    "answer_id": "INTEGER",
                },
                "approval_rules": {"action_id": "TEXT NOT NULL DEFAULT ''"},
                "tasks": {
                    "approval_mode": "TEXT NOT NULL DEFAULT 'auto'",
                    "thinking_level": "TEXT NOT NULL DEFAULT 'off'",
                    "workspace_id": "TEXT",
                    "workspace_path": "TEXT",
                    "parent_task_id": "TEXT",
                    "wait_reason": "TEXT",
                    "bot_id": "TEXT NOT NULL DEFAULT 'default'",
                    "bot_profile": "TEXT NOT NULL DEFAULT '{}'",
                    "browser_id": "TEXT NOT NULL DEFAULT 'shared'",
                },
                "schedules": {
                    "bot_id": "TEXT NOT NULL DEFAULT 'default'",
                    "archived": "INTEGER NOT NULL DEFAULT 0",
                    "last_error": "TEXT NOT NULL DEFAULT ''",
                },
                "occurrences": {"error": "TEXT NOT NULL DEFAULT ''"},
                "memories": {"bot_id": "TEXT NOT NULL DEFAULT 'default'"},
            }.items():
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, declaration in columns.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('epoch',?)", (packed(uid()),))
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('preferences',?)",
                (
                    packed(
                        {
                            "name": "ShinBot",
                            "timezone": "UTC",
                            "provider": "",
                            "model": "",
                            "daily_tokens": 0,
                            "task_tokens": 0,
                            "computer_auto": False,
                            "browser_auto": False,
                            "browser_backend": "camoufox",
                            "browser_close_on_finish": True,
                            "approval_mode": "auto",
                            "thinking_level": "off",
                            "extensions_auto": False,
                            "learn_from_tasks": True,
                            "learn_preferences": True,
                            "max_active_runs": 4,
                        }
                    ),
                ),
            )

            db.commit()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def all(self, query, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(query, args).fetchall()]

    def one(self, query, args=()):
        rows = self.all(query, args)
        return rows[0] if rows else None

    def execute(self, query, args=()):
        with self.transaction() as db:
            return db.execute(query, args).rowcount

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, packed(value)),
        )

    def event(self, kind, title, task_id=None):
        self.execute(
            "INSERT INTO events(kind,title,task_id,created) VALUES (?,?,?,?)",
            (kind, title, task_id, time.time()),
        )

    def record_usage(self, task_id, tokens, cost):
        with self.transaction() as db:
            old = db.execute("SELECT tokens,cost FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not old:
                return
            delta = max(0, int(tokens) - old["tokens"])
            money = max(0, float(cost) - old["cost"])
            if delta or money:
                db.execute(
                    "INSERT INTO usage_ledger(task_id,tokens,cost,created) VALUES (?,?,?,?)",
                    (task_id, delta, money, time.time()),
                )
            db.execute(
                "UPDATE tasks SET tokens=?,cost=? WHERE id=?",
                (max(tokens, old["tokens"]), max(cost, old["cost"]), task_id),
            )
