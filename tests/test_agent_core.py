"""
Tests for Agent.process() — core Prime orchestration.

Covers: basic response, memory failure resilience, Claude failure fallback,
session persistence, voice/scheduled context flags, confirmed executor.
"""

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from core.agent import Agent
from tools.confirmation import DANGEROUS_TOOLS

_DANGEROUS_TOOL = next(iter(DANGEROUS_TOOLS))


def _build_agent(
    *,
    claude_response="Hello from Claude",
    memory_side_effect=None,
    history=None,
    reasoning=None,
    gate=None,
):
    memory = MagicMock()
    memory.search = AsyncMock(
        side_effect=memory_side_effect if memory_side_effect else None,
        return_value=[],
    )
    memory.add = AsyncMock()
    memory.get_all = AsyncMock(return_value=[])

    claude = MagicMock()
    claude.complete = AsyncMock(return_value=claude_response)

    sessions = MagicMock()
    sessions.get_or_create = AsyncMock()
    sessions.get_history = AsyncMock(return_value=history or [])
    sessions.add_message = AsyncMock()

    agent = Agent(
        memory_client=memory,
        claude_client=claude,
        session_manager=sessions,
        tool_registry=None,
        reasoning_engine=reasoning,
        confirmation_gate=gate,
    )
    return agent, memory, claude, sessions


# ---------------------------------------------------------------------------
# Basic response flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_returns_claude_response():
    agent, _, _, _ = _build_agent(claude_response="Ahoj!")
    result = await agent.process("hello", "s1", "tomas")
    assert result == "Ahoj!"


@pytest.mark.asyncio
async def test_process_calls_session_get_or_create():
    agent, _, _, sessions = _build_agent()
    await agent.process("msg", "s1", "tomas")
    sessions.get_or_create.assert_called_once_with("s1", "tomas")


@pytest.mark.asyncio
async def test_process_saves_user_and_assistant_messages():
    agent, _, _, sessions = _build_agent(claude_response="Response")
    await agent.process("user msg", "s1", "tomas")
    assert sessions.add_message.call_count == 2
    sessions.add_message.assert_any_call("s1", "user", "user msg")
    sessions.add_message.assert_any_call("s1", "assistant", "Response")


@pytest.mark.asyncio
async def test_process_includes_session_history():
    history = [
        {"role": "user", "content": "prev user"},
        {"role": "assistant", "content": "prev reply"},
    ]
    agent, _, claude, _ = _build_agent(history=history)
    await agent.process("new msg", "s1", "tomas")
    messages = claude.complete.call_args[1]["messages"]
    assert messages[0]["content"] == "prev user"
    assert messages[-1]["content"] == "new msg"


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_memory_failure_continues():
    agent, _, claude, _ = _build_agent(
        memory_side_effect=RuntimeError("Mem0 down"),
        claude_response="Still works",
    )
    result = await agent.process("hello", "s1", "tomas")
    assert result == "Still works"
    claude.complete.assert_called_once()


@pytest.mark.asyncio
async def test_process_claude_failure_returns_error_string():
    agent, _, claude, _ = _build_agent()
    claude.complete = AsyncMock(side_effect=RuntimeError("API error"))
    result = await agent.process("hello", "s1", "tomas")
    assert isinstance(result, str)
    assert len(result) > 0  # returns a graceful error message, does not raise


# ---------------------------------------------------------------------------
# Context flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_voice_adds_suffix_to_message():
    agent, _, claude, _ = _build_agent()
    await agent.process("hello", "s1", "tomas", is_voice=True)
    messages = claude.complete.call_args[1]["messages"]
    user_content = messages[-1]["content"]
    # Voice suffix discourages markdown formatting
    assert "markdown" in user_content.lower() or "bullet" in user_content.lower()


@pytest.mark.asyncio
async def test_process_scheduled_passes_max_tokens():
    agent, _, claude, _ = _build_agent()
    await agent.process("task", "s1", "tomas", is_scheduled=True)
    assert claude.complete.call_args[1]["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_process_normal_passes_no_max_tokens():
    agent, _, claude, _ = _build_agent()
    await agent.process("task", "s1", "tomas", is_scheduled=False)
    assert claude.complete.call_args[1]["max_tokens"] is None


# ---------------------------------------------------------------------------
# Confirmed executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_executor_passes_safe_tool_through():
    raw = AsyncMock(return_value={"result": "ok"})
    agent, _, _, _ = _build_agent()
    executor = agent._make_confirmed_executor(raw, is_scheduled=False)
    result = await executor("safe_tool", {"arg": "val"})
    assert result == {"result": "ok"}
    raw.assert_called_once_with("safe_tool", {"arg": "val"})


@pytest.mark.asyncio
async def test_confirmed_executor_blocks_dangerous_in_scheduled():
    raw = AsyncMock(return_value={"ok": True})
    agent, _, _, _ = _build_agent()
    executor = agent._make_confirmed_executor(raw, is_scheduled=True)
    result = await executor(_DANGEROUS_TOOL, {})
    assert "error" in result
    raw.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_executor_no_gate_returns_error_for_dangerous():
    raw = AsyncMock(return_value={"ok": True})
    agent, _, _, _ = _build_agent(gate=None)
    executor = agent._make_confirmed_executor(raw, is_scheduled=False)
    result = await executor(_DANGEROUS_TOOL, {})
    assert "error" in result
    raw.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_executor_gate_approved_executes_tool():
    raw = AsyncMock(return_value={"ok": True})
    gate = MagicMock()
    gate.request = AsyncMock(return_value=True)
    agent, _, _, _ = _build_agent(gate=gate)
    executor = agent._make_confirmed_executor(raw, is_scheduled=False)
    result = await executor(_DANGEROUS_TOOL, {"a": 1})
    assert result == {"ok": True}
    raw.assert_called_once_with(_DANGEROUS_TOOL, {"a": 1})


@pytest.mark.asyncio
async def test_confirmed_executor_gate_denied_returns_error():
    raw = AsyncMock(return_value={"ok": True})
    gate = MagicMock()
    gate.request = AsyncMock(return_value=False)
    agent, _, _, _ = _build_agent(gate=gate)
    executor = agent._make_confirmed_executor(raw, is_scheduled=False)
    result = await executor(_DANGEROUS_TOOL, {})
    assert "error" in result
    raw.assert_not_called()
