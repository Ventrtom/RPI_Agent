import asyncio
from functools import partial

from mem0 import Memory


class MemoryClient:
    def __init__(self, user_id: str, chroma_path: str):
        self._user_id = user_id
        config = {
            "llm": {
                "provider": "anthropic",
                "config": {
                    "model": "claude-haiku-4-5",
                },
            },
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": "agent_memory",
                    "path": chroma_path,
                },
            },
            "embedder": {
                "provider": "huggingface",
                "config": {
                    "model": "all-MiniLM-L6-v2",
                },
            },
        }
        self._memory = Memory.from_config(config)

    async def search(self, query: str, limit: int = 10) -> list[str]:
        """Vrátí list textových vzpomínek relevantních pro dotaz."""
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            partial(self._memory.search, query, user_id=self._user_id, limit=limit),
        )
        return [r["memory"] for r in results.get("results", [])]

    async def add(self, messages: list[dict]) -> None:
        """Extrahuje a uloží fakta z konverzace."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            partial(self._memory.add, messages, user_id=self._user_id),
        )

    async def get_all(self) -> list[str]:
        """Vrátí všechny uložené vzpomínky — pro debug."""
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            partial(self._memory.get_all, user_id=self._user_id),
        )
        return [r["memory"] for r in results.get("results", [])]
