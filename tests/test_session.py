"""
Tests for SessionManager lifecycle and SessionStore SQLite persistence.
"""

from datetime import datetime, timedelta

import pytest

from core.session import Session, SessionManager
from core.session_store import SessionStore


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_creates_new_session():
    m = SessionManager(timeout_minutes=30)
    session = await m.get_or_create("s1", "tomas")
    assert session.session_id == "s1"
    assert session.user_id == "tomas"
    assert session.status == "active"


@pytest.mark.asyncio
async def test_session_manager_returns_same_session_on_second_call():
    m = SessionManager()
    s1 = await m.get_or_create("s1", "tomas")
    s2 = await m.get_or_create("s1", "tomas")
    assert s1 is s2


@pytest.mark.asyncio
async def test_session_manager_get_or_create_after_close_creates_fresh():
    m = SessionManager()
    await m.get_or_create("s1", "tomas")
    await m.close_session("s1")
    # After close, a new session should be created
    session = await m.get_or_create("s1", "tomas")
    assert session.status == "active"
    assert session.messages == []


@pytest.mark.asyncio
async def test_session_manager_add_message_visible_in_history():
    m = SessionManager()
    await m.get_or_create("s1", "tomas")
    await m.add_message("s1", "user", "hello")
    await m.add_message("s1", "assistant", "hi there")
    history = await m.get_history("s1")
    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "hello"}
    assert history[1] == {"role": "assistant", "content": "hi there"}


@pytest.mark.asyncio
async def test_session_manager_get_history_empty_for_unknown_session():
    m = SessionManager()
    history = await m.get_history("no-such-session")
    assert history == []


@pytest.mark.asyncio
async def test_session_manager_close_session_clears_messages():
    m = SessionManager()
    await m.get_or_create("s1", "tomas")
    await m.add_message("s1", "user", "hello")
    await m.close_session("s1")
    # get_session returns the closed session object (status=closed)
    session = m.get_session("s1")
    assert session.status == "closed"
    assert session.messages == []


@pytest.mark.asyncio
async def test_session_manager_get_history_returns_empty_for_closed():
    m = SessionManager()
    await m.get_or_create("s1", "tomas")
    await m.add_message("s1", "user", "hello")
    await m.close_session("s1")
    history = await m.get_history("s1")
    assert history == []


@pytest.mark.asyncio
async def test_session_manager_cleanup_expired_closes_old_sessions():
    m = SessionManager(timeout_minutes=30)
    await m.get_or_create("s1", "tomas")
    # Simulate session being inactive for longer than timeout
    session = m.get_session("s1")
    session.last_activity = datetime.utcnow() - timedelta(minutes=60)
    await m.cleanup_expired()
    assert m.get_session("s1").status == "closed"


@pytest.mark.asyncio
async def test_session_manager_cleanup_does_not_close_active_session():
    m = SessionManager(timeout_minutes=30)
    await m.get_or_create("s1", "tomas")
    # Session just created — should not be expired
    await m.cleanup_expired()
    assert m.get_session("s1").status == "active"


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


def test_session_store_save_and_load(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    now = datetime.utcnow()
    store.save_session("s1", "tomas", now, now)
    store.save_message("s1", "user", "hello")
    store.save_message("s1", "assistant", "hi")

    loaded = store.load_all_active()
    assert len(loaded) == 1
    entry = loaded[0]
    assert entry["session_id"] == "s1"
    assert entry["user_id"] == "tomas"
    assert len(entry["messages"]) == 2
    assert entry["messages"][0]["role"] == "user"
    assert entry["messages"][0]["content"] == "hello"


def test_session_store_mark_closed_removes_messages(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    now = datetime.utcnow()
    store.save_session("s1", "tomas", now, now)
    store.save_message("s1", "user", "hello")
    store.mark_closed("s1")

    # Closed session should not appear in load_all_active
    loaded = store.load_all_active()
    assert all(e["session_id"] != "s1" for e in loaded)


def test_session_store_multiple_sessions_isolated(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    now = datetime.utcnow()
    store.save_session("s1", "tomas", now, now)
    store.save_session("s2", "tomas", now, now)
    store.save_message("s1", "user", "msg-s1")
    store.save_message("s2", "user", "msg-s2")

    loaded = {e["session_id"]: e for e in store.load_all_active()}
    assert loaded["s1"]["messages"][0]["content"] == "msg-s1"
    assert loaded["s2"]["messages"][0]["content"] == "msg-s2"


def test_session_store_save_is_idempotent(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    now = datetime.utcnow()
    store.save_session("s1", "tomas", now, now)
    store.save_session("s1", "tomas", now, now)  # second save = update

    loaded = store.load_all_active()
    assert len(loaded) == 1


def test_session_store_messages_preserve_order(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    now = datetime.utcnow()
    store.save_session("s1", "tomas", now, now)
    for i in range(5):
        store.save_message("s1", "user", f"msg{i}")

    loaded = store.load_all_active()
    contents = [m["content"] for m in loaded[0]["messages"]]
    assert contents == [f"msg{i}" for i in range(5)]
