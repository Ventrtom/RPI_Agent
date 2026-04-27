"""Unit testy pro agents/aeterna.py a tools/subagent_tools.py (plan_task, review_my_schedule)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.aeterna import Aeterna, AETERNA_TOOL_WHITELIST
from agents.base import SubagentResult
from tools.subagent_tools import make_plan_task_tool, make_review_schedule_tool


# ---------------------------------------------------------------------------
# Konstanty
# ---------------------------------------------------------------------------

_SAMPLE_SCHEDULE_OUTPUT = """\
## Action taken
Created a weekly scheduled task for Apollo project review.

## Details
- Object type: scheduled_task
- ID: 42
- Time: Every Friday 10:00 CEST (until 2026-05-31)
- Description: Weekly review of project Apollo

## Assumptions made
- "end of May" interpreted as 2026-05-31

## Status
success"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_registry(extra_tools: list[str] | None = None):
    """Vrátí mock ToolRegistry s celým Aeternin whitelistem + volitelnými extras."""
    all_names = list(AETERNA_TOOL_WHITELIST) + list(extra_tools or [])
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


def _make_aeterna(
    mock_claude=None,
    mock_registry=None,
    confirmation_gate=None,
    is_scheduled_context: bool = False,
):
    if mock_claude is None:
        mock_claude = AsyncMock()
        mock_claude.complete = AsyncMock(return_value=_SAMPLE_SCHEDULE_OUTPUT)
    if mock_registry is None:
        mock_registry = _make_mock_registry()

    return Aeterna(
        claude_client=mock_claude,
        tool_registry=mock_registry,
        confirmation_gate=confirmation_gate,
        is_scheduled_context=is_scheduled_context,
    )


# ---------------------------------------------------------------------------
# test 1: scoped tools whitelist
# ---------------------------------------------------------------------------


def test_aeterna_scoped_tools_only():
    """Aeterna dostane z registru pouze povolené tools — zakázané jsou odebrány."""
    # send_email je nyní v AETERNA_TOOL_WHITELIST (Aeterna ji smí posílat v scheduled kontextu)
    forbidden = ["ha_call_service", "web_search", "vault_write", "vault_read"]
    registry = _make_mock_registry(extra_tools=forbidden)
    aeterna = _make_aeterna(mock_registry=registry)

    tool_names = {t["name"] for t in aeterna.scoped_tools}
    assert tool_names == AETERNA_TOOL_WHITELIST

    for name in forbidden:
        assert name not in tool_names

    assert len(aeterna.scoped_tools) == len(AETERNA_TOOL_WHITELIST)


# ---------------------------------------------------------------------------
# test 2: scoped executor blokuje zakázané tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_no_email_or_ha_or_web():
    """Scoped executor odmítne zakázané tools — vrátí error string, nevyhodí výjimku."""
    captured: dict = {}

    async def mock_complete(system, messages, max_tokens=None, tools=None, tool_executor=None):
        captured["executor"] = tool_executor
        return _SAMPLE_SCHEDULE_OUTPUT

    mock_claude = AsyncMock()
    mock_claude.complete = mock_complete

    aeterna = _make_aeterna(mock_claude=mock_claude)
    await aeterna.schedule("schedule something")

    executor = captured["executor"]
    assert executor is not None

    # send_email je nyní v AETERNA_TOOL_WHITELIST — testujeme ostatní zakázané tools
    for forbidden in ("ha_call_service", "web_search", "vault_write"):
        result = await executor(forbidden, {})
        assert isinstance(result, str)
        assert "not available to Aeterna" in result
        assert forbidden in result


# ---------------------------------------------------------------------------
# test 3: schedule success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_schedule_success():
    """Schedule s funkční Claude instancí vrátí success=True a metadata."""
    aeterna = _make_aeterna()
    result = await aeterna.schedule("schedule weekly Apollo review every Friday 10am")

    assert result.success is True
    assert result.summary != ""
    assert "trace_id" in result.metadata
    assert "latency_ms" in result.metadata


