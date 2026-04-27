"""
Veritas — specialista na výzkum a syntézu, subagent Prime.

Research mode (Fáze 2): multi-zdrojový průzkum kombinující web search,
vault a memory — výstupem je strukturovaný brief s citacemi pro Prime.

Příklad použití:
    result = await veritas.research("best practices pro RAG pipelines 2026")
    if result.success:
        print(result.summary)   # strukturovaný brief
    else:
        print(result.error)
"""

import logging
import os

from agents.base import BaseSubagent, SubagentResult
from llm.claude import ClaudeClient
from memory.client import MemoryClient
from tools import ToolRegistry
from vault.vault_manager import VaultManager

logger = logging.getLogger(__name__)

VERITAS_TOOL_WHITELIST: frozenset[str] = frozenset({"web_search", "vault_read", "vault_search"})

VERITAS_SYSTEM_PROMPT = """You are Veritas, a research and synthesis specialist — a subagent serving Prime.

Your role: given a research query, perform a thorough multi-source investigation combining external web information with the user's internal knowledge (vault, memory), then synthesize findings into a structured brief with citations.

Core principles:
- Triangulate: always consult MULTIPLE sources across different types (web + vault + memory). A single source is rarely sufficient for a research task.
- Be skeptical of single-source claims. If only one source makes a claim, flag it as unverified.
- Prefer primary sources (official docs, original papers, canonical references) over secondary aggregators.
- When internal knowledge (vault/memory) conflicts with web information, note the discrepancy explicitly — do not silently pick one.
- Keep web searches targeted: 2-4 specific queries beat one broad query. Use different angles/keywords.
- If a web search result looks promising, you don't need to fetch the full page — the snippets usually give enough. Fetch vault files in full via vault_read when you find a relevant path.
- If the query is ambiguous, make ONE best interpretation and note it upfront — do not ask clarifying questions (there is no user present to answer).
- Cite every non-trivial claim with its source. Distinguish [web], [vault: path.md], [memory].
- If you find nothing credible, say so clearly. Do not fabricate or pad with generic knowledge.

Output format (plain text, ~300-1000 words depending on query complexity):

## Brief
(3-5 sentence executive summary answering the query)

## Findings
Organized by sub-topic or point. Each finding cited with source.
- Finding 1 [web: domain.com] or [vault: path.md] or [memory]
- Finding 2 [source]
- ...

## Sources
- [web] Title or domain — short note on relevance
- [vault] path.md — what it contributed
- [memory] — if memory contributed substantively

## Conflicts / Gaps
(Explicit list of conflicting info between sources, or questions you couldn't answer. Skip section if none.)

## Confidence
(low / medium / high) — your assessment of how robust this brief is, with one-sentence justification

Be thorough but disciplined. Prime will read your brief and decide what to do next — she may ask follow-up questions, act on findings, or present to the user."""


