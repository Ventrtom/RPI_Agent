"""
SQLite-backed persistence for scheduled tasks and execution history.
Mirrors the style of core/session_store.py.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scheduler.models import ExecutionLog, Task, TaskStatus, TaskType

logger = logging.getLogger(__name__)

_INIT_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    run_at          TEXT,
    cron_expr       TEXT,
    timezone        TEXT NOT NULL DEFAULT 'Europe/Prague',
    status          TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    last_run_at     TEXT,
    next_run_at     TEXT,
    end_at          TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, next_run_at);

CREATE TABLE IF NOT EXISTS execution_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    outcome       TEXT NOT NULL,
    error_message TEXT,
    response_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_execlog_task_id ON execution_log(task_id);
"""


def _dt(value: Optional[str]) -> Optional[datetime]:
    """Parse UTC ISO-8601 string to aware datetime, or return None."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize aware datetime to UTC ISO-8601 string, or return None."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        name=row["name"],
        prompt=row["prompt"],
        task_type=TaskType(row["task_type"]),
        timezone=row["timezone"],
        status=TaskStatus(row["status"]),
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        timeout_seconds=row["timeout_seconds"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        run_at=_dt(row["run_at"]),
        cron_expr=row["cron_expr"],
        last_run_at=_dt(row["last_run_at"]),
        next_run_at=_dt(row["next_run_at"]),
        end_at=_dt(row["end_at"]),
    )


def _row_to_log(row: sqlite3.Row) -> ExecutionLog:
    return ExecutionLog(
        id=row["id"],
        task_id=row["task_id"],
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
        outcome=row["outcome"],
        error_message=row["error_message"],
        response_text=row["response_text"],
    )


class TaskStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_db()
        recovered = self.reset_running_to_pending()
        if recovered:
            logger.warning("TaskStore: reset %d stuck 'running' tasks to 'pending' after restart", recovered)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_INIT_SQL)
            # Migration: add end_at column to existing databases
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN end_at TEXT")
            except Exception:
                pass  # column already exists

    def reset_running_to_pending(self) -> int:
        """Reset any tasks stuck in 'running' state to 'pending' on startup.

        Called once in __init__ to recover tasks that were interrupted by a process crash.
        Increments retry_count since the in-progress run was lost.
        """
        now = _iso(datetime.now(timezone.utc))
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = 'pending', retry_count = retry_count + 1, updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Task CRUD
    # ------------------------------------------------------------------

    def save_task(self, task: Task) -> None:
        """Insert or replace a task."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, name, prompt, task_type, run_at, cron_expr, timezone,
                     status, retry_count, max_retries, last_run_at, next_run_at,
                     end_at, timeout_seconds, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    prompt=excluded.prompt,
                    task_type=excluded.task_type,
                    run_at=excluded.run_at,
                    cron_expr=excluded.cron_expr,
                    timezone=excluded.timezone,
                    status=excluded.status,
                    retry_count=excluded.retry_count,
                    max_retries=excluded.max_retries,
                    last_run_at=excluded.last_run_at,
                    next_run_at=excluded.next_run_at,
                    end_at=excluded.end_at,
                    timeout_seconds=excluded.timeout_seconds,
                    updated_at=excluded.updated_at
                """,
                (
                    task.id, task.name, task.prompt, task.task_type.value,
                    _iso(task.run_at), task.cron_expr, task.timezone,
                    task.status.value, task.retry_count, task.max_retries,
                    _iso(task.last_run_at), _iso(task.next_run_at),
                    _iso(task.end_at), task.timeout_seconds,
                    _iso(task.created_at), _iso(task.updated_at),
                ),
            )

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(self, status: Optional[str] = None) -> list[Task]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY next_run_at ASC NULLS LAST",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY next_run_at ASC NULLS LAST"
                ).fetchall()
        return [_row_to_task(r) for r in rows]

    def get_due_tasks(self, now_utc: datetime) -> list[Task]:
        """Return pending tasks whose next_run_at is at or before now_utc."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'pending' AND next_run_at <= ?",
                (_iso(now_utc),),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def update_status(self, task_id: str, status: str, updated_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, _iso(updated_at), task_id),
            )

    def update_after_run(
        self,
        task_id: str,
        status: str,
        last_run_at: datetime,
        next_run_at: Optional[datetime],
        retry_count: int,
    ) -> None:
        now = _iso(datetime.now(timezone.utc))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, last_run_at = ?, next_run_at = ?,
                    retry_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, _iso(last_run_at), _iso(next_run_at), retry_count, now, task_id),
            )

    def delete_task(self, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Execution log
    # ------------------------------------------------------------------

    def log_execution(self, log: ExecutionLog) -> int:
        """Insert a new execution log entry and return the new row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO execution_log (task_id, started_at, finished_at, outcome, error_message, response_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    log.task_id, _iso(log.started_at), _iso(log.finished_at),
                    log.outcome, log.error_message, log.response_text,
                ),
            )
            return cur.lastrowid

    def finish_execution(
        self,
        log_id: int,
        finished_at: datetime,
        outcome: str,
        error_message: Optional[str],
        response_text: Optional[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE execution_log
                SET finished_at = ?, outcome = ?, error_message = ?, response_text = ?
                WHERE id = ?
                """,
                (_iso(finished_at), outcome, error_message, response_text, log_id),
            )

    def get_execution_history(self, task_id: str, limit: int = 10) -> list[ExecutionLog]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM execution_log
                WHERE task_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [_row_to_log(r) for r in rows]

    def prune_execution_log(self, task_id: str, keep: int = 20) -> None:
        """Keep only the most recent `keep` execution log rows for a task."""
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM execution_log
                WHERE task_id = ?
                  AND id NOT IN (
                      SELECT id FROM execution_log
                      WHERE task_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (task_id, task_id, keep),
            )
