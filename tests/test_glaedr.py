"""Unit testy pro agents/glaedr.py a tools/subagent_tools.py."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.base import SubagentResult
from agents.glaedr import Glaedr, GLAEDR_TOOL_WHITELIST
from tools.subagent_tools import make_memory_dive_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_registry(extra_tools: list[str] | None = None):
    """Vrátí mock ToolRegistry s vault_read, vault_search a volitelnými extras."""
    schemas = [
        {"name": "vault_read", "description": "Read vault file", "input_schema": {}},
        {"name": "vault_search", "description": "Search vault", "input_schema": {}},
    ]
    for t in (extra_tools or []):
        schemas.append({"name": t, "description": t, "input_schema": {}})

    registry = MagicMock()
    registry.get_schemas.return_value = schemas
    registry.execute = AsyncMock(return_value="tool_result")
    return registry


def _make_glaedr(mock_claude=None, mock_memory=None, mock_registry=None):
    if mock_claude is None:
        mock_claude = AsyncMock()
        mock_claude.complete = AsyncMock(return_value="## Brief\nTest brief.\n\n## Confidence\nhigh")
    if mock_memory is None:
        mock_memory = AsyncMock()
        mock_memory.search = AsyncMock(return_value=[])
    if mock_registry is None:
        mock_registry = _make_mock_registry()

    vault_manager = MagicMock()

    return Glaedr(
        claude_client=mock_claude,
        memory_client=mock_memory,
        vault_manager=vault_manager,
        tool_registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# test 1: scoped tools whitelist
# ---------------------------------------------------------------------------


def test_glaedr_scoped_tools_only():
    """Glaedr dostane z registru pouze vault_read a vault_search — nic jiného."""
    registry = _make_mock_registry(extra_tools=["vault_write", "web_search", "ha_call_service"])
    glaedr = _make_glaedr(mock_registry=registry)

    tool_names = {t["name"] for t in glaedr.scoped_tools}
    assert tool_names == GLAEDR_TOOL_WHITELIST
    assert "vault_write" not in tool_names
    assert "web_search" not in tool_names
    assert len(glaedr.scoped_tools) == 2


# ---------------------------------------------------------------------------
# test 2: retrieve success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glaedr_retrieve_success():
    """Retrieve s funkční pamětí i Claude vrátí success=True a neprázdné summary."""
    mock_memory = AsyncMock()
    mock_memory.search = AsyncMock(return_value=["paměť 1: projekt Apollo", "paměť 2: cache"])

    mock_claude = AsyncMock()
    brief = "## Brief\nProjekt Apollo používal TTL-based cache invalidation.\n\n## Confidence\nhigh"
    mock_claude.complete = AsyncMock(return_value=brief)

    glaedr = _make_glaedr(mock_claude=mock_claude, mock_memory=mock_memory)
    result = await glaedr.retrieve("cache invalidation Apollo")

    assert result.success is True
    assert result.summary != ""
    assert "trace_id" in result.metadata
    assert "latency_ms" in result.metadata


# ---------------------------------------------------------------------------
# test 3: memory failure → subagent pokračuje
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glaedr_retrieve_handles_memory_failure():
    """Pokud memory.search selže, Glaedr stále běží — jen bez initial context."""
    mock_memory = AsyncMock()
    mock_memory.search = AsyncMock(side_effect=RuntimeError("Mem0 connection error"))

    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(return_value="## Brief\nNezjistil jsem nic relevantního.\n\n## Confidence\nlow")

    glaedr = _make_glaedr(mock_claude=mock_claude, mock_memory=mock_memory)
    result = await glaedr.retrieve("cokoliv")

    assert result.success is True
    assert result.summary != ""


# ---------------------------------------------------------------------------
# test 4: Claude failure → success=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glaedr_retrieve_handles_claude_failure():
    """Pokud Claude selže, retrieve vrátí success=False s popisem chyby."""
    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(side_effect=RuntimeError("API timeout"))

    glaedr = _make_glaedr(mock_claude=mock_claude)
    result = await glaedr.retrieve("cokoliv")

    assert result.success is False
    assert result.error is not None
    assert "API timeout" in result.error
    assert result.summary == ""


# ---------------------------------------------------------------------------
# test 5: memory_dive tool — success passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_dive_tool_success():
    """make_memory_dive_tool vrátí callable; při úspěchu Glaedra vrátí dict success=True."""
    mock_glaedr = AsyncMock()
    mock_glaedr.retrieve = AsyncMock(
        return_value=SubagentResult(
            success=True,
            summary="## Brief\nNašel jsem relevantní záznamy.",
            metadata={"latency_ms": 42, "trace_id": "abc"},
        )
    )

    memory_dive = make_memory_dive_tool(mock_glaedr)
    result = await memory_dive("Apollo projekt cache")

    assert result["success"] is True
    assert result["summary"] != ""
    assert "metadata" in result
    assert "error" not in result


# ---------------------------------------------------------------------------
# test 6: memory_dive tool — error passthrough (ne výjimka)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_dive_tool_error_passthrough():
    """Pokud Glaedr selže, tool vrátí error dict — ne výjimku."""
    mock_glaedr = AsyncMock()
    mock_glaedr.retrieve = AsyncMock(
        return_value=SubagentResult(
            success=False,
            summary="",
            error="Claude API timeout",
            metadata={"latency_ms": 5000, "trace_id": "xyz"},
        )
    )

    memory_dive = make_memory_dive_tool(mock_glaedr)
    result = await memory_dive("cokoliv")

    assert result["success"] is False
    assert "error" in result
    assert "Claude API timeout" in result["error"]
    assert "metadata" in result
