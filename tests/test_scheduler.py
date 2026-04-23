"""
Tests for the scheduler subsystem:
- compute_next_run (timezone-aware cron scheduling)
- TaskStore (SQLite CRUD, get_due_tasks, reset on restart)
- TaskScheduler._poll (due task execution, duplicate guard)
- Curator auto-registration dedup
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from scheduler.daemon import TaskScheduler, compute_next_run
from scheduler.models import ExecutionLog, Task, TaskStatus, TaskType
from scheduler.store import TaskStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRAGUE = ZoneInfo("Europe/Prague")


def _task(**kwargs) -> Task:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=str(uuid.uuid4()),
        name="test-task",
        prompt="hello",
        task_type=TaskType.RECURRING,
        timezone="Europe/Prague",
        status=TaskStatus.PENDING,
        retry_count=0,
        max_retries=3,
        timeout_seconds=300,
        created_at=now,
        updated_at=now,
        run_at=None,
        cron_expr="0 2 * * 0",
        last_run_at=None,
        next_run_at=now + timedelta(hours=1),
        end_at=None,
    )
    defaults.update(kwargs)
    return Task(**defaults)


@pytest.fixture
def store(tmp_path):
    return TaskStore(str(tmp_path / "tasks.db"))


# ---------------------------------------------------------------------------
# compute_next_run
# ---------------------------------------------------------------------------


def test_compute_next_run_returns_utc():
    # '0 8 * * *' fired from Jan 1 (CET = UTC+1) → 08:00 Praha = 07:00 UTC
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    result = compute_next_run("0 8 * * *", after, _PRAGUE)
    assert result.tzinfo == timezone.utc
    assert result.hour == 7  # 08:00 CET = 07:00 UTC


def test_compute_next_run_summer_time():
    # '0 8 * * *' fired from April (CEST = UTC+2) → 08:00 Praha = 06:00 UTC
    after = datetime(2026, 4, 20, 0, 0, tzinfo=timezone.utc)
    result = compute_next_run("0 8 * * *", after, _PRAGUE)
    assert result.tzinfo == timezone.utc
    assert result.hour == 6  # 08:00 CEST = 06:00 UTC


def test_compute_next_run_next_occurrence_is_future():
    # Result must always be strictly after after_utc
    after = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    result = compute_next_run("0 2 * * 0", after, _PRAGUE)
    assert result > after


def test_compute_next_run_weekly_curator_cron():
    # '0 2 * * 0' from Monday 2026-04-20 → next Sunday 2026-04-26 00:00 UTC (CEST = UTC+2)
    after = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    result = compute_next_run("0 2 * * 0", after, _PRAGUE)
    assert result.weekday() == 6  # Sunday
    assert result.hour == 0       # 02:00 CEST = 00:00 UTC


def test_compute_next_run_accepts_string_timezone():
    # main.py passes scheduler_tz as a plain string — must not raise TypeError
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    result = compute_next_run("0 8 * * *", after, "Europe/Prague")
    assert result.tzinfo == timezone.utc
    assert result.hour == 7  # same as ZoneInfo("Europe/Prague") version


def test_compute_next_run_string_and_zoneinfo_equivalent():
    # Passing a string or a ZoneInfo for the same timezone must give identical output
    after = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    result_str = compute_next_run("0 2 * * 0", after, "Europe/Prague")
    result_zi = compute_next_run("0 2 * * 0", after, _PRAGUE)
    assert result_str == result_zi


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------


def test_store_save_and_get(store):
    task = _task()
    store.save_task(task)
    loaded = store.get_task(task.id)
    assert loaded is not None
    assert loaded.id == task.id
    assert loaded.name == task.name
    assert loaded.cron_expr == task.cron_expr
    assert loaded.status == TaskStatus.PENDING


def test_store_get_nonexistent(store):
    assert store.get_task("does-not-exist") is None


def test_store_datetime_roundtrip(store):
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    task = _task(next_run_at=now, created_at=now, updated_at=now)
    store.save_task(task)
    loaded = store.get_task(task.id)
    assert loaded.next_run_at == now
    assert loaded.created_at == now


def test_store_get_due_tasks_returns_overdue(store):
    now = datetime.now(timezone.utc)
    overdue = _task(next_run_at=now - timedelta(seconds=1))
    future = _task(next_run_at=now + timedelta(hours=1))
    store.save_task(overdue)
    store.save_task(future)
    due = store.get_due_tasks(now)
    assert len(due) == 1
    assert due[0].id == overdue.id


def test_store_get_due_tasks_excludes_non_pending(store):
    now = datetime.now(timezone.utc)
    running = _task(status=TaskStatus.RUNNING, next_run_at=now - timedelta(seconds=1))
    disabled = _task(status=TaskStatus.DISABLED, next_run_at=now - timedelta(seconds=1))
    store.save_task(running)
    store.save_task(disabled)
    due = store.get_due_tasks(now)
    assert len(due) == 0


def test_store_reset_running_to_pending_on_restart(tmp_path):
    db = str(tmp_path / "tasks.db")
    store1 = TaskStore(db)
    task = _task(status=TaskStatus.PENDING)
    store1.save_task(task)
    store1.update_status(task.id, "running", datetime.now(timezone.utc))

    # Simulate process restart — new TaskStore instance on same DB
    store2 = TaskStore(db)
    loaded = store2.get_task(task.id)
    assert loaded.status == TaskStatus.PENDING
    assert loaded.retry_count == 1  # incremented because the run was lost


def test_store_reset_running_returns_count(tmp_path):
    db = str(tmp_path / "tasks.db")
    store1 = TaskStore(db)
    for _ in range(3):
        t = _task(status=TaskStatus.PENDING)
        store1.save_task(t)
        store1.update_status(t.id, "running", datetime.now(timezone.utc))

    store2 = TaskStore(db)
    # All three should now be pending
    pending = [t for t in store2.list_tasks() if t.status == TaskStatus.PENDING]
    assert len(pending) == 3


def test_store_delete_task(store):
    task = _task()
    store.save_task(task)
    assert store.delete_task(task.id) is True
    assert store.get_task(task.id) is None
    assert store.delete_task(task.id) is False  # second delete returns False


def test_store_list_tasks_no_filter(store):
    for _ in range(3):
        store.save_task(_task())
    tasks = store.list_tasks()
    assert len(tasks) == 3


def test_store_list_tasks_status_filter(store):
    pending = _task(status=TaskStatus.PENDING)
    failed = _task(status=TaskStatus.FAILED)
    store.save_task(pending)
    store.save_task(failed)
    result = store.list_tasks(status="pending")
    assert len(result) == 1
    assert result[0].id == pending.id


def test_store_prune_execution_log(store):
    task = _task()
    store.save_task(task)
    now = datetime.now(timezone.utc)
    log_template = ExecutionLog(
        id=0, task_id=task.id, started_at=now, finished_at=None,
        outcome="failed", error_message=None, response_text=None,
    )
    for _ in range(25):
        log_id = store.log_execution(log_template)
        store.finish_execution(log_id, now, "completed", None, "ok")

    store.prune_execution_log(task.id, keep=20)
    history = store.get_execution_history(task.id, limit=100)
    assert len(history) == 20


# ---------------------------------------------------------------------------
# TaskScheduler._poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_poll_executes_due_task():
    now = datetime.now(timezone.utc)
    task = _task(id="abc", next_run_at=now - timedelta(seconds=1))

    mock_store = MagicMock()
    mock_store.get_due_tasks = MagicMock(return_value=[task])
    mock_store.update_status = MagicMock()
    mock_store.log_execution = MagicMock(return_value=1)
    mock_store.finish_execution = MagicMock()
    mock_store.prune_execution_log = MagicMock()
    mock_store.update_after_run = MagicMock()

    mock_agent = MagicMock()
    mock_agent.process = AsyncMock(return_value="ok")

    scheduler = TaskScheduler(mock_store, mock_agent, user_id="tomas",
                              timezone_name="Europe/Prague", poll_interval=9999)
    await scheduler._poll()
    await asyncio.sleep(0.05)  # let the spawned task run

    mock_agent.process.assert_called_once()
    call_kwargs = mock_agent.process.call_args
    assert call_kwargs[0][0] == task.prompt  # prompt passed correctly
    assert call_kwargs[1].get("is_scheduled") is True


@pytest.mark.asyncio
async def test_scheduler_poll_skips_already_running():
    now = datetime.now(timezone.utc)
    task = _task(id="xyz", next_run_at=now - timedelta(seconds=1))

    mock_store = MagicMock()
    mock_store.get_due_tasks = MagicMock(return_value=[task])

    mock_agent = MagicMock()
    mock_agent.process = AsyncMock(return_value="ok")

    scheduler = TaskScheduler(mock_store, mock_agent, user_id="tomas",
                              timezone_name="Europe/Prague", poll_interval=9999)
    scheduler._running_ids.add(task.id)  # simulate task already running

    await scheduler._poll()
    await asyncio.sleep(0.05)

    mock_agent.process.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_execute_clears_running_ids():
    now = datetime.now(timezone.utc)
    task = _task(id="def", next_run_at=now - timedelta(seconds=1))

    mock_store = MagicMock()
    mock_store.update_status = MagicMock()
    mock_store.log_execution = MagicMock(return_value=1)
    mock_store.finish_execution = MagicMock()
    mock_store.prune_execution_log = MagicMock()
    mock_store.update_after_run = MagicMock()

    mock_agent = MagicMock()
    mock_agent.process = AsyncMock(return_value="ok")

    scheduler = TaskScheduler(mock_store, mock_agent, user_id="tomas",
                              timezone_name="Europe/Prague", poll_interval=9999)

    await scheduler._execute(task)

    assert task.id not in scheduler._running_ids


# ---------------------------------------------------------------------------
# Curator auto-registration dedup
# ---------------------------------------------------------------------------


def _register_curator_if_needed(
    task_store: TaskStore, cron: str = "0 2 * * 0", tz_name: str = "Europe/Prague"
) -> None:
    """Mirrors the main.py startup logic for _curator_weekly registration.

    Intentionally passes tz_name as a plain string (not ZoneInfo) to match
    the main.py pattern: scheduler_tz = os.getenv(...) → string passed to
    compute_next_run. This ensures the test catches the TypeError that bit us.
    """
    existing = task_store.list_tasks()
    if any(t.name == "_curator_weekly" for t in existing):
        return
    now = datetime.now(timezone.utc)
    next_run = compute_next_run(cron, now, tz_name)  # string, not ZoneInfo
    task_store.save_task(Task(
        id=str(uuid.uuid4()),
        name="_curator_weekly",
        prompt="Spusť Glaedr curator (scope='week', dry_run=false).",
        task_type=TaskType.RECURRING,
        timezone="Europe/Prague",
        status=TaskStatus.PENDING,
        retry_count=0,
        max_retries=3,
        timeout_seconds=600,
        created_at=now,
        updated_at=now,
        run_at=None,
        cron_expr=cron,
        last_run_at=None,
        next_run_at=next_run,
        end_at=None,
    ))


def test_curator_registered_on_first_start(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    _register_curator_if_needed(store)
    tasks = [t for t in store.list_tasks() if t.name == "_curator_weekly"]
    assert len(tasks) == 1
    assert tasks[0].cron_expr == "0 2 * * 0"
    assert tasks[0].status == TaskStatus.PENDING


def test_curator_not_duplicated_on_restart(tmp_path):
    db = str(tmp_path / "tasks.db")
    store1 = TaskStore(db)
    _register_curator_if_needed(store1)

    # Simulate process restart
    store2 = TaskStore(db)
    _register_curator_if_needed(store2)

    tasks = [t for t in store2.list_tasks() if t.name == "_curator_weekly"]
    assert len(tasks) == 1  # exactly one, no duplicate


def test_curator_next_run_is_in_future(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    _register_curator_if_needed(store)
    task = next(t for t in store.list_tasks() if t.name == "_curator_weekly")
    assert task.next_run_at > datetime.now(timezone.utc)
