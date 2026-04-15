"""
Data models for the Task Scheduler module.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskType(str, Enum):
    ONCE = "once"           # fires exactly once at run_at
    RECURRING = "recurring" # fires on cron_expr schedule


class TaskStatus(str, Enum):
    PENDING = "pending"     # waiting to fire
    RUNNING = "running"     # currently executing
    COMPLETED = "completed" # once-off finished, or manually cancelled
    FAILED = "failed"       # exhausted max_retries
    DISABLED = "disabled"   # recurring task paused by user


@dataclass
class Task:
    id: str                          # UUIDv4
    name: str                        # human label, e.g. "Morning digest"
    prompt: str                      # sent verbatim to agent.process()
    task_type: TaskType
    timezone: str                    # IANA tz name, e.g. "Europe/Prague"
    status: TaskStatus
    retry_count: int                 # attempts so far
    max_retries: int                 # default 3
    timeout_seconds: int             # default 300 (5 min)
    created_at: datetime             # UTC-aware
    updated_at: datetime             # UTC-aware

    # scheduling — one of these is set depending on task_type
    run_at: Optional[datetime]       # once: UTC target datetime
    cron_expr: Optional[str]         # recurring: e.g. "0 8 * * 1-5"

    # runtime tracking
    last_run_at: Optional[datetime]  # UTC, None if never run
    next_run_at: Optional[datetime]  # UTC, pre-computed and indexed in DB

    # optional expiry for recurring tasks
    end_at: Optional[datetime]       # UTC, stop recurring after this datetime (None = never)


@dataclass
class ExecutionLog:
    id: int                          # AUTOINCREMENT PK, 0 for unsaved
    task_id: str
    started_at: datetime             # UTC-aware
    finished_at: Optional[datetime]  # UTC-aware, None if still running
    outcome: str                     # "completed" | "failed" | "timeout"
    error_message: Optional[str]     # None on success
    response_text: Optional[str]     # first 500 chars of agent response
