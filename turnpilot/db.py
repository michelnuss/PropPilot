import json
import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, property_context TEXT NOT NULL DEFAULT '',
  issue TEXT NOT NULL DEFAULT '', trade TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open', plan_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT UNIQUE, case_id INTEGER REFERENCES cases(id),
  sender TEXT NOT NULL, direction TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'received', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES messages(id),
  path TEXT NOT NULL, mime TEXT NOT NULL, source_url TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
  trade TEXT NOT NULL, status TEXT NOT NULL, plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS agent_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER REFERENCES cases(id),
  message_id INTEGER REFERENCES messages(id), agent TEXT NOT NULL, action TEXT NOT NULL,
  detail_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cases_sender ON cases(sender, status, id);
CREATE INDEX IF NOT EXISTS idx_messages_case ON messages(case_id, id);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def inbound(self, sid: str, sender: str, body: str):
        with self.connect() as db:
            try:
                cur = db.execute(
                    "INSERT INTO messages (sid,sender,direction,body) VALUES (?,?, 'in',?)",
                    (sid, sender, body),
                )
                return cur.lastrowid, True
            except sqlite3.IntegrityError:
                row = db.execute("SELECT id FROM messages WHERE sid=?", (sid,)).fetchone()
                return row["id"], False

    def active_case(self, sender: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM cases WHERE sender=? AND status='open' ORDER BY id DESC LIMIT 1", (sender,)
            ).fetchone()
            return dict(row) if row else None

    def create_case(self, sender: str, property_context: str = "") -> int:
        with self.connect() as db:
            return db.execute(
                "INSERT INTO cases (sender,property_context) VALUES (?,?)", (sender, property_context)
            ).lastrowid

    def attach(self, message_id: int, case_id: int):
        with self.connect() as db:
            db.execute("UPDATE messages SET case_id=? WHERE id=?", (case_id, message_id))

    def photo(self, message_id: int, path: str, mime: str, url: str):
        with self.connect() as db:
            db.execute(
                "INSERT INTO photos (message_id,path,mime,source_url) VALUES (?,?,?,?)",
                (message_id, path, mime, url),
            )

    def update_case(self, case_id: int, triage: dict, plan: dict):
        with self.connect() as db:
            db.execute(
                "UPDATE cases SET issue=?,trade=?,severity=?,plan_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (triage["issue"], triage["trade"], triage["severity"], json.dumps(plan), case_id),
            )
            db.execute(
                "INSERT INTO tasks (case_id,trade,status,plan_json) VALUES (?,?,?,?)",
                (case_id, triage["trade"], "draft_simulated", json.dumps(plan)),
            )

    def action(self, case_id: int | None, message_id: int | None, agent: str, action: str, detail: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO agent_actions (case_id,message_id,agent,action,detail_json) VALUES (?,?,?,?,?)",
                (case_id, message_id, agent, action, json.dumps(detail)),
            )

    def outbound(self, case_id: int | None, sender: str, body: str, status: str):
        with self.connect() as db:
            db.execute(
                "INSERT INTO messages (case_id,sender,direction,body,status) VALUES (?,?,'out',?,?)",
                (case_id, sender, body, status),
            )

    def history(self, case_id: int):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT direction,body FROM messages WHERE case_id=? ORDER BY id DESC LIMIT 12", (case_id,)
            ).fetchall()][::-1]

    def actions(self, limit: int = 30):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM agent_actions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]
