from typing import Callable


class ToolRegistry:
    """
    Registr nástrojů dostupných agentovi.
    V MVP fázi prázdný — připravený pro budoucí rozšíření.
    """

    def __init__(self):
        self._tools: list[Callable] = []

    def register(self, tool_fn: Callable) -> None:
        self._tools.append(tool_fn)

    def get_all(self) -> list[Callable]:
        return list(self._tools)

    def get_schemas(self) -> list[dict]:
        """Vrátí schémata nástrojů pro Claude tool use API."""
        schemas = []
        for tool in self._tools:
            schema = {
                "name": tool.__name__,
                "description": (tool.__doc__ or "").strip(),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            }
            schemas.append(schema)
        return schemas


tool_registry = ToolRegistry()