# ---------------------------------------------------------------------------
# test 4: structured output parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_schedule_parses_data_from_summary():
    """Data field se vyplní parsováním ID, object type a statusu z výstupu."""
    aeterna = _make_aeterna()
    result = await aeterna.schedule("schedule weekly Apollo review every Friday 10am")

    assert result.success is True
    assert result.data is not None
    assert result.data["object_id"] == "42"
    assert result.data["object_type"] == "scheduled_task"
    assert result.data["status"] == "success"


# ---------------------------------------------------------------------------
# test 5: malformed output → data=None, success=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_schedule_handles_malformed_output():
    """Pokud Claude vrátí nestrukturovaný výstup, data=None, ale success=True a summary non-empty."""
    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(return_value="Hotovo, task vytvořen, číslo 999.")

    aeterna = _make_aeterna(mock_claude=mock_claude)
    result = await aeterna.schedule("schedule something")

    assert result.success is True
    assert result.summary != ""
    assert result.data is None


# ---------------------------------------------------------------------------
# test 6: Claude failure → success=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_schedule_handles_claude_failure():
    """Pokud Claude selže, schedule vrátí success=False s popisem chyby."""
    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(side_effect=RuntimeError("API timeout"))

    aeterna = _make_aeterna(mock_claude=mock_claude)
    result = await aeterna.schedule("schedule something")

    assert result.success is False
    assert result.error is not None
    assert "API timeout" in result.error
    assert result.summary == ""


# ---------------------------------------------------------------------------
# test 7: review task prompt obsahuje zákaz modifikace
# ---------------------------------------------------------------------------


def test_aeterna_review_is_readonly_intent():
    """Task prompt pro review vždy obsahuje instrukci 'Do not modify anything'."""
    aeterna = _make_aeterna()

    prompt = aeterna._build_review_task(scope=None)
    assert "Do not modify anything" in prompt

    prompt_with_scope = aeterna._build_review_task(scope="this week")
    assert "Do not modify anything" in prompt_with_scope
    assert "this week" in prompt_with_scope


# ---------------------------------------------------------------------------
# test 8: confirmation gate — gate.request() se zavolá pro dangerous tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_confirmation_gate_passthrough(monkeypatch):
    """Pokud je tool v DANGEROUS_TOOLS a gate schválí, výsledek je tool_result."""
    mock_gate = AsyncMock()
    mock_gate.request = AsyncMock(return_value=True)

    captured: dict = {}

    async def mock_complete(system, messages, max_tokens=None, tools=None, tool_executor=None):
        captured["executor"] = tool_executor
        return _SAMPLE_SCHEDULE_OUTPUT

    mock_claude = AsyncMock()
    mock_claude.complete = mock_complete

    aeterna = _make_aeterna(mock_claude=mock_claude, confirmation_gate=mock_gate)
    await aeterna.schedule("test schedule intent")

    executor = captured["executor"]
    assert executor is not None

    # schedule_task je v Aernině whitelistu; dočasně ho přidáme do DANGEROUS_TOOLS
    monkeypatch.setattr("agents.aeterna.DANGEROUS_TOOLS", frozenset({"schedule_task"}))

    result = await executor("schedule_task", {"prompt": "weekly review"})

    mock_gate.request.assert_called_once_with("schedule_task", {"prompt": "weekly review"})
    assert result == "tool_result"


