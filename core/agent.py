import asyncio

from core.prompts import build_system_prompt
from core.session import SessionManager
from llm.claude import ClaudeClient
from memory.client import MemoryClient


class Agent:
    def __init__(
        self,
        memory_client: MemoryClient,
        claude_client: ClaudeClient,
        session_manager: SessionManager,
    ):
        self._memory = memory_client
        self._claude = claude_client
        self._sessions = session_manager

    async def process(
        self,
        user_message: str,
        session_id: str,
        user_id: str = "tomas",
    ) -> str:
        """
        Přijme textový vstup, vrátí textový výstup.

        1. Načte session historii
        2. Načte relevantní vzpomínky z Mem0
        3. Sestaví zprávy pro Claude
        4. Zavolá Claude API
        5. Uloží zprávy do session
        6. Asynchronně na pozadí uloží nová fakta do Mem0
        7. Vrátí textovou odpověď
        """
        session = await self._sessions.get_or_create(session_id, user_id)
        memories = await self._memory.search(user_message)
        system_prompt = build_system_prompt(memories)

        history = await self._sessions.get_history(session_id)
        messages = history + [{"role": "user", "content": user_message}]

        response_text = await self._claude.complete(system=system_prompt, messages=messages)

        await self._sessions.add_message(session_id, "user", user_message)
        await self._sessions.add_message(session_id, "assistant", response_text)

        full_exchange = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": response_text},
        ]
        asyncio.create_task(self._memory.add(full_exchange))

        return response_text
