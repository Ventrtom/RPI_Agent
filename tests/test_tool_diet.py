"""Tests pro Mikro-fázi A: tool diet — segregace tools mezi prime a internal registry."""
import pytest
from unittest.mock import MagicMock

from tools import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_SCHEMA = {"type": "object", "properties": {}, "required": []}


def _make_tool(name: str):
    """Dummy async function with the given __name__ for registry testing."""
    async def tool(**kwargs):  # noqa: ARG001
        return {}
    tool.__name__ = name
    tool.__doc__ = f"Mock tool {name}"
    return tool


def _build_prime_registry() -> ToolRegistry:
    """Builds a prime_registry matching main.py's registration."""
    r = ToolRegistry()
    for name in (
        "get_system_status", "get_agent_logs", "restart_agent_service", "shutdown_raspberry_pi",
        "get_contacts", "get_contact_by_name", "add_contact", "remove_contact",
        "send_telegram_message", "list_tasks", "get_self_info",
        "vault_read", "vault_write", "vault_search", "vault_patch",
        "set_voice_profile", "get_observability_data",
    ):
        r.register(_make_tool(name), _MOCK_SCHEMA)
    return r


def _build_internal_registry() -> ToolRegistry:
    """Builds an internal_registry matching main.py's registration."""
    r = ToolRegistry()
    for name in (
        "web_search",
        "get_calendar_events", "create_calendar_event", "delete_calendar_event", "find_free_slots",
        "send_email",
        "schedule_task", "get_task_details", "cancel_task", "enable_task", "update_task",
    ):
        r.register(_make_tool(name), _MOCK_SCHEMA)
    return r


def _mock_claude():
    from llm.claude import ClaudeClient
    return MagicMock(spec=ClaudeClient)


def _mock_memory():
    from memory.client import MemoryClient
    return MagicMock(spec=MemoryClient)


def _mock_vault():
    from vault.vault_manager import VaultManager
    return MagicMock(spec=VaultManager)


# ---------------------------------------------------------------------------
# Prime registry — must NOT contain these tools
# ---------------------------------------------------------------------------


def test_prime_registry_does_not_have_web_search():
    assert not _build_prime_registry().has_tool("web_search")


def test_prime_registry_does_not_have_calendar_writes():
    prime = _build_prime_registry()
    for name in ("create_calendar_event", "delete_calendar_event", "get_calendar_events", "find_free_slots"):
        assert not prime.has_tool(name), f"Prime should not have {name}"


def test_prime_registry_does_not_have_scheduler_writes():
    prime = _build_prime_registry()
    for name in ("schedule_task", "get_task_details", "cancel_task", "enable_task", "update_task"):
        assert not prime.has_tool(name), f"Prime should not have {name}"


def test_prime_registry_does_not_have_send_email():
    assert not _build_prime_registry().has_tool("send_email")


# ---------------------------------------------------------------------------
# Prime registry — must contain these tools
# ---------------------------------------------------------------------------


def test_prime_registry_keeps_list_tasks():
    assert _build_prime_registry().has_tool("list_tasks")


def test_prime_registry_keeps_vault_writes():
    prime = _build_prime_registry()
    assert prime.has_tool("vault_write")
    assert prime.has_tool("vault_patch")


# ---------------------------------------------------------------------------
# Internal registry — must contain
# ---------------------------------------------------------------------------


def test_internal_registry_has_web_search():
    assert _build_internal_registry().has_tool("web_search")


def test_internal_registry_has_calendar_writes():
    internal = _build_internal_registry()
    for name in ("create_calendar_event", "delete_calendar_event", "get_calendar_events", "find_free_slots"):
        assert internal.has_tool(name), f"Internal registry should have {name}"


# ---------------------------------------------------------------------------
# Subagent scoped_tools — access via internal registry
# ---------------------------------------------------------------------------


def test_veritas_can_access_web_search_via_internal():
    from agents.veritas import Veritas
    prime = _build_prime_registry()
    internal = _build_internal_registry()
    veritas = Veritas(
        claude_client=_mock_claude(),
        memory_client=_mock_memory(),
        vault_manager=_mock_vault(),
        tool_registry=prime,
        internal_registry=internal,
    )
    tool_names = {s["name"] for s in veritas.scoped_tools}
    assert "web_search" in tool_names


def test_aeterna_can_access_calendar_via_internal():
    from agents.aeterna import Aeterna
    prime = _build_prime_registry()
    internal = _build_internal_registry()
    aeterna = Aeterna(
        claude_client=_mock_claude(),
        tool_registry=prime,
        internal_registry=internal,
    )
    tool_names = {s["name"] for s in aeterna.scoped_tools}
    for name in ("get_calendar_events", "create_calendar_event", "delete_calendar_event"):
        assert name in tool_names, f"Aeterna scoped_tools missing {name}"


def test_aeterna_can_access_send_email():
    from agents.aeterna import Aeterna
    prime = _build_prime_registry()
    internal = _build_internal_registry()
    aeterna = Aeterna(
        claude_client=_mock_claude(),
        tool_registry=prime,
        internal_registry=internal,
    )
    assert "send_email" in {s["name"] for s in aeterna.scoped_tools}


def test_glaedr_unchanged_scoped_tools():
    from agents.glaedr import Glaedr, GLAEDR_TOOL_WHITELIST
    prime = _build_prime_registry()
    internal = _build_internal_registry()
    glaedr = Glaedr(
        claude_client=_mock_claude(),
        memory_client=_mock_memory(),
        vault_manager=_mock_vault(),
        tool_registry=prime,
        internal_registry=internal,
    )
    tool_names = {s["name"] for s in glaedr.scoped_tools}
    assert tool_names == GLAEDR_TOOL_WHITELIST


# ---------------------------------------------------------------------------
# Source tools — must not be registered in any registry
# ---------------------------------------------------------------------------


def test_source_tools_not_registered():
    prime = _build_prime_registry()
    internal = _build_internal_registry()
    for name in ("list_own_source", "read_own_source"):
        assert not prime.has_tool(name), f"Prime should not have {name}"
        assert not internal.has_tool(name), f"Internal should not have {name}"


# ---------------------------------------------------------------------------
# Executor routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_tool_lookup_falls_back_to_internal():
    """When a tool is absent from prime but present in internal, executor routes to internal."""
    from agents.veritas import Veritas

    prime = _build_prime_registry()

    internal = ToolRegistry()

    async def web_search(query, max_results=5):  # noqa: ARG001
        return {"result": "from_internal"}

    internal.register(web_search, _MOCK_SCHEMA)

    veritas = Veritas(
        claude_client=_mock_claude(),
        memory_client=_mock_memory(),
        vault_manager=_mock_vault(),
        tool_registry=prime,
        internal_registry=internal,
    )
    result = await veritas.tool_executor("web_search", {"query": "test"})
    assert result == {"result": "from_internal"}


@pytest.mark.asyncio
async def test_subagent_tool_lookup_rejects_unknown_tool():
    """A tool not in the whitelist returns an error string, not an exception."""
    from agents.veritas import Veritas

    prime = _build_prime_registry()
    internal = _build_internal_registry()

    veritas = Veritas(
        claude_client=_mock_claude(),
        memory_client=_mock_memory(),
        vault_manager=_mock_vault(),
        tool_registry=prime,
        internal_registry=internal,
    )
    result = await veritas.tool_executor("send_email", {"to": "x"})
    assert isinstance(result, str)
    assert "not available" in result.lower() or "permitted" in result.lower()