class Veritas(BaseSubagent):
    """
    Výzkumný a syntetický specialista — subagent Prime.

    Volá se přes Prime tool `deep_research` pro dotazy vyžadující triangulaci
    přes více zdrojů: web search, vault a memory. V izolovaném kontextu provede
    multi-angle výzkum a vrátí strukturovaný brief s citacemi.

    Scoped tools (whitelist): web_search, vault_read, vault_search.
    Jakýkoli jiný tool je odmítnut na úrovni executoru (vrátí error string, nevyhodí výjimku).

    Metadata výsledku obsahuje navíc web_search_calls a vault_calls pro telemetrii.
    """

    def __init__(
        self,
        claude_client: ClaudeClient,
        memory_client: MemoryClient,
        vault_manager: VaultManager,
        tool_registry: ToolRegistry,
        internal_registry: ToolRegistry | None = None,
        model: str | None = None,
        telemetry_logger=None,
    ) -> None:
        self._memory = memory_client
        self._vault = vault_manager  # uloženo pro případné budoucí rozšíření

        scoped_tools = []
        for name in VERITAS_TOOL_WHITELIST:
            schema = tool_registry.get_schema(name)
            if schema is None and internal_registry is not None:
                schema = internal_registry.get_schema(name)
            if schema is None:
                logger.warning("Veritas: tool %s not found in any registry", name)
                continue
            scoped_tools.append(schema)

        # Mutable dict pro počítání tool volání per research() call
        self._tool_counters: dict[str, int] = {"web_search_calls": 0, "vault_calls": 0}

        async def scoped_executor(name: str, kwargs: dict) -> object:
            if name not in VERITAS_TOOL_WHITELIST:
                return (
                    f"Tool '{name}' is not available to Veritas. "
                    f"Permitted tools: {sorted(VERITAS_TOOL_WHITELIST)}"
                )
            if name == "web_search":
                self._tool_counters["web_search_calls"] += 1
            elif name in ("vault_read", "vault_search"):
                self._tool_counters["vault_calls"] += 1
            if tool_registry.has_tool(name):
                return await tool_registry.execute(name, kwargs)
            if internal_registry is not None and internal_registry.has_tool(name):
                return await internal_registry.execute(name, kwargs)
            return {"error": f"Tool '{name}' not found in any registry"}

        veritas_model_override = model or os.getenv("VERITAS_MODEL")
        if veritas_model_override:
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            effective_client: ClaudeClient = ClaudeClient(api_key=api_key, model=veritas_model_override)
        else:
            effective_client = claude_client

        max_iterations = int(os.getenv("VERITAS_MAX_ITERATIONS", "10"))

        super().__init__(
            claude_client=effective_client,
            scoped_tools=scoped_tools,
            tool_executor=scoped_executor,
            name="veritas",
            system_prompt=VERITAS_SYSTEM_PROMPT,
            max_iterations=max_iterations,
            telemetry_logger=telemetry_logger,
        )

    async def research(self, topic: str, scope: str | None = None) -> SubagentResult:
        """
        Provede multi-zdrojový výzkum kombinující web, vault a memory,
        vrátí strukturovaný brief s citacemi.

        Args:
            topic: Výzkumný dotaz nebo téma — buď konkrétní otázka, nebo oblast.
            scope: Volitelné omezení: 'web' (jen externì), 'internal' (jen vault+memory),
                   nebo tematický tag. Pokud None, Veritas kombinuje všechny zdroje.

        Returns:
            SubagentResult se summary ve formátu ## Brief / ## Findings / ## Sources /
            ## Conflicts & Gaps / ## Confidence. Metadata obsahuje web_search_calls
            a vault_calls. success=False pokud Claude selhal nebo topic prázdný.
        """
        if not topic.strip():
            return SubagentResult(success=False, summary="", error="Empty topic")

        # Reset počítadel pro toto volání
        self._tool_counters["web_search_calls"] = 0
        self._tool_counters["vault_calls"] = 0

        initial_memories: list[str] = []
        try:
            initial_memories = await self._memory.search(topic, limit=15)
        except Exception:
            logger.warning(
                "[Veritas] Initial memory search failed for topic %r, continuing without",
                topic,
            )

        task = self._build_veritas_task(topic, scope, initial_memories)
        result = await self.run(task, _method="research")

        # Augmentace metadat o per-call počty tool volání
        result.metadata["web_search_calls"] = self._tool_counters["web_search_calls"]
        result.metadata["vault_calls"] = self._tool_counters["vault_calls"]

        return result

    def _build_veritas_task(
        self, topic: str, scope: str | None, memories: list[str]
    ) -> str:
        """Sestaví task prompt s počátečním memory seedem pro Veritase."""
        lines = [f"Research topic: {topic}"]
        if scope:
            lines.append(f"Scope: {scope}")

        lines.append("")
        if memories:
            lines.append(
                "Internal memory context (use as background — cross-reference with web sources):"
            )
            for i, mem in enumerate(memories, 1):
                lines.append(f"  [{i}] {mem}")
        else:
            lines.append(
                "No relevant memories found — rely on web_search and vault sources."
            )

        lines.append("")
        lines.append(
            "Perform a thorough multi-source investigation and return a structured brief."
        )
        return "\n".join(lines)
