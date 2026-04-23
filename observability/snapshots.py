"""
SessionSnapshotManager — ukládá kompletní session snapshot (zprávy + telemetry events).

Auto snapshot: volán z SessionManager.close_session() před smazáním zpráv.
Manual snapshot: volán z /snapshot příkazu.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^[a-zA-Z0-9-]{1,50}$")


def _now_iso() -> str:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Prague"))
    return datetime.now(tz).isoformat()


def _safe_filename_ts() -> str:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Prague"))
    return datetime.now(tz).strftime("%Y-%m-%dT%H-%M-%S")


class SessionSnapshotManager:
    def __init__(
        self,
        base_path: Path = Path("data/observability/sessions"),
        telemetry_log_path: Path | None = None,
    ) -> None:
        self._base = Path(base_path)
        self._telemetry_path = Path(telemetry_log_path) if telemetry_log_path else None
        self._base.mkdir(parents=True, exist_ok=True)

    async def save_snapshot(
        self,
        session: object,  # core.session.Session — no import to avoid circular
        snapshot_type: str = "auto",
        tag: str | None = None,
    ) -> str:
        if tag is not None and not _TAG_RE.match(tag):
            raise ValueError(
                f"Neplatný tag '{tag}' — povoleny pouze [a-zA-Z0-9-], max 50 znaků"
            )

        session_id = getattr(session, "session_id", "unknown")
        user_id = getattr(session, "user_id", "")
        messages = list(getattr(session, "messages", []))
        created_at = getattr(session, "created_at", None)
        last_activity = getattr(session, "last_activity", None)

        events = await asyncio.to_thread(self._load_events_for_session, session_id)

        label = tag or session_id[:8]
        filename = f"{_safe_filename_ts()}_{label}.json"
        path = self._base / filename

        snapshot = {
            "snapshot_type": snapshot_type,
            "tag": tag,
            "saved_at": _now_iso(),
            "session": {
                "session_id": session_id,
                "user_id": user_id,
                "started_at": created_at.isoformat() if created_at else None,
                "ended_at": last_activity.isoformat() if last_activity else None,
                "message_count": len(messages),
                "messages": messages,
            },
            "telemetry_events": events,
        }

        await asyncio.to_thread(self._write_json, path, snapshot)
        logger.info("Snapshot uložen: %s (type=%s, events=%d)", path, snapshot_type, len(events))
        return str(path)

    def _load_events_for_session(self, session_id: str) -> list[dict]:
        if not self._telemetry_path or not self._telemetry_path.exists():
            return []
        events = []
        with open(self._telemetry_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("session_id") == session_id:
                        events.append(record)
                except json.JSONDecodeError:
                    logger.warning("Corrupted telemetry line skipped during snapshot")
        return events

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
