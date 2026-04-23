"""
TelemetryLogger — append-only log delegací, tool callů a session událostí.

Exportuje také _current_session_id (ContextVar), který Agent.process() nastavuje
před každým toolem, takže subagenti a ToolRegistry mohou session_id číst
bez změn svých signatur.
"""
import asyncio
import json
import logging
import os
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")

_MB = 1024 * 1024


def _now_iso() -> str:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Prague"))
    return datetime.now(tz).isoformat()


class TelemetryLogger:
    def __init__(
        self,
        log_path: Path = Path("data/observability/telemetry.jsonl"),
        max_size_mb: float = 10.0,
    ) -> None:
        self._path = Path(log_path)
        self._max_bytes = int(max_size_mb * _MB)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def log_event(self, event_type: str, **kwargs) -> None:
        record = {"timestamp": _now_iso(), "event_type": event_type, **kwargs}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        async with self._lock:
            try:
                await asyncio.to_thread(self._write, line)
            except Exception:
                logger.warning("Telemetry zápis selhal (event_type=%s)", event_type)

    def _write(self, line: str) -> None:
        if self._path.exists() and self._path.stat().st_size >= self._max_bytes:
            backup = self._path.with_suffix(".jsonl.1")
            self._path.rename(backup)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)

    async def log_delegation(
        self,
        subagent: str,
        method: str,
        task: str,
        result: object,  # SubagentResult — no import to avoid circular
        session_id: str,
    ) -> None:
        meta = getattr(result, "metadata", {}) or {}
        await self.log_event(
            "delegation",
            session_id=session_id,
            subagent=subagent,
            method=method,
            task_preview=task[:200],
            success=getattr(result, "success", False),
            latency_ms=meta.get("latency_ms"),
            error=getattr(result, "error", None),
            metadata={
                "tool_calls_count": meta.get("tool_calls_count"),
                "iterations": meta.get("iterations"),
            },
        )

    async def log_session_start(
        self, session_id: str, user_id: str, interface: str
    ) -> None:
        await self.log_event(
            "session_start",
            session_id=session_id,
            user_id=user_id,
            interface=interface,
        )

    async def log_session_end(self, session_id: str, stats: dict) -> None:
        await self.log_event("session_end", session_id=session_id, **stats)

    async def log_tool_call(
        self, tool_name: str, success: bool, latency_ms: int, session_id: str
    ) -> None:
        await self.log_event(
            "tool_call",
            session_id=session_id,
            tool_name=tool_name,
            success=success,
            latency_ms=latency_ms,
        )

    async def log_error(
        self, where: str, error_type: str, message: str, session_id: str
    ) -> None:
        await self.log_event(
            "error",
            session_id=session_id,
            where=where,
            error_type=error_type,
            message=message,
        )
