"""
ObservabilityReader — čte a agreguje observability data pro get_observability_data tool.

Všechny read operace jsou synchronní (tool volán ve vlákně nebo přímo z async context).
Corrupted JSONL řádky jsou přeskočeny s warning logem.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("TZ", "Europe/Prague"))


def _parse_ts(ts_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Corrupted JSONL line skipped in %s", path)


class ObservabilityReader:
    def __init__(self, base_path: Path = Path("data/observability")) -> None:
        self._base = Path(base_path)

    def get_recent_events(
        self,
        event_types: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        results = []
        for record in _iter_jsonl(self._base / "telemetry.jsonl"):
            if event_types and record.get("event_type") not in event_types:
                continue
            if since:
                ts = _parse_ts(record.get("timestamp", ""))
                if ts and ts < since:
                    continue
            results.append(record)
        return results[-limit:]

    def get_session_stats(
        self, since: datetime, until: datetime | None = None
    ) -> dict:
        sessions: set[str] = set()
        delegations = 0
        errors = 0
        latencies_by_agent: dict[str, list[int]] = {}

        for record in _iter_jsonl(self._base / "telemetry.jsonl"):
            ts = _parse_ts(record.get("timestamp", ""))
            if ts is None or ts < since:
                continue
            if until and ts > until:
                continue

            etype = record.get("event_type")
            sid = record.get("session_id", "")
            if etype == "session_start":
                sessions.add(sid)
            elif etype == "delegation":
                delegations += 1
                agent = record.get("subagent", "unknown")
                lat = record.get("latency_ms")
                if lat is not None:
                    latencies_by_agent.setdefault(agent, []).append(int(lat))
            elif etype == "error":
                errors += 1

        avg_latency = {
            agent: int(sum(lats) / len(lats))
            for agent, lats in latencies_by_agent.items()
        }
        return {
            "session_count": len(sessions),
            "delegation_count": delegations,
            "error_count": errors,
            "avg_latency_ms_per_subagent": avg_latency,
        }

    def get_feedback(self, since: datetime, limit: int = 50) -> list[dict]:
        results = []
        feedback_dir = self._base / "feedback"
        if not feedback_dir.exists():
            return []
        for path in sorted(feedback_dir.glob("*.jsonl")):
            for record in _iter_jsonl(path):
                ts = _parse_ts(record.get("timestamp", ""))
                if ts and ts >= since:
                    results.append(record)
        return results[-limit:]

    def get_reflections(self, since: datetime, limit: int = 20) -> list[dict]:
        results = []
        reflections_dir = self._base / "reflections"
        if not reflections_dir.exists():
            return []
        for path in sorted(reflections_dir.glob("*.jsonl")):
            for record in _iter_jsonl(path):
                ts = _parse_ts(record.get("timestamp", ""))
                if ts and ts >= since:
                    results.append(record)
        return results[-limit:]

    def get_delegation_stats(
        self, subagent: str | None = None, since: datetime | None = None
    ) -> dict:
        total = 0
        success = 0
        latencies: list[int] = []
        errors: list[str] = []

        for record in _iter_jsonl(self._base / "telemetry.jsonl"):
            if record.get("event_type") != "delegation":
                continue
            if subagent and record.get("subagent") != subagent:
                continue
            if since:
                ts = _parse_ts(record.get("timestamp", ""))
                if ts and ts < since:
                    continue
            total += 1
            if record.get("success"):
                success += 1
            else:
                err = record.get("error")
                if err:
                    errors.append(err)
            lat = record.get("latency_ms")
            if lat is not None:
                latencies.append(int(lat))

        return {
            "total": total,
            "success": success,
            "failed": total - success,
            "success_rate": round(success / total, 3) if total else 0.0,
            "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
            "top_errors": errors[-5:],
        }
