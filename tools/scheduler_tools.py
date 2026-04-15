"""
Scheduler tools for the agent.

Allows the agent to create, list, inspect, update, cancel, and re-enable
scheduled tasks. All DB I/O is wrapped in asyncio.to_thread() to avoid
blocking the event loop.

Initialise with init_scheduler_tools(store, timezone) before registering.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from scheduler.models import Task, TaskStatus, TaskType
from scheduler.store import TaskStore

logger = logging.getLogger(__name__)

# Module-level singletons set by init_scheduler_tools()
_store: Optional[TaskStore] = None
_tz_name: str = "Europe/Prague"

MAX_PROMPT_LEN = 2000


def init_scheduler_tools(store: TaskStore, timezone_name: str = "Europe/Prague") -> None:
    """Call once in main.py before registering scheduler tools."""
    global _store, _tz_name
    _store = store
    _tz_name = timezone_name


def _get_store() -> TaskStore:
    if _store is None:
        raise RuntimeError("Scheduler tools not initialized — call init_scheduler_tools() first")
    return _store


def _parse_local_datetime(dt_str: str, tz_name: str) -> datetime:
    """Parse an ISO-8601 datetime string (possibly naive) as local time and return UTC-aware datetime."""
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        raise ValueError(f"Invalid datetime format '{dt_str}'. Use ISO-8601, e.g. '2026-05-01T14:00:00'")
    if dt.tzinfo is None:
        # Treat as local time in the configured timezone
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            raise ValueError(f"Unknown timezone '{tz_name}'")
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _compute_next_run_for_cron(cron_expr: str, tz_name: str) -> datetime:
    """Compute next UTC run time for a cron expression starting from now."""
    from scheduler.daemon import compute_next_run
    tz = ZoneInfo(tz_name)
    return compute_next_run(cron_expr, datetime.now(timezone.utc), tz)


def _task_summary(task: Task) -> dict:
    """Return a concise dict for list views."""
    return {
        "id": task.id,
        "name": task.name,
        "type": task.task_type.value,
        "status": task.status.value,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "cron_expr": task.cron_expr,
        "run_at": task.run_at.isoformat() if task.run_at else None,
        "end_at": task.end_at.isoformat() if task.end_at else None,
    }


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SCHEDULE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Short human-readable label for the task, e.g. 'Morning digest'.",
        },
        "prompt": {
            "type": "string",
            "description": (
                "The message that will be sent to the agent when this task fires. "
                "Write it as if you were sending it as a user message, e.g. "
                "'Send me a summary of today's calendar events'."
            ),
        },
        "run_at": {
            "type": "string",
            "description": (
                "For a one-time task: ISO-8601 datetime in local (Prague) time, "
                "e.g. '2026-06-01T14:00:00'. Must be in the future."
            ),
        },
        "cron_expr": {
            "type": "string",
            "description": (
                "For a recurring task: standard 5-field cron expression in local (Prague) time, "
                "e.g. '0 8 * * 1-5' (weekdays at 08:00). "
                "Provide either run_at or cron_expr, not both."
            ),
        },
        "end_at": {
            "type": "string",
            "description": (
                "For recurring tasks only: ISO-8601 datetime (local Prague time) after which the task "
                "stops recurring automatically, e.g. '2026-05-01T18:00:00'. "
                "The task completes its last run before this time and then stops."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "How many seconds to wait for the agent before marking the task as failed (default: 300).",
        },
    },
    "required": ["name", "prompt"],
}

LIST_TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pending", "running", "completed", "failed", "disabled"],
            "description": "Filter by status. Omit to return all tasks.",
        },
    },
    "required": [],
}

GET_TASK_DETAILS_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "UUID of the task to inspect.",
        },
    },
    "required": ["task_id"],
}

CANCEL_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "UUID of the task to cancel.",
        },
    },
    "required": ["task_id"],
}

ENABLE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "UUID of the disabled recurring task to re-enable.",
        },
    },
    "required": ["task_id"],
}

UPDATE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "UUID of the task to update.",
        },
        "name": {
            "type": "string",
            "description": "New name for the task.",
        },
        "prompt": {
            "type": "string",
            "description": "New prompt text for the task.",
        },
        "cron_expr": {
            "type": "string",
            "description": "New cron expression (recurring tasks only).",
        },
        "run_at": {
            "type": "string",
            "description": "New run datetime in ISO-8601 local time (once-off tasks only).",
        },
        "end_at": {
            "type": "string",
            "description": (
                "New expiry datetime for a recurring task (ISO-8601 local Prague time). "
                "Pass an empty string to remove an existing end_at."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "New timeout in seconds.",
        },
    },
    "required": ["task_id"],
}


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

async def schedule_task(
    name: str,
    prompt: str,
    run_at: Optional[str] = None,
    cron_expr: Optional[str] = None,
    end_at: Optional[str] = None,
    timeout_seconds: int = 300,
) -> dict:
    """
    Schedule a task for the agent to execute automatically.
    Use run_at (ISO-8601 local datetime) for a one-time task,
    or cron_expr (5-field cron) for a recurring task.
    Exactly one of run_at or cron_expr must be provided.
    Returns the new task ID on success.
    """
    store = _get_store()

    # Validate input
    if run_at and cron_expr:
        return {"error": "Provide either run_at or cron_expr, not both."}
    if not run_at and not cron_expr:
        return {"error": "Provide either run_at (one-time) or cron_expr (recurring)."}
    if len(prompt) > MAX_PROMPT_LEN:
        return {"error": f"Prompt is too long ({len(prompt)} chars, max {MAX_PROMPT_LEN})."}

    now_utc = datetime.now(timezone.utc)

    if run_at:
        try:
            run_at_utc = _parse_local_datetime(run_at, _tz_name)
        except ValueError as e:
            return {"error": str(e)}
        if run_at_utc <= now_utc:
            return {"error": f"run_at must be in the future (got '{run_at}', current time is past that)."}
        task_type = TaskType.ONCE
        next_run_at = run_at_utc
        cron_expr_stored = None
        run_at_stored = run_at_utc
        end_at_utc = None  # end_at only applies to recurring tasks
    else:
        if not croniter.is_valid(cron_expr):
            return {"error": f"Invalid cron expression: '{cron_expr}'. Use standard 5-field format, e.g. '0 8 * * 1-5'."}
        try:
            next_run_at = _compute_next_run_for_cron(cron_expr, _tz_name)
        except Exception as e:
            return {"error": f"Failed to compute next run: {e}"}
        task_type = TaskType.RECURRING
        run_at_stored = None
        cron_expr_stored = cron_expr

        end_at_utc = None
        if end_at:
            try:
                end_at_utc = _parse_local_datetime(end_at, _tz_name)
            except ValueError as e:
                return {"error": str(e)}
            if end_at_utc <= now_utc:
                return {"error": "end_at must be in the future."}
            if end_at_utc <= next_run_at:
                return {"error": "end_at must be after the first scheduled run."}

    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        name=name,
        prompt=prompt,
        task_type=task_type,
        timezone=_tz_name,
        status=TaskStatus.PENDING,
        retry_count=0,
        max_retries=3,
        timeout_seconds=timeout_seconds,
        created_at=now_utc,
        updated_at=now_utc,
        run_at=run_at_stored,
        cron_expr=cron_expr_stored,
        last_run_at=None,
        next_run_at=next_run_at,
        end_at=end_at_utc,
    )

    await asyncio.to_thread(store.save_task, task)
    logger.info("Scheduled task '%s' (%s) next_run_at=%s", name, task_id, next_run_at.isoformat())

    result = {
        "success": True,
        "task_id": task_id,
        "name": name,
        "type": task_type.value,
        "next_run_at": next_run_at.isoformat(),
    }
    if end_at_utc:
        result["end_at"] = end_at_utc.isoformat()
    return result


async def list_tasks(status: Optional[str] = None) -> dict:
    """
    List all scheduled tasks, optionally filtered by status.
    status can be: pending, running, completed, failed, disabled.
    Returns a list of tasks with their next run time and current status.
    """
    store = _get_store()
    tasks = await asyncio.to_thread(store.list_tasks, status)

    if not tasks:
        msg = f"No tasks with status '{status}'" if status else "No tasks scheduled"
        return {"tasks": [], "message": msg}

    return {"tasks": [_task_summary(t) for t in tasks], "count": len(tasks)}


async def get_task_details(task_id: str) -> dict:
    """
    Return full details of a scheduled task including recent execution history.
    Shows the last 5 executions with outcomes and any error messages.
    Use this when asked what a task does, when it last ran, or why it failed.
    """
    store = _get_store()
    task = await asyncio.to_thread(store.get_task, task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found."}

    history = await asyncio.to_thread(store.get_execution_history, task_id, 5)

    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "type": task.task_type.value,
        "status": task.status.value,
        "timezone": task.timezone,
        "cron_expr": task.cron_expr,
        "run_at": task.run_at.isoformat() if task.run_at else None,
        "end_at": task.end_at.isoformat() if task.end_at else None,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "timeout_seconds": task.timeout_seconds,
        "created_at": task.created_at.isoformat(),
        "execution_history": [
            {
                "started_at": log.started_at.isoformat(),
                "finished_at": log.finished_at.isoformat() if log.finished_at else None,
                "outcome": log.outcome,
                "error": log.error_message,
                "response_preview": log.response_text,
            }
            for log in history
        ],
    }


async def cancel_task(task_id: str) -> dict:
    """
    Cancel a scheduled task.
    For once-off tasks, marks them as completed (skipped).
    For recurring tasks, sets status to 'disabled' (preserves the task for re-enabling later).
    Use this when asked to stop, cancel, pause, or remove a scheduled task.
    """
    store = _get_store()
    task = await asyncio.to_thread(store.get_task, task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found."}
    if task.status == TaskStatus.RUNNING:
        return {"error": f"Task '{task.name}' is currently running and cannot be cancelled right now."}

    now_utc = datetime.now(timezone.utc)
    new_status = TaskStatus.DISABLED if task.task_type == TaskType.RECURRING else TaskStatus.COMPLETED
    await asyncio.to_thread(store.update_status, task_id, new_status.value, now_utc)

    action = "disabled" if new_status == TaskStatus.DISABLED else "cancelled"
    return {"success": True, "message": f"Task '{task.name}' has been {action}."}


async def enable_task(task_id: str) -> dict:
    """
    Re-enable a disabled recurring task and recompute its next run time.
    Use this when asked to resume a previously paused or disabled recurring task.
    """
    store = _get_store()
    task = await asyncio.to_thread(store.get_task, task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found."}
    if task.task_type != TaskType.RECURRING:
        return {"error": f"Task '{task.name}' is a once-off task and cannot be re-enabled. Create a new task instead."}
    if task.status != TaskStatus.DISABLED:
        return {"error": f"Task '{task.name}' is not disabled (current status: {task.status.value})."}

    try:
        next_run_at = _compute_next_run_for_cron(task.cron_expr, _tz_name)
    except Exception as e:
        return {"error": f"Failed to compute next run: {e}"}

    now_utc = datetime.now(timezone.utc)
    task.status = TaskStatus.PENDING
    task.next_run_at = next_run_at
    task.updated_at = now_utc
    await asyncio.to_thread(store.save_task, task)

    return {
        "success": True,
        "message": f"Task '{task.name}' re-enabled.",
        "next_run_at": next_run_at.isoformat(),
    }


async def update_task(
    task_id: str,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    cron_expr: Optional[str] = None,
    run_at: Optional[str] = None,
    end_at: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> dict:
    """
    Modify an existing scheduled task. Only provide the fields to change.
    If cron_expr or run_at changes, next_run_at is recomputed automatically.
    Cannot change task_type (once vs recurring) after creation.
    """
    store = _get_store()
    task = await asyncio.to_thread(store.get_task, task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found."}
    if task.status == TaskStatus.RUNNING:
        return {"error": f"Task '{task.name}' is currently running. Wait until it finishes before editing."}

    now_utc = datetime.now(timezone.utc)

    if name is not None:
        task.name = name
    if prompt is not None:
        if len(prompt) > MAX_PROMPT_LEN:
            return {"error": f"Prompt too long ({len(prompt)} chars, max {MAX_PROMPT_LEN})."}
        task.prompt = prompt
    if timeout_seconds is not None:
        task.timeout_seconds = timeout_seconds

    if cron_expr is not None:
        if task.task_type != TaskType.RECURRING:
            return {"error": "Cannot set cron_expr on a once-off task."}
        if not croniter.is_valid(cron_expr):
            return {"error": f"Invalid cron expression: '{cron_expr}'."}
        task.cron_expr = cron_expr
        task.next_run_at = _compute_next_run_for_cron(cron_expr, _tz_name)

    if run_at is not None:
        if task.task_type != TaskType.ONCE:
            return {"error": "Cannot set run_at on a recurring task."}
        try:
            run_at_utc = _parse_local_datetime(run_at, _tz_name)
        except ValueError as e:
            return {"error": str(e)}
        if run_at_utc <= now_utc:
            return {"error": "run_at must be in the future."}
        task.run_at = run_at_utc
        task.next_run_at = run_at_utc
        task.status = TaskStatus.PENDING  # re-arm a completed once-off if needed

    if end_at is not None:
        if task.task_type != TaskType.RECURRING:
            return {"error": "end_at only applies to recurring tasks."}
        if end_at == "":
            task.end_at = None  # remove expiry
        else:
            try:
                end_at_utc = _parse_local_datetime(end_at, _tz_name)
            except ValueError as e:
                return {"error": str(e)}
            if end_at_utc <= now_utc:
                return {"error": "end_at must be in the future."}
            task.end_at = end_at_utc

    task.updated_at = now_utc
    await asyncio.to_thread(store.save_task, task)

    return {
        "success": True,
        "message": f"Task '{task.name}' updated.",
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
    }
