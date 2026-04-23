"""
Glaedr — paměťový a znalostní specialista, subagent Prime.

Retrieve mode (Fáze 1): hluboké vyhledávání v Mem0 + ChromaDB a vaultu,
syntéza výsledků do strukturovaného briefu pro Prime.

Curator mode (Fáze 4): aktivní údržba paměti — zatím neimplementováno.

Příklad použití:
    result = await glaedr.retrieve("rozhodnutí o cache invalidation v projektu Apollo")
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

GLAEDR_TOOL_WHITELIST: frozenset[str] = frozenset({"vault_read", "vault_search"})

GLAEDR_SYSTEM_PROMPT = """You are Glaedr, a memory and knowledge retrieval specialist — a subagent serving Prime.

Your role: given a query, perform a thorough, multi-angle search across the user's long-term memory (semantic) and vault (markdown knowledge base), then synthesize findings into a structured brief.

Core principles:
- Always perform MULTIPLE search queries from different angles. A single search rarely captures what's relevant.
- Prefer 3-5 targeted searches over 1 broad search.
- Search both memory (via provided memories context) AND vault (via vault_search + vault_read).
- If you find a relevant vault file path, READ it with vault_read to get full content — don't guess from the search snippet.
- Cite sources: when a fact comes from vault, note the file path. When from memory, note that it's from long-term memory.
- If the query is ambiguous, make ONE best interpretation and note it upfront — do not ask clarifying questions (there is no user present).
- If you find nothing relevant, say so clearly. Do not fabricate.

Output format (plain text, ~200-800 words):

## Brief
(2-4 sentence executive summary answering the query)

## Findings
- Key point 1 [source: vault/path.md or memory]
- Key point 2 [source: ...]
- ...

## Gaps
(What you searched for but didn't find, if relevant. Skip this section if nothing notable.)

## Confidence
(low / medium / high — your assessment of how complete this brief is)

Be concise but thorough. Prime will read your brief and decide next steps."""


class Glaedr(BaseSubagent):
    """
    Paměťový a znalostní specialista — subagent Prime.

    Volá se přes Prime tool `memory_dive` pro dotazy vyžadující syntézu
    přes více pamětí nebo vault souborů. V izolovaném kontextu provede
    multi-angle hledání a vrátí strukturovaný brief.

    Scoped tools (whitelist): vault_read, vault_search.
    Jakýkoli jiný tool je odmítnut na úrovni executoru.
    """

    def __init__(
        self,
        claude_client: ClaudeClient,
        memory_client: MemoryClient,
        vault_manager: VaultManager,
        tool_registry: ToolRegistry,
        model: str | None = None,
    ) -> None:
        self._memory = memory_client
        self._vault = vault_manager  # uloženo pro curator mode (Fáze 4)

        scoped_tools = [
            s for s in tool_registry.get_schemas()
            if s["name"] in GLAEDR_TOOL_WHITELIST
        ]

        async def scoped_executor(name: str, kwargs: dict) -> object:
            if name not in GLAEDR_TOOL_WHITELIST:
                return (
                    f"Tool '{name}' is not available to Glaedr. "
                    f"Permitted tools: {sorted(GLAEDR_TOOL_WHITELIST)}"
                )
            return await tool_registry.execute(name, kwargs)

        glaedr_model_override = model or os.getenv("GLAEDR_MODEL")
        if glaedr_model_override:
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            effective_client: ClaudeClient = ClaudeClient(api_key=api_key, model=glaedr_model_override)
        else:
            effective_client = claude_client

        max_iterations = int(os.getenv("GLAEDR_MAX_ITERATIONS", "8"))

        super().__init__(
            claude_client=effective_client,
            scoped_tools=scoped_tools,
            tool_executor=scoped_executor,
            name="glaedr",
            system_prompt=GLAEDR_SYSTEM_PROMPT,
            max_iterations=max_iterations,
        )

    async def retrieve(self, query: str, scope: str | None = None) -> SubagentResult:
        """
        Provede hluboké vyhledávání v paměti a vaultu a vrátí strukturovaný brief.

        Args:
            query: Co Prime potřebuje zjistit — buď konkrétní otázka nebo téma.
            scope: Volitelný hint pro zúžení: 'vault', 'memory', nebo tag/téma.
                   Pokud None, Glaedr hledá globálně.

        Returns:
            SubagentResult se summary obsahujícím brief ve formátu ## Brief / ## Findings /
            ## Gaps / ## Confidence. success=False pokud Claude selhal nebo query prázdný.
        """
        if not query.strip():
            return SubagentResult(success=False, summary="", error="Empty query")

        initial_memories: list[str] = []
        try:
            initial_memories = await self._memory.search(query, limit=15)
        except Exception:
            logger.warning("[Glaedr] Initial memory search failed for query %r, continuing without", query)

        task = self._build_glaedr_task(query, scope, initial_memories)
        return await self.run(task)

    def _build_glaedr_task(
        self, query: str, scope: str | None, memories: list[str]
    ) -> str:
        lines = [f"Query: {query}"]
        if scope:
            lines.append(f"Scope hint: {scope}")

        lines.append("")
        if memories:
            lines.append(
                "Initial memory context (semantic search results — use as a starting point, then search deeper):"
            )
            for i, mem in enumerate(memories, 1):
                lines.append(f"  [{i}] {mem}")
        else:
            lines.append(
                "No initial memories found — rely on vault_search and vault_read."
            )

        lines.append("")
        lines.append("Synthesize all findings into a structured brief.")
        return "\n".join(lines)
