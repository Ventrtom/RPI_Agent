"""
Testy pro observability subsystém (Fáze 6).

Všechny testy používají tmp_path fixture aby neznečišťovaly data/.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import SubagentResult
from observability.feedback import FeedbackRecorder
from observability.reader import ObservabilityReader
from observability.snapshots import SessionSnapshotManager
from observability.telemetry import TelemetryLogger


# ---------------------------------------------------------------------------
# TelemetryLogger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_logger_appends_event(tmp_path):
    path = tmp_path / "tel.jsonl"
    logger = TelemetryLogger(log_path=path, max_size_mb=10)
    await logger.log_event("test_event", session_id="s1", key="val")

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "test_event"
    assert record["session_id"] == "s1"
    assert record["key"] == "val"
    assert "timestamp" in record


@pytest.mark.asyncio
async def test_telemetry_logger_appends_multiple_events(tmp_path):
    path = tmp_path / "tel.jsonl"
    logger = TelemetryLogger(log_path=path, max_size_mb=10)
    await logger.log_event("e1", session_id="s1")
    await logger.log_event("e2", session_id="s2")

    lines = [l for l in path.read_text().strip().split("\n") if l]
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "e1"
    assert json.loads(lines[1])["event_type"] == "e2"


@pytest.mark.asyncio
async def test_telemetry_logger_rotation(tmp_path):
    path = tmp_path / "tel.jsonl"
    # max_size_mb = 0 → každý zápis přes limit → okamžitá rotace
    logger = TelemetryLogger(log_path=path, max_size_mb=0.000001)
    await logger.log_event("first", session_id="s1")
    await logger.log_event("second", session_id="s1")

    backup = tmp_path / "tel.jsonl.1"
    assert backup.exists(), "Backup .jsonl.1 měl vzniknout po rotaci"
    # Aktuální soubor obsahuje poslední event
    current = json.loads(path.read_text().strip())
    assert current["event_type"] == "second"


@pytest.mark.asyncio
async def test_telemetry_log_delegation_from_subagent_result(tmp_path):
    path = tmp_path / "tel.jsonl"
    logger = TelemetryLogger(log_path=path, max_size_mb=10)
    result = SubagentResult(
        success=True,
        summary="brief",
        metadata={"latency_ms": 500, "tool_calls_count": 3, "iterations": 2},
    )
    await logger.log_delegation(
        subagent="glaedr", method="retrieve", task="Najdi X", result=result, session_id="sess1"
    )
    record = json.loads(path.read_text().strip())
    assert record["event_type"] == "delegation"
    assert record["subagent"] == "glaedr"
    assert record["method"] == "retrieve"
    assert record["success"] is True
    assert record["latency_ms"] == 500
    assert record["metadata"]["tool_calls_count"] == 3
    assert record["task_preview"] == "Najdi X"


@pytest.mark.asyncio
async def test_telemetry_log_delegation_task_preview_truncated(tmp_path):
    path = tmp_path / "tel.jsonl"
    logger = TelemetryLogger(log_path=path, max_size_mb=10)
    long_task = "X" * 300
    result = SubagentResult(success=False, summary="", error="fail")
    await logger.log_delegation("veritas", "research", long_task, result, "s1")
    record = json.loads(path.read_text().strip())
    assert len(record["task_preview"]) == 200


# ---------------------------------------------------------------------------
# FeedbackRecorder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feedback_recorder_creates_daily_file(tmp_path):
    recorder = FeedbackRecorder(base_path=tmp_path)
    path = await recorder.record_feedback("test feedback", "sess1", [])
    assert Path(path).exists()
    record = json.loads(Path(path).read_text().strip())
    assert record["text"] == "test feedback"
    assert record["session_id"] == "sess1"


@pytest.mark.asyncio
async def test_feedback_recorder_appends_multiple(tmp_path):
    recorder = FeedbackRecorder(base_path=tmp_path)
    await recorder.record_feedback("first", "s1", [])
    await recorder.record_feedback("second", "s1", [])

    feedback_files = list((tmp_path / "feedback").glob("*.jsonl"))
    assert len(feedback_files) == 1
    lines = [l for l in feedback_files[0].read_text().strip().split("\n") if l]
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_feedback_recorder_preserves_last_messages(tmp_path):
    recorder = FeedbackRecorder(base_path=tmp_path)
    messages = [
        {"role": "user", "content": "u" * 300},
        {"role": "assistant", "content": "a" * 300},
        {"role": "user", "content": "u2" * 150},
        {"role": "assistant", "content": "a2" * 150},
        {"role": "user", "content": "u3"},  # 5th message, should be excluded if last_n=4
    ]
    path = await recorder.record_feedback("fb", "s1", messages)
    record = json.loads(Path(path).read_text().strip())
    preview = record["last_messages_preview"]
    # Posledních 4 zpráv
    assert len(preview) == 4
    # Každá zpráva oříznutá na max 200 znaků
    for msg in preview:
        assert len(msg["content"]) <= 200


@pytest.mark.asyncio
async def test_feedback_recorder_saves_reflection(tmp_path):
    recorder = FeedbackRecorder(base_path=tmp_path)
    path = await recorder.save_reflection("reflexe textu", "s1", {"msg_count": 5})
    assert Path(path).exists()
    record = json.loads(Path(path).read_text().strip())
    assert record["reflection"] == "reflexe textu"
    assert record["trigger"] == "manual"
    assert record["session_stats"]["msg_count"] == 5


# ---------------------------------------------------------------------------
# SessionSnapshotManager
# ---------------------------------------------------------------------------

def _make_mock_session(session_id="s1", user_id="tomas", messages=None):
    session = MagicMock()
    session.session_id = session_id
    session.user_id = user_id
    session.messages = messages or [{"role": "user", "content": "hello"}]
    session.created_at = datetime(2026, 4, 23, 10, 0, 0)
    session.last_activity = datetime(2026, 4, 23, 10, 30, 0)
    return session


@pytest.mark.asyncio
async def test_snapshot_manager_saves_auto_snapshot(tmp_path):
    mgr = SessionSnapshotManager(base_path=tmp_path / "sessions")
    session = _make_mock_session()
    path = await mgr.save_snapshot(session, snapshot_type="auto")
    assert Path(path).exists()
    data = json.loads(Path(path).read_text())
    assert data["snapshot_type"] == "auto"
    assert data["session"]["session_id"] == "s1"
    assert len(data["session"]["messages"]) == 1


@pytest.mark.asyncio
async def test_snapshot_manager_saves_manual_with_tag(tmp_path):
    mgr = SessionSnapshotManager(base_path=tmp_path / "sessions")
    session = _make_mock_session()
    path = await mgr.save_snapshot(session, snapshot_type="manual", tag="my-tag")
    assert "my-tag" in path
    data = json.loads(Path(path).read_text())
    assert data["snapshot_type"] == "manual"
    assert data["tag"] == "my-tag"


@pytest.mark.asyncio
async def test_snapshot_sanitizes_tag(tmp_path):
    mgr = SessionSnapshotManager(base_path=tmp_path / "sessions")
    session = _make_mock_session()
    with pytest.raises(ValueError, match="Neplatný tag"):
        await mgr.save_snapshot(session, tag="invalid tag with spaces")


@pytest.mark.asyncio
async def test_snapshot_includes_telemetry_events(tmp_path):
    tel_path = tmp_path / "tel.jsonl"
    tel_logger = TelemetryLogger(log_path=tel_path, max_size_mb=10)
    await tel_logger.log_event("delegation", session_id="s1", subagent="glaedr")
    await tel_logger.log_event("delegation", session_id="OTHER", subagent="veritas")

    mgr = SessionSnapshotManager(
        base_path=tmp_path / "sessions",
        telemetry_log_path=tel_path,
    )
    session = _make_mock_session(session_id="s1")
    path = await mgr.save_snapshot(session)
    data = json.loads(Path(path).read_text())
    assert len(data["telemetry_events"]) == 1
    assert data["telemetry_events"][0]["subagent"] == "glaedr"


# ---------------------------------------------------------------------------
# ObservabilityReader
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _ts(days_ago: int = 0) -> str:
    from datetime import timedelta
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


@pytest.mark.asyncio
async def test_reader_get_recent_events(tmp_path):
    tel_path = tmp_path / "telemetry.jsonl"
    records = [
        {"event_type": "delegation", "session_id": "s1", "timestamp": _ts(1)},
        {"event_type": "tool_call", "session_id": "s1", "timestamp": _ts(0)},
        {"event_type": "error", "session_id": "s1", "timestamp": _ts(0)},
    ]
    _write_jsonl(tel_path, records)
    reader = ObservabilityReader(base_path=tmp_path)

    all_events = reader.get_recent_events()
    assert len(all_events) == 3

    filtered = reader.get_recent_events(event_types=["delegation"])
    assert len(filtered) == 1
    assert filtered[0]["event_type"] == "delegation"


@pytest.mark.asyncio
async def test_reader_get_recent_events_since_filter(tmp_path):
    tel_path = tmp_path / "telemetry.jsonl"
    from datetime import timedelta
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    _write_jsonl(tel_path, [
        {"event_type": "delegation", "timestamp": old_ts},
        {"event_type": "delegation", "timestamp": new_ts},
    ])
    reader = ObservabilityReader(base_path=tmp_path)
    since = datetime.now(timezone.utc) - timedelta(days=5)
    events = reader.get_recent_events(since=since)
    assert len(events) == 1


def test_reader_get_delegation_stats(tmp_path):
    tel_path = tmp_path / "telemetry.jsonl"
    records = [
        {"event_type": "delegation", "session_id": "s1", "subagent": "glaedr",
         "success": True, "latency_ms": 200, "timestamp": _ts(0)},
        {"event_type": "delegation", "session_id": "s1", "subagent": "glaedr",
         "success": False, "error": "timeout", "latency_ms": 5000, "timestamp": _ts(0)},
        {"event_type": "delegation", "session_id": "s1", "subagent": "veritas",
         "success": True, "latency_ms": 3000, "timestamp": _ts(0)},
    ]
    _write_jsonl(tel_path, records)
    reader = ObservabilityReader(base_path=tmp_path)

    stats = reader.get_delegation_stats(subagent="glaedr")
    assert stats["total"] == 2
    assert stats["success"] == 1
    assert stats["failed"] == 1
    assert stats["success_rate"] == 0.5
    assert stats["avg_latency_ms"] == 2600
    assert "timeout" in stats["top_errors"]


def test_reader_get_session_stats(tmp_path):
    tel_path = tmp_path / "telemetry.jsonl"
    records = [
        {"event_type": "session_start", "session_id": "s1", "timestamp": _ts(0)},
        {"event_type": "delegation", "session_id": "s1", "subagent": "glaedr",
         "latency_ms": 100, "timestamp": _ts(0)},
        {"event_type": "error", "session_id": "s1", "timestamp": _ts(0)},
    ]
    _write_jsonl(tel_path, records)
    reader = ObservabilityReader(base_path=tmp_path)
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=1)
    stats = reader.get_session_stats(since=since)
    assert stats["session_count"] == 1
    assert stats["delegation_count"] == 1
    assert stats["error_count"] == 1


def test_reader_skips_corrupted_lines(tmp_path):
    tel_path = tmp_path / "telemetry.jsonl"
    with open(tel_path, "w") as f:
        f.write('{"event_type": "delegation", "timestamp": "' + _ts(0) + '"}\n')
        f.write("NOT JSON {{{}\n")
        f.write('{"event_type": "error", "timestamp": "' + _ts(0) + '"}\n')
    reader = ObservabilityReader(base_path=tmp_path)
    events = reader.get_recent_events()
    assert len(events) == 2  # corrupted line je přeskočen


def test_reader_get_feedback(tmp_path):
    from datetime import timedelta
    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = [
        {"timestamp": _ts(0), "session_id": "s1", "text": "good job"},
        {"timestamp": _ts(0), "session_id": "s1", "text": "needs work"},
    ]
    _write_jsonl(feedback_dir / f"{today}.jsonl", records)

    reader = ObservabilityReader(base_path=tmp_path)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    feedback = reader.get_feedback(since=since)
    assert len(feedback) == 2


# ---------------------------------------------------------------------------
# Observability tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_observability_tool_returns_stats(tmp_path):
    from tools.observability_tools import get_observability_data, init_observability_tools

    reader = ObservabilityReader(base_path=tmp_path)
    init_observability_tools(reader)

    result = await get_observability_data(scope="stats", since="last_7_days")
    assert "session_count" in result
    assert "delegation_count" in result
    assert "error_count" in result


@pytest.mark.asyncio
async def test_observability_tool_returns_feedback(tmp_path):
    from tools.observability_tools import get_observability_data, init_observability_tools

    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _write_jsonl(
        feedback_dir / f"{today}.jsonl",
        [{"timestamp": _ts(0), "session_id": "s1", "text": "test fb"}],
    )

    reader = ObservabilityReader(base_path=tmp_path)
    init_observability_tools(reader)

    result = await get_observability_data(scope="feedback", since="today")
    assert "records" in result
    assert len(result["records"]) == 1
    assert result["records"][0]["text"] == "test fb"


@pytest.mark.asyncio
async def test_observability_tool_parses_since_natural(tmp_path):
    from tools.observability_tools import _parse_since

    today_start = _parse_since("today")
    assert today_start.hour == 0
    assert today_start.minute == 0

    last_7 = _parse_since("last_7_days")
    from datetime import timedelta
    now = datetime.now(last_7.tzinfo)
    diff = now - last_7
    assert 6 <= diff.days <= 7

    yesterday = _parse_since("yesterday")
    assert (now - yesterday).days >= 1


@pytest.mark.asyncio
async def test_observability_tool_unknown_scope(tmp_path):
    from tools.observability_tools import get_observability_data, init_observability_tools

    reader = ObservabilityReader(base_path=tmp_path)
    init_observability_tools(reader)
    result = await get_observability_data(scope="nonexistent_scope")
    assert "error" in result


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

def test_cli_help_lists_all_commands():
    from interfaces.cli import _HELP_TEXT
    for cmd in ["/start", "/clear", "/memory", "/feedback", "/self-reflect", "/snapshot", "/help", "/quit"]:
        assert cmd in _HELP_TEXT, f"Command {cmd} chybí v _HELP_TEXT"


def test_telegram_help_lists_all_commands():
    from interfaces.telegram_bot import _HELP_TEXT
    for cmd in ["/start", "/newsession", "/memory", "/feedback", "/self-reflect", "/snapshot", "/help"]:
        assert cmd in _HELP_TEXT, f"Command {cmd} chybí v telegram _HELP_TEXT"


# ---------------------------------------------------------------------------
# Command handlery — unit testy s mock contextem
# ---------------------------------------------------------------------------

def _make_telegram_context(session_id="telegram_123", observability=None, session_manager=None):
    update = MagicMock()
    update.effective_chat.id = 123
    update.message.reply_text = AsyncMock()
    update.message.chat.send_action = AsyncMock()

    context = MagicMock()
    context.args = []
    context.bot_data = {
        "agent": MagicMock(),
        "session_manager": session_manager or MagicMock(),
        "observability": observability,
    }
    return update, context


@pytest.mark.asyncio
async def test_feedback_command_records_with_context(tmp_path):
    from interfaces.telegram_bot import _cmd_feedback

    recorder = FeedbackRecorder(base_path=tmp_path)
    bundle = MagicMock()
    bundle.feedback = recorder

    session_manager = MagicMock()
    session_manager.get_history = AsyncMock(return_value=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ])

    update, context = _make_telegram_context(
        observability=bundle, session_manager=session_manager
    )
    context.args = ["Odpoved", "byla", "moc", "strucna"]

    await _cmd_feedback(update, context)

    # Ověř, že reply_text byl zavolán s cestou k souboru
    reply_call = update.message.reply_text.call_args[0][0]
    assert "Feedback uložen" in reply_call

    # Ověř, že soubor vznikl
    feedback_files = list((tmp_path / "feedback").glob("*.jsonl"))
    assert len(feedback_files) == 1


@pytest.mark.asyncio
async def test_feedback_command_empty_text(tmp_path):
    from interfaces.telegram_bot import _cmd_feedback

    bundle = MagicMock()
    update, context = _make_telegram_context(observability=bundle)
    context.args = []

    await _cmd_feedback(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "Použití" in reply


@pytest.mark.asyncio
async def test_self_reflect_command_generates_reflection(tmp_path):
    from interfaces.telegram_bot import _cmd_self_reflect

    recorder = FeedbackRecorder(base_path=tmp_path)
    bundle = MagicMock()
    bundle.feedback = recorder

    agent_mock = MagicMock()
    agent_mock.generate_self_reflection = AsyncMock(return_value="Reflexe textu...")

    update, context = _make_telegram_context(observability=bundle)
    context.bot_data["agent"] = agent_mock

    await _cmd_self_reflect(update, context)

    # Ověř, že reflexe byla uložena
    reflection_files = list((tmp_path / "reflections").glob("*.jsonl"))
    assert len(reflection_files) == 1

    # Ověř, že odpověď obsahuje preview reflexe
    reply = update.message.reply_text.call_args[0][0]
    assert "Reflexe textu" in reply


@pytest.mark.asyncio
async def test_snapshot_command_manual_with_tag(tmp_path):
    from interfaces.telegram_bot import _cmd_snapshot

    mgr = SessionSnapshotManager(base_path=tmp_path / "sessions")
    bundle = MagicMock()
    bundle.snapshots = mgr

    mock_session = _make_mock_session()
    session_manager = MagicMock()
    session_manager.get_session = MagicMock(return_value=mock_session)

    update, context = _make_telegram_context(
        observability=bundle, session_manager=session_manager
    )
    context.args = ["my-tag"]

    await _cmd_snapshot(update, context)

    reply = update.message.reply_text.call_args[0][0]
    assert "Snapshot uložen" in reply
    assert "my-tag" in reply


@pytest.mark.asyncio
async def test_snapshot_command_invalid_tag(tmp_path):
    from interfaces.telegram_bot import _cmd_snapshot

    mgr = SessionSnapshotManager(base_path=tmp_path / "sessions")
    bundle = MagicMock()
    bundle.snapshots = mgr

    mock_session = _make_mock_session()
    session_manager = MagicMock()
    session_manager.get_session = MagicMock(return_value=mock_session)

    update, context = _make_telegram_context(
        observability=bundle, session_manager=session_manager
    )
    context.args = ["invalid tag with spaces"]

    await _cmd_snapshot(update, context)

    reply = update.message.reply_text.call_args[0][0]
    assert "Neplatný tag" in reply
