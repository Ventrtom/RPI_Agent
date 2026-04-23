"""Unit testy pro agents/base.py — SubagentResult a BaseSubagent."""
import pytest
from unittest.mock import AsyncMock

from agents.base import BaseSubagent, SubagentResult


# ---------------------------------------------------------------------------
# SubagentResult
# ---------------------------------------------------------------------------


def test_subagent_result_defaults():
    result = SubagentResult(success=True, summary="odpověď")
    assert result.success is True
    assert result.summary == "odpověď"
    assert result.data is None
    assert result.error is None
    assert result.clarification_needed is None
    assert result.metadata == {}


def test_subagent_result_error():
    result = SubagentResult(success=False, summary="", error="něco selhalo")
    assert result.success is False
    assert result.summary == ""
    assert result.error == "něco selhalo"
    assert result.data is None
    assert result.clarification_needed is None


# ---------------------------------------------------------------------------
# BaseSubagent.run()
# ---------------------------------------------------------------------------


def _make_agent(mock_claude, max_iterations=10, tool_executor=None):
    return BaseSubagent(
        claude_client=mock_claude,
        scoped_tools=[],
        tool_executor=tool_executor or AsyncMock(return_value="tool_result"),
        name="test-agent",
        system_prompt="Jsi testovací subagent.",
        max_iterations=max_iterations,
    )


@pytest.mark.asyncio
async def test_base_subagent_run_success():
    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(return_value="finální odpověď")

    agent = _make_agent(mock_claude)
    result = await agent.run("Jaký je dnešní den?")

    assert result.success is True
    assert result.summary == "finální odpověď"
    assert "latency_ms" in result.metadata
    assert "iterations" in result.metadata
    assert "tool_calls_count" in result.metadata
    assert "trace_id" in result.metadata


@pytest.mark.asyncio
async def test_base_subagent_run_exception():
    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(side_effect=RuntimeError("API timeout"))

    agent = _make_agent(mock_claude)
    result = await agent.run("Úkol, který selže")

    assert result.success is False
    assert "API timeout" in result.error
    assert result.summary == ""
    assert "trace_id" in result.metadata


@pytest.mark.asyncio
async def test_base_subagent_respects_max_iterations():
    """Ověří, že limited_executor vyhodí RuntimeError po překročení max_iterations."""
    captured: dict = {}

    async def mock_complete(system, messages, max_tokens=None, tools=None, tool_executor=None):
        captured["tool_executor"] = tool_executor
        return "výsledek"

    mock_claude = AsyncMock()
    mock_claude.complete = mock_complete

    agent = _make_agent(mock_claude, max_iterations=2)
    await agent.run("testovací úkol")

    limited_executor = captured.get("tool_executor")
    assert limited_executor is not None, "tool_executor nebyl předán do complete()"

    # První dvě volání musí projít
    await limited_executor("tool_a", {})
    await limited_executor("tool_b", {})

    # Třetí volání překročí limit
    with pytest.raises(RuntimeError, match="Iteration limit"):
        await limited_executor("tool_c", {})
