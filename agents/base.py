"""
Sdílený základ pro všechny subagenty Prime.

Obsahuje SubagentResult (standardizovaný návratový typ) a BaseSubagent
(abstraktní kostra — konkrétní subagenti dědí a přizpůsobují _build_task_prompt).
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from llm.claude import ClaudeClient

if TYPE_CHECKING:
    from observability.telemetry import TelemetryLogger

logger = logging.getLogger(__name__)

_SUBAGENT_MAX_TOKENS = 4096


@dataclass
class SubagentResult:
    """Standardizovaný výstup každého subagenta vracený Prime."""

    success: bool
    """True pokud subagent dokončil úkol, False pokud selhal."""

    summary: str
    """Finální brief pro Prime (hlavní payload, 100–1000 tokenů)."""

    data: dict | None = None
    """Volitelná strukturovaná data (např. IDs vytvořených tasků)."""

    error: str | None = None
    """Pokud success=False, popis chyby."""

    clarification_needed: str | None = None
    """Pokud subagent potřebuje upřesnění od uživatele skrz Prime."""

    metadata: dict = field(default_factory=dict)
    """Latence, počet tokenů, tool calls, trace_id."""


class BaseSubagent:
    """
    Společná kostra pro všechny subagenty.

    Každý subagent dostane bílou listinu nástrojů (scoped_tools) a
    sdílený tool_executor z Prime. Metoda run() spustí izolovanou
    konverzaci s Claude a vrátí SubagentResult.

    Poznámka k parametru model: v Fázi 0 se nepoužívá — ClaudeClient
    nemá per-call override. Subagenty ve Fázi 1+ si vytvoří vlastní
    instanci ClaudeClient s příslušným modelem.
    """

    def __init__(
        self,
        claude_client: ClaudeClient,
        scoped_tools: list[dict],
        tool_executor: Callable,
        name: str,
        system_prompt: str,
        max_iterations: int = 10,
        model: str | None = None,
        telemetry_logger: TelemetryLogger | None = None,
    ) -> None:
        self.claude_client = claude_client
        self.scoped_tools = scoped_tools
        self.tool_executor = tool_executor
        self.name = name
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.model = model
        self._telemetry_logger = telemetry_logger

    async def run(self, task: str, context: dict | None = None, _method: str = "run") -> SubagentResult:
        """
        Spustí subagenta na daném úkolu a vrátí SubagentResult.

        Iterační limit je vynucen obalem kolem tool_executor — po
        max_iterations voláních vyhodí RuntimeError, který ClaudeClient
        zachytí a předá Claude jako chybovou zprávu.
        """
        trace_id = str(uuid.uuid4())
        start = time.monotonic()
        logger.info("[%s] Subagent '%s' start", trace_id, self.name)

        iteration_count = 0
        tool_calls_count = 0

        async def limited_executor(name: str, kwargs: dict) -> object:
            nonlocal iteration_count, tool_calls_count
            iteration_count += 1
            tool_calls_count += 1
            if iteration_count > self.max_iterations:
                raise RuntimeError(
                    f"Iteration limit ({self.max_iterations}) exceeded"
                )
            return await self.tool_executor(name, kwargs)

        messages = [{"role": "user", "content": self._build_task_prompt(task, context)}]
        executor = limited_executor if self.tool_executor else None
        tools = self.scoped_tools if self.scoped_tools else None

        try:
            summary = await self.claude_client.complete(
                system=self.system_prompt,
                messages=messages,
                tools=tools,
                tool_executor=executor,
                max_tokens=_SUBAGENT_MAX_TOKENS,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "[%s] Subagent '%s' done in %d ms (%d tool calls)",
                trace_id,
                self.name,
                latency_ms,
                tool_calls_count,
            )
            result = SubagentResult(
                success=True,
                summary=summary,
                metadata={
                    "trace_id": trace_id,
                    "latency_ms": latency_ms,
                    "iterations": iteration_count,
                    "tool_calls_count": tool_calls_count,
                },
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.exception("[%s] Subagent '%s' failed", trace_id, self.name)
            result = SubagentResult(
                success=False,
                summary="",
                error=str(exc),
                metadata={
                    "trace_id": trace_id,
                    "latency_ms": latency_ms,
                    "iterations": iteration_count,
                    "tool_calls_count": tool_calls_count,
                },
            )

        if self._telemetry_logger:
            from observability.telemetry import _current_session_id
            sid = _current_session_id.get("")
            try:
                await self._telemetry_logger.log_delegation(
                    subagent=self.name,
                    method=_method,
                    task=task,
                    result=result,
                    session_id=sid,
                )
            except Exception:
                logger.warning("Telemetry log_delegation selhal (subagent=%s)", self.name)

        return result

    def _build_task_prompt(self, task: str, context: dict | None) -> str:
        """Sestaví prompt předaný Claude. Subagenti mohou přepsat."""
        if not context:
            return task
        ctx_lines = "\n".join(f"{k}: {v}" for k, v in context.items())
        return f"Kontext:\n{ctx_lines}\n\nÚkol:\n{task}"
