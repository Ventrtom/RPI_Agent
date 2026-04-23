"""
Task Scheduler daemon — runs as a background asyncio task inside the main process.

Poll interval: 60 seconds (no API calls while waiting).
On each tick: fetch due tasks from DB → execute each concurrently via agent.process().
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from scheduler.models import ExecutionLog, Task, TaskStatus, TaskType
from scheduler.store import TaskStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60  # seconds


def compute_next_run(cron_expr: str, after_utc: datetime, tz: ZoneInfo | str) -> datetime:
    """Return the next UTC datetime when cron_expr fires after after_utc.

    The cron expression is interpreted in local wall-clock time (tz),
    so e.g. '0 8 * * *' means 08:00 Prague time regardless of DST.
    tz may be a ZoneInfo instance or an IANA timezone string.
    """
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    after_local = after_utc.astimezone(tz)
    it = croniter(cron_expr, after_local)
    next_local = it.get_next(datetime)
    # next_local may be naive if croniter returns naive dt — make it aware
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(timezone.utc)


class TaskScheduler:
    """Background asyncio scheduler that executes due tasks via the agent."""

    def __init__(
        self,
        store: TaskStore,
        agent,  # core.agent.Agent — imported at runtime to avoid circular imports
        user_id: str,
        timezone_name: str = "Europe/Prague",
        poll_interval: int = _POLL_INTERVAL,
    ) -> None:
        self._store = store
        self._agent = agent
        self._user_id = user_id
        self._tz = ZoneInfo(timezone_name)
        self._poll_interval = poll_interval
        self._running_ids: set[str] = set()  # in-memory duplicate guard
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the scheduler background loop. Call once after the event loop is running."""
        self._task = asyncio.get_event_loop().create_task(self._loop())
        logger.info("TaskScheduler started (poll_interval=%ds, tz=%s)", self._poll_interval, self._tz)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._poll()
            except Exception:
                logger.exception("TaskScheduler: unexpected error in poll cycle")

    async def _poll(self) -> None:
        now_utc = datetime.now(timezone.utc)
        due_tasks = await asyncio.to_thread(self._store.get_due_tasks, now_utc)

        for task in due_tasks:
            if task.id in self._running_ids:
                logger.debug("TaskScheduler: task %s already running, skipping", task.id)
                continue
            asyncio.create_task(self._execute(task))

    async def _execute(self, task: Task) -> None:
        self._running_ids.add(task.id)
        started_at = datetime.now(timezone.utc)

        logger.info("TaskScheduler: starting task '%s' (%s)", task.name, task.id)

        # Mark running in DB
        await asyncio.to_thread(self._store.update_status, task.id, "running", started_at)

        # Create initial execution log row
        log = ExecutionLog(
            id=0,
            task_id=task.id,
            started_at=started_at,
            finished_at=None,
            outcome="failed",
            error_message=None,
            response_text=None,
        )
        log_id = await asyncio.to_thread(self._store.log_execution, log)

        response_text: str | None = None
        outcome = "failed"
        error_msg: str | None = None

        try:
            # Use a per-run session so history never accumulates across runs
            session_id = f"scheduler_{task.id}_{uuid.uuid4().hex[:8]}"
            response_text = await asyncio.wait_for(
                self._agent.process(task.prompt, session_id, self._user_id, is_scheduled=True),
                timeout=task.timeout_seconds,
            )
            outcome = "completed"
            logger.info("TaskScheduler: task '%s' completed successfully", task.name)

        except asyncio.TimeoutError:
            outcome = "timeout"
            error_msg = f"Timed out after {task.timeout_seconds}s"
            logger.warning("TaskScheduler: task '%s' timed out", task.name)

        except Exception as exc:
            outcome = "failed"
            error_msg = str(exc)
            logger.exception("TaskScheduler: task '%s' raised an exception", task.name)

        finally:
            self._running_ids.discard(task.id)

        finished_at = datetime.now(timezone.utc)

        # Persist execution result
        await asyncio.to_thread(
            self._store.finish_execution,
            log_id,
            finished_at,
            outcome,
            error_msg,
            (response_text or "")[:500] if response_text else error_msg,
        )

        # Prune old log rows (SD card wear mitigation)
        await asyncio.to_thread(self._store.prune_execution_log, task.id, 20)

        # Determine new task state
        new_retry_count = task.retry_count + (0 if outcome == "completed" else 1)
        next_run_at: datetime | None = None

        if outcome == "completed":
            if task.task_type == TaskType.RECURRING:
                new_retry_count = 0  # reset per-run; failures within this run don't count against the next
                next_run_at = compute_next_run(task.cron_expr, finished_at, self._tz)
                if task.end_at and next_run_at >= task.end_at:
                    # Next occurrence is at or past the expiry — stop the task
                    next_run_at = None
                    new_status = TaskStatus.COMPLETED.value
                    logger.info(
                        "TaskScheduler: recurring task '%s' reached end_at, marking completed",
                        task.name,
                    )
                else:
                    new_status = TaskStatus.PENDING.value
            else:
                new_status = TaskStatus.COMPLETED.value
        else:
            # failed or timeout — check end_at before scheduling a retry
            past_end = task.end_at and finished_at >= task.end_at
            if past_end:
                # Already past the expiry — don't retry, just stop
                new_status = TaskStatus.COMPLETED.value
                logger.info(
                    "TaskScheduler: recurring task '%s' failed after end_at, marking completed",
                    task.name,
                )
            elif new_retry_count < task.max_retries:
                backoff_seconds = 60 * (2 ** (new_retry_count - 1))  # 60s, 120s, 240s
                retry_at = finished_at + timedelta(seconds=backoff_seconds)
                # Don't schedule the retry past end_at
                if task.end_at and retry_at >= task.end_at:
                    new_status = TaskStatus.COMPLETED.value
                    logger.info(
                        "TaskScheduler: task '%s' retry would exceed end_at, stopping",
                        task.name,
                    )
                else:
                    next_run_at = retry_at
                    new_status = TaskStatus.PENDING.value
                    logger.info(
                        "TaskScheduler: task '%s' will retry in %ds (attempt %d/%d)",
                        task.name, backoff_seconds, new_retry_count, task.max_retries,
                    )
            else:
                new_status = TaskStatus.FAILED.value
                logger.error(
                    "TaskScheduler: task '%s' permanently failed after %d attempts",
                    task.name, new_retry_count,
                )

        await asyncio.to_thread(
            self._store.update_after_run,
            task.id,
            new_status,
            finished_at,
            next_run_at,
            new_retry_count,
        )
