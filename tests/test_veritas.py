"""Unit testy pro agents/veritas.py a tools/subagent_tools.py (deep_research)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.base import SubagentResult
from agents.veritas import Veritas, VERITAS_TOOL_WHITELIST
from tools.subagent_tools import make_deep_research_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_registry(extra_tools: list[str] | None = None):
    """Vrátí mock ToolRegistry s web_search, vault_read, vault_search a volitelnými extras."""
    base_tools = ["web_search", "vault_read", "vault_search"]
    all_names = base_tools + list(extra_tools or [])
    schemas_by_name = {
        name: {"name": name, "description": name, "input_schema": {}}
        for name in all_names
    }

    registry = MagicMock()
    registry.get_schemas.return_value = list(schemas_by_name.values())
    registry.get_schema.side_effect = lambda name: schemas_by_name.get(name)
    registry.has_tool.side_effect = lambda name: name in schemas_by_name
    registry.execute = AsyncMock(return_value="tool_result")
    return registry


def _make_veritas(mock_claude=None, mock_memory=None, mock_registry=None):
    if mock_claude is None:
        mock_claude = AsyncMock()
        mock_claude.complete = AsyncMock(return_value="## Brief\nTest brief.\n\n## Confidence\nhigh")
    if mock_memory is None:
        mock_memory = AsyncMock()
        mock_memory.search = AsyncMock(return_value=[])
    if mock_registry is None:
        mock_registry = _make_mock_registry()

    vault_manager = MagicMock()

    return Veritas(
        claude_client=mock_claude,
        memory_client=mock_memory,
        vault_manager=vault_manager,
        tool_registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# test 1: scoped tools whitelist
# ---------------------------------------------------------------------------


def test_veritas_scoped_tools_only():
    """Veritas dostane z registru pouze web_search, vault_read a vault_search — nic jiného."""
    registry = _make_mock_registry(extra_tools=["vault_write", "ha_call_service", "send_email"])
    veritas = _make_veritas(mock_registry=registry)

    tool_names = {t["name"] for t in veritas.scoped_tools}
    assert tool_names == VERITAS_TOOL_WHITELIST
    assert "vault_write" not in tool_names
    assert "ha_call_service" not in tool_names
    assert "send_email" not in tool_names
    assert len(veritas.scoped_tools) == 3


# ---------------------------------------------------------------------------
# test 2: research success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_veritas_research_success():
    """Research s funkční pamětí i Claude vrátí success=True a neprázdné summary."""
    mock_memory = AsyncMock()
    mock_memory.search = AsyncMock(return_value=["context 1: RAG pipeline", "context 2: paměť"])

    mock_claude = AsyncMock()
    brief = "## Brief\nNašel jsem relevantní výsledky.\n\n## Confidence\nhigh"
    mock_claude.complete = AsyncMock(return_value=brief)

    veritas = _make_veritas(mock_claude=mock_claude, mock_memory=mock_memory)
    result = await veritas.research("RAG pipelines best practices 2026")

    assert result.success is True
    assert result.summary != ""
    assert "trace_id" in result.metadata
    assert "latency_ms" in result.metadata
    assert "web_search_calls" in result.metadata
    assert "vault_calls" in result.metadata


# ---------------------------------------------------------------------------
# test 3: memory failure → subagent pokračuje
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_veritas_research_handles_memory_failure():
    """Pokud memory.search selže, Veritas stále běží — jen bez initial context."""
    mock_memory = AsyncMock()
    mock_memory.search = AsyncMock(side_effect=RuntimeError("Mem0 connection error"))

    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(
        return_value="## Brief\nNic jsem nenašel.\n\n## Confidence\nlow"
    )

    veritas = _make_veritas(mock_claude=mock_claude, mock_memory=mock_memory)
    result = await veritas.research("cokoliv")

    assert result.success is True
    assert result.summary != ""


# ---------------------------------------------------------------------------
# test 4: Claude failure → success=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_veritas_research_handles_claude_failure():
    """Pokud Claude selže, research vrátí success=False s popisem chyby."""
    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(side_effect=RuntimeError("API timeout"))

    veritas = _make_veritas(mock_claude=mock_claude)
    result = await veritas.research("cokoliv")

    assert result.success is False
    assert result.error is not None
    assert "API timeout" in result.error
    assert result.summary == ""


# ---------------------------------------------------------------------------
# test 5: scoped executor blokuje nepovolené tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_veritas_scoped_executor_blocks_unauthorized_tool():
    """Scoped executor odmítne nástroj mimo whitelist — vrátí error string, nevyhodí výjimku."""
    captured = {}

    async def mock_complete(system, messages, max_tokens=None, tools=None, tool_executor=None):
        captured["executor"] = tool_executor
        return "## Brief\nhotovo"

    mock_claude = AsyncMock()
    mock_claude.complete = mock_complete

    veritas = _make_veritas(mock_claude=mock_claude)
    await veritas.research("test topic")

    executor = captured["executor"]
    assert executor is not None

    # Nepovolený tool vrátí error string — nevyhodí výjimku
    result = await executor("vault_write", {"path": "x.md", "content": "y"})
    assert "not available to Veritas" in result
    assert "vault_write" in result

    # Povolený tool prochází normálně
    ok = await executor("web_search", {"query": "test"})
    assert ok == "tool_result"


# ---------------------------------------------------------------------------
# test 6: deep_research tool — success passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_research_tool_success():
    """make_deep_research_tool vrátí callable; při úspěchu Veritase vrátí dict success=True."""
    mock_veritas = AsyncMock()
    mock_veritas.research = AsyncMock(
        return_value=SubagentResult(
            success=True,
            summary="## Brief\nNašel jsem výsledky.",
            metadata={
                "latency_ms": 150,
                "trace_id": "abc",
                "web_search_calls": 3,
                "vault_calls": 1,
            },
        )
    )

    deep_research = make_deep_research_tool(mock_veritas)
    result = await deep_research("RAG pipelines 2026")

    assert result["success"] is True
    assert result["summary"] != ""
    assert "metadata" in result
    assert "error" not in result


# ---------------------------------------------------------------------------
# test 7: deep_research tool — error passthrough (ne výjimka)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_research_tool_error_passthrough():
    """Pokud Veritas selže, tool vrátí error dict — ne výjimku."""
    mock_veritas = AsyncMock()
    mock_veritas.research = AsyncMock(
        return_value=SubagentResult(
            success=False,
            summary="",
            error="Claude API timeout",
            metadata={"latency_ms": 5000, "trace_id": "xyz"},
        )
    )

    deep_research = make_deep_research_tool(mock_veritas)
    result = await deep_research("cokoliv")

    assert result["success"] is False
    assert "error" in result
    assert "Claude API timeout" in result["error"]
    assert "metadata" in result
