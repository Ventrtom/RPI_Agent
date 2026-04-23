import asyncio
import inspect
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of tools available to the agent."""

    def __init__(self, telemetry_logger=None):
        self._tools: list[tuple[Callable, dict]] = []
        self._telemetry_logger = telemetry_logger

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
        start = time.monotonic()
        success = True
        try:
            for fn, _ in self._tools:
                if fn.__name__ == name:
                    if inspect.iscoroutinefunction(fn):
                        return await fn(**kwargs)
                    return fn(**kwargs)
            raise ValueError(f"Unknown tool: {name}")
        except Exception:
            success = False
            raise
        finally:
            if self._telemetry_logger:
                latency_ms = int((time.monotonic() - start) * 1000)
                try:
                    from observability.telemetry import _current_session_id
                    sid = _current_session_id.get("")
                    await self._telemetry_logger.log_tool_call(name, success, latency_ms, sid)
                except Exception:
                    logger.warning("Telemetry log_tool_call selhal (tool=%s)", name)

    def __len__(self) -> int:
        return len(self._tools)
