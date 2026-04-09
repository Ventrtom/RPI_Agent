"""
SQLite-backed persistence pro session historii.
Umožňuje agentovi obnovit konverzace po restartu.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id   TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'active',
                    created_at   TEXT NOT NULL,
                    last_activity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    ts          TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );
            """)

    def save_session(self, session_id: str, user_id: str, created_at: datetime, last_activity: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, user_id, status, created_at, last_activity)
                VALUES (?, ?, 'active', ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_activity=excluded.last_activity
                """,
                (session_id, user_id, created_at.isoformat(), last_activity.isoformat()),
            )

    def save_message(self, session_id: str, role: str, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (session_id, role, content, datetime.utcnow().isoformat()),
            )

    def mark_closed(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status='closed' WHERE session_id=?",
                (session_id,),
            )
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))

    def load_all_active(self) -> list[dict]:
        """Vrátí všechny aktivní sessions včetně jejich zpráv."""
        with self._connect() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE status='active'"
            ).fetchall()
            result = []
            for s in sessions:
                messages = conn.execute(
                    "SELECT role, content FROM messages WHERE session_id=? ORDER BY id",
                    (s["session_id"],),
                ).fetchall()
                result.append({
                    "session_id": s["session_id"],
                    "user_id": s["user_id"],
                    "created_at": s["created_at"],
                    "last_activity": s["last_activity"],
                    "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
                })
            logger.info("SessionStore: načteno %d aktivních sessions", len(result))
            return result
