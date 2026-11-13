from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, key TEXT, row_ref TEXT,
  created_at TEXT, received_at TEXT,
  name TEXT, student_name TEXT, phone TEXT, phone_raw TEXT,
  campus TEXT, campus_raw TEXT, grade TEXT, grade_raw TEXT, email TEXT, city TEXT,
  status TEXT, attempts INTEGER DEFAULT 0, next_attempt_at TEXT,
  last_outcome TEXT, transferred_to TEXT, alerted_not_called INTEGER DEFAULT 0,
  note TEXT, raw TEXT, updated_at TEXT,
  UNIQUE(source, key)
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
CREATE TABLE IF NOT EXISTS calls(
  call_ref TEXT PRIMARY KEY, lead_id INTEGER, attempt INTEGER,
  started_at TEXT, provider_call_id TEXT, outcome TEXT, detail TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, kind TEXT, lead_id INTEGER,
  subject TEXT, body TEXT, sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dnc(phone TEXT PRIMARY KEY, added_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


PG_SCHEMA = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    """SQLite for local/tests, Postgres (Supabase) when the path starts with postgres://"""

    def __init__(self, path: str = ":memory:"):
        path = os.environ.get("DATABASE_URL") or path
        self.pg = path.startswith(("postgres://", "postgresql://"))
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            self.db = psycopg.connect(path, autocommit=True, row_factory=dict_row, prepare_threshold=None)
            for stmt in PG_SCHEMA.split(";"):
                if stmt.strip():
                    self.db.execute(stmt)
        else:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)

    def _sql(self, sql: str) -> str:
        if not self.pg:
            return sql
        sql = sql.replace("?", "%s")
        if sql.lstrip().upper().startswith("INSERT OR REPLACE INTO KV"):
            sql = sql.replace("INSERT OR REPLACE INTO kv", "INSERT INTO kv") + " ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v"
        if sql.lstrip().upper().startswith("INSERT OR REPLACE INTO DNC"):
            sql = sql.replace("INSERT OR REPLACE INTO dnc", "INSERT INTO dnc") + \
                " ON CONFLICT (phone) DO UPDATE SET added_at=EXCLUDED.added_at, reason=EXCLUDED.reason"
        return sql

    def q(self, sql: str, args: tuple = ()):
        return self.db.execute(self._sql(sql), args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        return self.db.execute(self._sql(sql), args).fetchone()

    def x(self, sql: str, args: tuple = ()):
        cur = self.db.execute(self._sql(sql), args)
        if not self.pg:
            self.db.commit()
        return cur

    def insert(self, sql: str, args: tuple = ()) -> int:
        """INSERT and return the new row id on either database."""
        if self.pg:
            return self.db.execute(self._sql(sql) + " RETURNING id", args).fetchone()["id"]
        return self.x(sql, args).lastrowid

    def update_lead(self, lead_id: int, now: datetime, **fields):
        fields["updated_at"] = iso(now)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE leads SET {cols} WHERE id=?", (*fields.values(), lead_id))

    def add_alert(self, now: datetime, kind: str, lead_id: int | None, subject: str, body: str) -> int:
        return self.insert("INSERT INTO alerts(created_at,kind,lead_id,subject,body) VALUES(?,?,?,?,?)",
                           (iso(now), kind, lead_id, subject, body))

    def kv_get(self, k: str) -> str | None:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else None

    def kv_set(self, k: str, v: str) -> None:
        self.x("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))

    def is_dnc(self, phone: str) -> bool:
        return self.one("SELECT 1 FROM dnc WHERE phone=?", (phone,)) is not None

    def add_dnc(self, phone: str, now: datetime, reason: str = ""):
        self.x("INSERT OR REPLACE INTO dnc VALUES(?,?,?)", (phone, iso(now), reason))

    @staticmethod
    def dump_raw(d: dict) -> str:
        return json.dumps(d, ensure_ascii=False, default=str)
