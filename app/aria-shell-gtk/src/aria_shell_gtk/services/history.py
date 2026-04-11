from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    title: str
    created_at: str
    last_message: str
    last_timestamp: str
    message_count: int


@dataclass(frozen=True)
class SessionMessage:
    role: str
    content: str
    timestamp: str


class HistoryService:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, id)")
            conn.commit()

    def list_sessions(self, limit: int = 40) -> list[SessionSummary]:
        if not self.db_path.exists():
            return []
        sql = """
            SELECT
                s.id,
                s.title,
                s.created_at,
                COALESCE(
                    (SELECT content FROM messages m WHERE m.session_id = s.id ORDER BY m.id DESC LIMIT 1),
                    ''
                ) AS last_message,
                COALESCE(
                    (SELECT timestamp FROM messages m WHERE m.session_id = s.id ORDER BY m.id DESC LIMIT 1),
                    s.created_at
                ) AS last_timestamp,
                COALESCE(
                    (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id),
                    0
                ) AS message_count
            FROM sessions s
            ORDER BY last_timestamp DESC, s.created_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (int(limit),)).fetchall()
        return [
            SessionSummary(
                session_id=str(row["id"]),
                title=str(row["title"] or "New Chat"),
                created_at=str(row["created_at"] or ""),
                last_message=str(row["last_message"] or ""),
                last_timestamp=str(row["last_timestamp"] or row["created_at"] or ""),
                message_count=int(row["message_count"] or 0),
            )
            for row in rows
        ]

    def create_session(self, session_id: str, title: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, title, created_at) VALUES (?, ?, ?)",
                (session_id, title, now),
            )
            conn.commit()

    def rename_session(self, session_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
            conn.commit()

    def delete_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()

    def add_message(self, session_id: str, role: str, content: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            conn.commit()

    def get_messages(self, session_id: str, limit: int | None = None) -> list[SessionMessage]:
        if not self.db_path.exists():
            return []
        sql = (
            "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id ASC"
            if limit is None
            else "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?"
        )
        with self._connect() as conn:
            if limit is None:
                rows = conn.execute(sql, (session_id,)).fetchall()
            else:
                rows = list(reversed(conn.execute(sql, (session_id, int(limit))).fetchall()))
        return [
            SessionMessage(
                role=str(row["role"] or "assistant"),
                content=str(row["content"] or ""),
                timestamp=str(row["timestamp"] or ""),
            )
            for row in rows
        ]

    def count_user_messages(self, session_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ? AND role = 'user'",
                (session_id,),
            ).fetchone()
            return int(row["c"]) if row else 0
