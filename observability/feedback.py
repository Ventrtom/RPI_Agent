"""
FeedbackRecorder — ukládá manuální feedback uživatele a self-reflection od Prime.

Feedback: data/observability/feedback/{YYYY-MM-DD}.jsonl
Reflections: data/observability/reflections/{YYYY-MM-DD}.jsonl
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _today_str() -> str:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Prague"))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _now_iso() -> str:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Prague"))
    return datetime.now(tz).isoformat()


def _truncate_messages(messages: list[dict], last_n: int = 4, max_chars: int = 200) -> list[dict]:
    recent = messages[-last_n:] if len(messages) >= last_n else messages
    return [
        {"role": m.get("role", ""), "content": m.get("content", "")[:max_chars]}
        for m in recent
    ]


class FeedbackRecorder:
    def __init__(self, base_path: Path = Path("data/observability")) -> None:
        self._base = Path(base_path)
        self._feedback_dir = self._base / "feedback"
        self._reflections_dir = self._base / "reflections"
        self._feedback_dir.mkdir(parents=True, exist_ok=True)
        self._reflections_dir.mkdir(parents=True, exist_ok=True)

    async def record_feedback(
        self, text: str, session_id: str, last_messages: list[dict]
    ) -> str:
        record = {
            "timestamp": _now_iso(),
            "session_id": session_id,
            "text": text,
            "last_messages_preview": _truncate_messages(last_messages),
        }
        path = self._feedback_dir / f"{_today_str()}.jsonl"
        await asyncio.to_thread(self._append, path, record)
        return str(path)

    async def save_reflection(
        self,
        text: str,
        session_id: str,
        stats: dict,
        trigger: str = "manual",
    ) -> str:
        record = {
            "timestamp": _now_iso(),
            "session_id": session_id,
            "trigger": trigger,
            "reflection": text,
            "session_stats": stats,
        }
        path = self._reflections_dir / f"{_today_str()}.jsonl"
        await asyncio.to_thread(self._append, path, record)
        return str(path)

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
