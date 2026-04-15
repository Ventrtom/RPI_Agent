import asyncio
import inspect
from collections.abc import Callable


class ToolRegistry:
    """Registry of tools available to the agent."""

    def __init__(self):
        self._tools: list[tuple[Callable, dict]] = []

    def register(self, tool_fn: Callable, input_schema: dict | None = None) -> None:
        """Register a tool function with an optional input schema for Claude tool use API.

        If input_schema is omitted, the tool is assumed to take no parameters.
        """
        schema = input_schema or {"type": "object", "properties": {}, "required": []}
        self._tools.append((tool_fn, schema))

    def get_all(self) -> list[Callable]:
        return [fn for fn, _ in self._tools]

    def get_schemas(self) -> list[dict]:
        """Return tool schemas for Claude tool use API."""
        return [
            {
                "name": fn.__name__,
                "description": (fn.__doc__ or "").strip(),
                "input_schema": schema,
            }
            for fn, schema in self._tools
        ]

    async def execute(self, name: str, kwargs: dict) -> object:
        """Execute a registered tool by name with the given arguments."""
        for fn, _ in self._tools:
            if fn.__name__ == name:
                if inspect.iscoroutinefunction(fn):
                    return await fn(**kwargs)
                return fn(**kwargs)
        raise ValueError(f"Unknown tool: {name}")

    def __len__(self) -> int:
        return len(self._tools)


tool_registry = ToolRegistry()

from tools.google_tools import (  # noqa: E402
    CREATE_CALENDAR_EVENT_SCHEMA,
    GET_CALENDAR_EVENTS_SCHEMA,
    SEND_EMAIL_SCHEMA,
    create_calendar_event,
    get_calendar_events,
    send_email,
)

tool_registry.register(get_calendar_events, GET_CALENDAR_EVENTS_SCHEMA)
tool_registry.register(create_calendar_event, CREATE_CALENDAR_EVENT_SCHEMA)
tool_registry.register(send_email, SEND_EMAIL_SCHEMA)
