import asyncio
import json
import logging
import os
from collections.abc import Callable, Awaitable
from datetime import datetime
from typing import TYPE_CHECKING

from llm.claude import ClaudeClient
from reasoning.context import ReasoningContext, ReasoningStep

if TYPE_CHECKING:
    from vault.vault_manager import VaultManager

logger = logging.getLogger(__name__)

_KEYWORDS = [
    "naplánuj", "zjisti", "porovnej", "rozhodni", "analyzuj",
    "co si myslíš o", "jak bych měl",
]

_STEP_PROMPTS: dict[str, str] = {
    "diagnose": (
        "Analyze this request carefully. What exactly is being asked? "
        "What information do you need to answer well? List any gaps in your knowledge. "
        "Do NOT answer yet — only diagnose."
    ),
    "gather": (
        "Based on your diagnosis, gather the missing context. "
        "You have access to all registered tools (vault_search, vault_read, web_search, etc.). "
        "Call whatever you need. Be targeted — only fetch what is genuinely necessary."
    ),
    "act": (
        "Execute any required actions (write to calendar, send an email, update vault, etc.). "
        "If no actions are needed, state that briefly and do nothing."
    ),
    "reflect": (
        "Review the context gathered so far and draft a mental outline of your response. "
        "Rate your readiness 1-5 (5 = fully ready). "
        "Respond ONLY with valid JSON, no extra text:\n"
        '{"score": <int 1-5>, "issues": [<strings>], "skip_revision": <bool>}\n'
        "Set skip_revision to true if score >= 4."
    ),
    "finalize": (
        "Produce the final response for the user. "
        "Incorporate all gathered context and address any issues noted during reflection. "
        "Be clear, concise, and helpful."
    ),
}


class ReasoningEngine:
    MAX_ITERATIONS = 4

    def __init__(self, claude_client: ClaudeClient, vault_manager: "VaultManager | None" = None) -> None:
        self._claude = claude_client
        self._vault = vault_manager

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def process(
        self,
        user_input: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None,
        tool_executor: Callable | None,
        progress_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        if not self.needs_iteration(user_input):
            return await self._direct_response(system_prompt, messages, tools, tool_executor)

        ctx = ReasoningContext(user_input=user_input)

        await self._notify(progress_callback, "🔄 Přemýšlím… (diagnostika)")

        for _ in range(self.MAX_ITERATIONS):
            # diagnose
            step = await self._run_step("diagnose", ctx, system_prompt, messages, tools, tool_executor)
            ctx.add_step(step)

            # gather
            await self._notify(progress_callback, "🔄 Přemýšlím… (sbírám kontext)")
            step = await self._run_step("gather", ctx, system_prompt, messages, tools, tool_executor)
            ctx.add_step(step)

            # act
            step = await self._run_step("act", ctx, system_prompt, messages, tools, tool_executor)
            ctx.add_step(step)

            # reflect
            await self._notify(progress_callback, "🔄 Přemýšlím… (reviduju odpověď)")
            step = await self._run_step("reflect", ctx, system_prompt, messages, tools, None)
            ctx.add_step(step)

            reflection = self._parse_reflection(step.thought or "")
            if reflection.get("skip_revision") or reflection.get("score", 0) >= 4:
                break

        # finalize
        step = await self._run_step("finalize", ctx, system_prompt, messages, tools, tool_executor)
        step.output = step.output or step.thought
        ctx.add_step(step)

        asyncio.create_task(self._write_trace(ctx))

        return ctx.compile_result()

    def needs_iteration(self, text: str) -> bool:
        if os.getenv("SKIP_REASONING", "").lower() == "true":
            return False
        if len(text) > 150:
            return True
        lower = text.lower()
        if text.count("?") > 1:
            return True
        for kw in _KEYWORDS:
            if kw in lower:
                return True
        return False

    async def delegate_to_subagent(self, task: str, agent_id: str) -> str:
        raise NotImplementedError("Subagents not yet configured")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _direct_response(self, system_prompt, messages, tools, tool_executor) -> str:
        return await self._claude.complete(
            system=system_prompt,
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
        )

    async def _run_step(
        self,
        step_type: str,
        ctx: ReasoningContext,
        base_system: str,
        original_messages: list[dict],
        tools: list[dict] | None,
        tool_executor: Callable | None,
    ) -> ReasoningStep:
        step_prompt = _STEP_PROMPTS[step_type]

        # Inject context summary into the system prompt — avoids consecutive user messages
        # which the Anthropic API rejects (roles must strictly alternate).
        context_summary = self._context_summary(ctx)
        if context_summary:
            system = f"{step_prompt}\n\n[Reasoning context so far]\n{context_summary}\n\n---\n{base_system}"
        else:
            system = f"{step_prompt}\n\n---\n{base_system}"

        messages = list(original_messages)

        tool_calls: list = []
        tool_results: list = []

        # Wrap tool_executor to record calls
        wrapped_executor = None
        if tool_executor and step_type in ("gather", "act", "finalize"):
            async def wrapped_executor(name: str, kwargs: dict):
                tool_calls.append({"name": name, "input": kwargs})
                result = await tool_executor(name, kwargs)
                tool_results.append({"name": name, "result": str(result)[:500]})
                return result

        try:
            output = await self._claude.complete(
                system=system,
                messages=messages,
                tools=tools if step_type in ("gather", "act", "finalize") else None,
                tool_executor=wrapped_executor,
            )
        except Exception:
            logger.exception("ReasoningEngine step %s failed", step_type)
            output = ""

        return ReasoningStep(
            type=step_type,
            thought=output,
            tool_calls=tool_calls,
            tool_results=tool_results,
            output=output if step_type == "finalize" else None,
        )

    @staticmethod
    def _context_summary(ctx: ReasoningContext) -> str:
        parts = []
        for step in ctx.steps:
            if step.thought:
                parts.append(f"[{step.type.upper()}] {step.thought[:600]}")
            for tr in step.tool_results:
                parts.append(f"  tool={tr['name']}: {tr['result'][:300]}")
        return "\n".join(parts)

    @staticmethod
    def _parse_reflection(text: str) -> dict:
        try:
            # Extract JSON from text (Claude may add surrounding prose)
            start = text.find("{")
            end = text.rfind("}") + 1
            if start != -1 and end > start:
                return json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return {"score": 4, "issues": [], "skip_revision": True}

    async def _write_trace(self, ctx: ReasoningContext) -> None:
        if self._vault is None:
            return
        try:
            ts = datetime.now().strftime("%Y-%m-%d-%H-%M")
            path = f"reasoning-traces/{ts}.md"
            content = (
                f"---\nsummary: \"Reasoning trace {ts}\"\n"
                f"updated: \"{datetime.now().date().isoformat()}\"\n---\n\n"
                f"# Query\n{ctx.user_input}\n\n"
                f"# Steps\n```json\n{ctx.to_json()}\n```\n"
            )
            await asyncio.to_thread(self._vault.write, path, content)
        except Exception:
            logger.exception("Failed to write reasoning trace")

    @staticmethod
    async def _notify(callback, message: str) -> None:
        if callback is not None:
            try:
                await callback(message)
            except Exception:
                pass
