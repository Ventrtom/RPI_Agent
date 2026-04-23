import asyncio
import logging
import os
from functools import partial

from mem0 import Memory

logger = logging.getLogger(__name__)


class MemoryClient:
    def __init__(self, user_id: str, chroma_path: str) -> None:
        self._user_id = user_id
        self._chroma_path = chroma_path
        config = {
            "llm": {
                "provider": "anthropic",
                "config": {
                    "model": os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
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
                    "model": os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
                },
            },
        }
        self._memory = Memory.from_config(config)

    async def search(self, query: str, limit: int = 10) -> list[str]:
        """Vrátí list textových vzpomínek relevantních pro dotaz."""
        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None,
                partial(self._memory.search, query, filters={"user_id": self._user_id}, top_k=limit),
            )
            return [r["memory"] for r in results.get("results", [])]
        except Exception:
            logger.warning("ChromaDB search failed, continuing without memories", exc_info=True)
            return []

    async def add(self, messages: list[dict]) -> None:
        """Extrahuje a uloží fakta z konverzace."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                partial(self._memory.add, messages, user_id=self._user_id),
            )
        except Exception:
            logger.exception("Memory add failed (Mem0/ChromaDB error)")

    async def get_all(self) -> list[str]:
        """Vrátí všechny uložené vzpomínky — pro debug."""
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None,
            partial(self._memory.get_all, filters={"user_id": self._user_id}),
        )
        return [r["memory"] for r in results.get("results", [])]

    async def get_stats(self) -> dict:
        """Vrátí počet vzpomínek a velikost ChromaDB na disku v MB."""
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None,
            partial(self._memory.get_all, filters={"user_id": self._user_id}),
        )
        count = len(results.get("results", []))

        size_bytes = 0
        try:
            for dirpath, _, filenames in os.walk(self._chroma_path):
                for fname in filenames:
                    fp = os.path.join(dirpath, fname)
                    try:
                        size_bytes += os.path.getsize(fp)
                    except OSError:
                        pass
        except Exception:
            pass

        return {
            "memory_count": count,
            "db_size_mb": round(size_bytes / 1024 / 1024, 2),
        }
