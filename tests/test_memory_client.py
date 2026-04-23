"""
Tests for MemoryClient — memory/client.py

Primary goal: catch mem0ai API signature changes before they reach production.
The mem0ai 2.0.0 migration (user_id= → filters=, limit= → top_k=) broke
memory silently because exceptions were swallowed. These tests verify exact
call signatures so a future breaking upgrade fails loudly here.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from memory.client import MemoryClient

_USER_ID = "tomas"


@pytest.fixture
def mock_mem0():
    return MagicMock()


@pytest.fixture
def client(mock_mem0, tmp_path):
    """MemoryClient with Memory.from_config patched — no HuggingFace loading."""
    with patch("memory.client.Memory") as MockMemoryClass:
        MockMemoryClass.from_config.return_value = mock_mem0
        c = MemoryClient(user_id=_USER_ID, chroma_path=str(tmp_path))
    return c


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_uses_filters_not_user_id(client, mock_mem0):
    """Catches mem0ai 2.0.0 change: user_id= kwarg → filters={"user_id": ...}."""
    mock_mem0.search.return_value = {"results": []}
    await client.search("coffee preferences")
    mock_mem0.search.assert_called_once_with(
        "coffee preferences",
        filters={"user_id": _USER_ID},
        top_k=10,
    )


@pytest.mark.asyncio
async def test_search_uses_top_k_not_limit(client, mock_mem0):
    """Catches mem0ai 2.0.0 change: limit= → top_k=."""
    mock_mem0.search.return_value = {"results": []}
    await client.search("query", limit=5)
    _, kwargs = mock_mem0.search.call_args
    assert "top_k" in kwargs, "must pass top_k= (not limit=)"
    assert "limit" not in kwargs, "old limit= must not be used"
    assert kwargs["top_k"] == 5


@pytest.mark.asyncio
async def test_search_returns_memory_strings(client, mock_mem0):
    mock_mem0.search.return_value = {
        "results": [
            {"id": "1", "memory": "likes coffee", "score": 0.9},
            {"id": "2", "memory": "owns a raspberry pi", "score": 0.8},
        ]
    }
    result = await client.search("hardware")
    assert result == ["likes coffee", "owns a raspberry pi"]


@pytest.mark.asyncio
async def test_search_returns_empty_on_empty_results(client, mock_mem0):
    mock_mem0.search.return_value = {"results": []}
    assert await client.search("anything") == []


@pytest.mark.asyncio
async def test_search_returns_empty_on_missing_results_key(client, mock_mem0):
    mock_mem0.search.return_value = {}
    assert await client.search("query") == []


@pytest.mark.asyncio
async def test_search_returns_empty_on_exception(client, mock_mem0):
    """search() must never raise — any failure returns [] so agent continues."""
    mock_mem0.search.side_effect = ValueError("ChromaDB gone")
    result = await client.search("query")
    assert result == []


# ---------------------------------------------------------------------------
# add()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_passes_user_id(client, mock_mem0):
    """add() still uses user_id= directly in mem0ai 2.0.0 (not filters=)."""
    messages = [{"role": "user", "content": "I like tea"}]
    await client.add(messages)
    mock_mem0.add.assert_called_once_with(messages, user_id=_USER_ID)


@pytest.mark.asyncio
async def test_add_does_not_raise_on_exception(client, mock_mem0):
    """add() runs in background — exceptions must not crash the agent."""
    mock_mem0.add.side_effect = RuntimeError("ChromaDB write failed")
    await client.add([{"role": "user", "content": "test"}])  # must not raise


@pytest.mark.asyncio
async def test_add_passes_full_message_list(client, mock_mem0):
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    await client.add(messages)
    args, _ = mock_mem0.add.call_args
    assert args[0] == messages


# ---------------------------------------------------------------------------
# get_all()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_uses_filters_not_user_id(client, mock_mem0):
    """Catches mem0ai 2.0.0 change in get_all."""
    mock_mem0.get_all.return_value = {"results": []}
    await client.get_all()
    mock_mem0.get_all.assert_called_once_with(filters={"user_id": _USER_ID})


@pytest.mark.asyncio
async def test_get_all_returns_memory_strings(client, mock_mem0):
    mock_mem0.get_all.return_value = {
        "results": [
            {"memory": "speaks Czech"},
            {"memory": "works in tech"},
        ]
    }
    result = await client.get_all()
    assert result == ["speaks Czech", "works in tech"]


@pytest.mark.asyncio
async def test_get_all_returns_empty_on_empty_results(client, mock_mem0):
    mock_mem0.get_all.return_value = {"results": []}
    assert await client.get_all() == []


# ---------------------------------------------------------------------------
# get_stats()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_stats_uses_filters_not_user_id(client, mock_mem0):
    """Catches mem0ai 2.0.0 change in get_stats."""
    mock_mem0.get_all.return_value = {"results": []}
    await client.get_stats()
    mock_mem0.get_all.assert_called_once_with(filters={"user_id": _USER_ID})


@pytest.mark.asyncio
async def test_get_stats_returns_correct_count(client, mock_mem0):
    mock_mem0.get_all.return_value = {
        "results": [{"memory": "a"}, {"memory": "b"}, {"memory": "c"}]
    }
    stats = await client.get_stats()
    assert stats["memory_count"] == 3


@pytest.mark.asyncio
async def test_get_stats_db_size_is_non_negative_float(client, mock_mem0):
    mock_mem0.get_all.return_value = {"results": []}
    stats = await client.get_stats()
    assert isinstance(stats["db_size_mb"], float)
    assert stats["db_size_mb"] >= 0.0


@pytest.mark.asyncio
async def test_get_stats_counts_files_on_disk(client, mock_mem0, tmp_path):
    (tmp_path / "segment.bin").write_bytes(b"x" * 10240)  # 10 KB → rounds to 0.01 MB
    mock_mem0.get_all.return_value = {"results": []}
    stats = await client.get_stats()
    assert stats["db_size_mb"] > 0.0
