"""
Tool get_observability_data — zpřístupní Prime její vlastní operační data.

Prime volá tento tool když uživatel ptá na její výkon, historii nebo feedback.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_reader = None


def init_observability_tools(reader: object) -> None:
    global _reader
    _reader = reader


GET_OBSERVABILITY_DATA_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["stats", "delegations", "feedback", "reflections", "errors"],
            "description": (
                "What kind of data to retrieve. "
                "'stats' returns aggregated metrics; others return individual records."
            ),
        },
        "since": {
            "type": "string",
            "description": (
                "ISO timestamp or natural period: 'today', 'yesterday', "
                "'this_week', 'last_7_days'. Default: 'last_7_days'."
            ),
        },
        "subagent": {
            "type": "string",
            "enum": ["glaedr", "veritas", "aeterna"],
            "description": "Optional filter for delegation-related scopes.",
        },
        "limit": {
            "type": "integer",
            "description": "Max records to return (default 20, max 100).",
        },
    },
    "required": ["scope"],
}


def _parse_since(since: str) -> datetime:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Prague"))
    now = datetime.now(tz)
    s = since.strip().lower()
    if s == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s == "yesterday":
        d = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return d
    if s == "this_week":
        days_since_monday = now.weekday()
        return (now - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if s == "last_7_days":
        return now - timedelta(days=7)
    # pokus o ISO parse
    try:
        dt = datetime.fromisoformat(since)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        # fallback na last_7_days
        logger.warning("Nelze parsovat since='%s', používám last_7_days", since)
        return now - timedelta(days=7)


async def get_observability_data(
    scope: str,
    since: str = "last_7_days",
    subagent: str | None = None,
    limit: int = 20,
) -> dict:
    """
    Retrieve observability data — telemetry, delegations, feedback, reflections, errors.

    Use this when the user asks about your recent behavior, performance, or history:
    - "Jak ti to šlo tento týden?"
    - "Byly nějaké chyby?"
    - "Jaké mám poznámky k tvému fungování?"
    - "Kolikrát jsi delegovala na Glaedra?"

    Scope options:
    - stats: aggregated metrics (session count, delegation success rates, avg latency)
    - delegations: individual delegation events
    - feedback: user's recorded notes
    - reflections: your past self-reflections
    - errors: recent errors

    Returns structured data. Synthesize it into a natural language answer for the user.
    """
    if _reader is None:
        return {"error": "ObservabilityReader not initialised"}

    limit = min(max(1, limit), 100)
    since_dt = _parse_since(since)

    if scope == "stats":
        return _reader.get_session_stats(since=since_dt)

    if scope == "delegations":
        events = _reader.get_recent_events(
            event_types=["delegation"], since=since_dt, limit=limit
        )
        if subagent:
            events = [e for e in events if e.get("subagent") == subagent]
        stats = _reader.get_delegation_stats(subagent=subagent, since=since_dt)
        return {"events": events, "summary": stats}

    if scope == "feedback":
        return {"records": _reader.get_feedback(since=since_dt, limit=limit)}

    if scope == "reflections":
        return {"records": _reader.get_reflections(since=since_dt, limit=limit)}

    if scope == "errors":
        events = _reader.get_recent_events(
            event_types=["error"], since=since_dt, limit=limit
        )
        return {"events": events}

    return {"error": f"Neznámý scope: {scope}"}