# ---------------------------------------------------------------------------
# test 9: scheduled context blokuje dangerous tool bez volání gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_scheduled_context_blocks_dangerous(monkeypatch):
    """V scheduled kontextu se dangerous tool zablokuje čistým errorem, gate se nevolá."""
    mock_gate = AsyncMock()
    mock_gate.request = AsyncMock(return_value=True)

    captured: dict = {}

    async def mock_complete(system, messages, max_tokens=None, tools=None, tool_executor=None):
        captured["executor"] = tool_executor
        return _SAMPLE_SCHEDULE_OUTPUT

    mock_claude = AsyncMock()
    mock_claude.complete = mock_complete

    aeterna = _make_aeterna(
        mock_claude=mock_claude,
        confirmation_gate=mock_gate,
        is_scheduled_context=True,
    )
    await aeterna.schedule("test")

    executor = captured["executor"]
    monkeypatch.setattr("agents.aeterna.DANGEROUS_TOOLS", frozenset({"schedule_task"}))

    result = await executor("schedule_task", {"prompt": "test"})

    mock_gate.request.assert_not_called()
    assert isinstance(result, dict)
    assert "error" in result
    assert "scheduled context" in result["error"]


# ---------------------------------------------------------------------------
# test 10: bez gate → čistý error dict, ne crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aeterna_without_confirmation_gate(monkeypatch):
    """Pokud confirmation_gate=None a je zavolán dangerous tool, vrátí error dict — ne výjimku."""
    captured: dict = {}

    async def mock_complete(system, messages, max_tokens=None, tools=None, tool_executor=None):
        captured["executor"] = tool_executor
        return _SAMPLE_SCHEDULE_OUTPUT

    mock_claude = AsyncMock()
    mock_claude.complete = mock_complete

    aeterna = _make_aeterna(mock_claude=mock_claude, confirmation_gate=None)
    await aeterna.schedule("test")

    executor = captured["executor"]
    monkeypatch.setattr("agents.aeterna.DANGEROUS_TOOLS", frozenset({"schedule_task"}))

    result = await executor("schedule_task", {"prompt": "test"})

    assert isinstance(result, dict)
    assert "error" in result
    assert "not configured" in result["error"]


# ---------------------------------------------------------------------------
# test 11: plan_task factory tool — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_task_tool_success():
    """make_plan_task_tool: úspěch Aeterny → dict s success=True a summary."""
    mock_aeterna = AsyncMock()
    mock_aeterna.schedule = AsyncMock(
        return_value=SubagentResult(
            success=True,
            summary=_SAMPLE_SCHEDULE_OUTPUT,
            data={"object_id": "42", "object_type": "scheduled_task", "status": "success"},
            metadata={"latency_ms": 200, "trace_id": "abc"},
        )
    )

    plan_task = make_plan_task_tool(mock_aeterna)
    result = await plan_task("schedule weekly Apollo review every Friday 10am")

    assert result["success"] is True
    assert result["summary"] != ""
    assert result["data"]["object_id"] == "42"
    assert "metadata" in result
    assert "error" not in result


# ---------------------------------------------------------------------------
# test 12: review_my_schedule factory tool — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_schedule_tool_success():
    """make_review_schedule_tool: úspěch Aeterny → dict s success=True a summary."""
    mock_aeterna = AsyncMock()
    mock_aeterna.review = AsyncMock(
        return_value=SubagentResult(
            success=True,
            summary="Active tasks: 3, upcoming events: 5. No issues found.",
            metadata={"latency_ms": 150, "trace_id": "xyz"},
        )
    )

    review_my_schedule = make_review_schedule_tool(mock_aeterna)
    result = await review_my_schedule(scope="this week")

    assert result["success"] is True
    assert result["summary"] != ""
    assert "metadata" in result
    assert "error" not in result


# ---------------------------------------------------------------------------
# test 13: plan_task factory tool — error passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_task_tool_error_passthrough():
    """Pokud Aeterna selže, plan_task vrátí error dict — ne výjimku."""
    mock_aeterna = AsyncMock()
    mock_aeterna.schedule = AsyncMock(
        return_value=SubagentResult(
            success=False,
            summary="",
            error="Claude API timeout",
            metadata={"latency_ms": 5000, "trace_id": "fail"},
        )
    )

    plan_task = make_plan_task_tool(mock_aeterna)
    result = await plan_task("schedule something")

    assert result["success"] is False
    assert "error" in result
    assert "Claude API timeout" in result["error"]
    assert "metadata" in result
