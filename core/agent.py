import asyncio
import logging
import os

from core.prompts import build_system_prompt
from core.session import SessionManager
from llm.claude import ClaudeClient
from memory.client import MemoryClient
from tools import ToolRegistry
from tools.confirmation import DANGEROUS_TOOLS

logger = logging.getLogger(__name__)

_SCHEDULED_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS_SCHEDULED", "2048"))


class Agent:
    def __init__(
        self,
        memory_client: MemoryClient,
        claude_client: ClaudeClient,
        session_manager: SessionManager,
        tool_registry: ToolRegistry | None = None,
        reasoning_engine=None,
        confirmation_gate=None,  # tools.confirmation.ConfirmationGate | None
    ) -> None:
        self._memory = memory_client
        self._claude = claude_client
        self._sessions = session_manager
        self._tools = tool_registry
        self._reasoning = reasoning_engine
        self._confirmation_gate = confirmation_gate

    async def process(
        self,
        user_message: str,
        session_id: str,
        user_id: str,
        is_voice: bool = False,
        is_scheduled: bool = False,
        progress_callback=None,
    ) -> str:
        """
        Process user input and return response.

        1. Load session history
        2. Fetch relevant memories from Mem0
        3. Build messages for Claude
        4. Call Claude API (with tool use loop if tools registered)
        5. Save messages to session
        6. Asynchronously save new facts to Mem0 in background
        7. Return text response
        """
        await self._sessions.get_or_create(session_id, user_id)
        try:
            memories = await self._memory.search(user_message)
        except Exception:
            logger.exception("Memory search failed (session=%s), continuing without memories", session_id)
            memories = []
        system_prompt = build_system_prompt(memories, is_voice=is_voice, is_scheduled=is_scheduled)

        history = await self._sessions.get_history(session_id)
        voice_suffix = "\n\n[Respond in english in flowing sentences without markdown, bullet points or headings.]" if is_voice else ""
        messages = history + [{"role": "user", "content": user_message + voice_suffix}]

        tools = self._tools.get_schemas() if self._tools and len(self._tools) > 0 else None
        raw_executor = self._tools.execute if tools else None
        tool_executor = (
            self._make_confirmed_executor(raw_executor, is_scheduled)
            if raw_executor is not None else None
        )

        try:
            if self._reasoning is not None:
                response_text = await self._reasoning.process(
                    user_message, system_prompt, messages, tools, tool_executor, progress_callback
                )
            else:
                response_text = await self._claude.complete(
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                    tool_executor=tool_executor,
                    max_tokens=_SCHEDULED_MAX_TOKENS if is_scheduled else None,
                )
        except Exception:
            logger.exception("Error with calling Claude API (session=%s)", session_id)
            return "Sorry, there was an error communicating with AI. Please try again in a moment."

        await self._sessions.add_message(session_id, "user", user_message)
        await self._sessions.add_message(session_id, "assistant", response_text)

        full_exchange = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": response_text},
        ]
        asyncio.create_task(self._save_memory(full_exchange))

        return response_text

    def _make_confirmed_executor(self, raw_executor, is_scheduled: bool):
        """Wraps raw_executor to require human confirmation for DANGEROUS_TOOLS."""
        gate = self._confirmation_gate

        async def confirmed_execute(name: str, kwargs: dict) -> object:
            if name not in DANGEROUS_TOOLS:
                return await raw_executor(name, kwargs)

            if is_scheduled:
                logger.warning("Dangerous tool '%s' blocked in scheduled context", name)
                return {
                    "error": (
                        f"Tool '{name}' requires human confirmation and cannot be "
                        "executed in an automated/scheduled context. "
                        "No human is present to approve this action."
                    )
                }

            if gate is None:
                logger.warning("Dangerous tool '%s' called but no confirmation gate configured", name)
                return {
                    "error": (
                        f"Tool '{name}' requires human confirmation, but the "
                        "confirmation system is not configured in this mode."
                    )
                }

            try:
                approved = await gate.request(name, kwargs)
            except RuntimeError as exc:
                logger.warning("ConfirmationGate.request() raised: %s", exc)
                return {"error": str(exc)}

            if not approved:
                logger.info("Dangerous tool '%s' denied by user (or timed out)", name)
                return {
                    "error": (
                        f"Tool '{name}' was denied by the user (or the 60-second "
                        "confirmation window expired). No action was taken."
                    )
                }

            logger.info("Dangerous tool '%s' approved by user, executing", name)
            return await raw_executor(name, kwargs)

        return confirmed_execute

    async def open_session(self, session_id: str, user_id: str) -> None:
        """Ensure session exists (idempotent). Called on /start before first message."""
        await self._sessions.get_or_create(session_id, user_id)

    async def close_session(self, session_id: str) -> None:
        """Close and remove a session (e.g. /clear or /newsession)."""
        await self._sessions.close_session(session_id)

    async def get_all_memories(self) -> list[str]:
        """Return all stored long-term memories (for /memory command)."""
        return await self._memory.get_all()

    async def _save_memory(self, messages: list[dict]) -> None:
        try:
            await self._memory.add(messages)
        except Exception:
            logger.exception("Error while saving Mem0")
