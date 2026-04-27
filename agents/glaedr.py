"""
Glaedr — paměťový a znalostní specialista, subagent Prime.

Retrieve mode (Fáze 1): hluboké vyhledávání v Mem0 + ChromaDB a vaultu,
syntéza výsledků do strukturovaného briefu pro Prime.

Curator mode (Fáze 4): proaktivní analýza paměti — detekce duplikátů,
návrhy tagů, týdenní digest do vaultu. Curator nikdy nezapisuje do paměti
(Mem0/ChromaDB) — všechny akce jsou návrhy v digestu.

Příklad použití:
    result = await glaedr.retrieve("rozhodnutí o cache invalidation v projektu Apollo")
    if result.success:
        print(result.summary)   # strukturovaný brief

    result = await glaedr.curate(scope="week", dry_run=True)
    if result.success:
        print(result.summary)   # markdown digest
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime

from agents.base import BaseSubagent, SubagentResult
from llm.claude import ClaudeClient
from memory.client import MemoryClient
from tools import ToolRegistry
from vault.vault_manager import VaultManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retrieve mode
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Curator mode
# ---------------------------------------------------------------------------

GLAEDR_CURATOR_TOOL_WHITELIST: frozenset[str] = frozenset(
    {"vault_read", "vault_search", "vault_write"}
)

GLAEDR_CURATOR_SYSTEM_PROMPT = """You are Glaedr in curator mode — a memory curator and knowledge custodian. Your role right now is NOT to answer user queries, but to review the user's long-term memory, identify patterns, propose housekeeping actions, and produce a human-readable digest.

You will receive a batch of memories (extracted facts from past conversations). Analyze them carefully.

Core principles:
- Be a custodian, not an editor. You PROPOSE actions, you do NOT execute destructive changes to memory.
- Focus on signal over noise. A good digest surfaces what matters — recurring themes, important facts, changes over time.
- Detect potential duplicates or contradictions. Two memories saying "user works at X" with different X values is a conflict — flag it.
- Suggest tags for memories that would benefit from categorization: work, personal, project:NAME, interest:TOPIC. Do not invent tags excessively — prefer a small consistent set.
- Spot stale information. "User is planning trip to Rome next month" from 6 months ago is likely outdated.
- Be concise. A digest is not an essay.

Output format (strict markdown, will be written to vault):

---
summary: "Memory digest {period}"
generated: "{timestamp}"
memory_count: {total}
new_since_last_run: {delta}
---

# Memory Digest — {period}

## Overview
(3-5 sentence summary of what's in memory: main themes, activity patterns, anything notable)

## Key Themes
Grouped by topic. Each theme has 2-4 representative memories.

### Theme: {name}
- Memory text [potentially relevant tag: #work]
- Memory text [potentially relevant tag: #project:apollo]
- ...

### Theme: {name}
...

## Potential Duplicates
List any pairs/groups that seem to refer to the same fact but are stored separately.
- Group 1:
  - "User works at Acme"
  - "Tomáš is employed at Acme Corp"
  (likely same fact — consider consolidation)
- ...

Skip section if none detected.

## Conflicts Detected
List memories that appear to contradict each other.
- "User uses VS Code" vs "User prefers Vim" — timing unclear
- ...

Skip if none.

## Suggested Tags
Memories that would benefit from tagging, grouped by proposed tag.

### #work
- "User is preparing Q4 review for stakeholders"
- ...

### #project:apollo
- ...

Skip if no strong suggestions.

## Possibly Stale
Memories that reference time-bound events that have likely passed.
- "User is planning trip to Rome next month" (added 6 months ago — trip probably over)
- ...

Skip if none.

## Housekeeping Notes
Any other observations worth the user's attention: unusual patterns, gaps, suggestions for how memory is being used.

---

Remember: all these are PROPOSALS for the user to review. You do not modify memory."""

# ---------------------------------------------------------------------------
# Curator state helpers
# ---------------------------------------------------------------------------

_DEFAULT_CURATOR_STATE: dict = {
    "last_run_at": None,
    "last_memory_count": 0,
    "total_runs": 0,
    "last_digest_path": None,
}


def _load_curator_state() -> dict:
    state_path = os.getenv("CURATOR_STATE_PATH", "./data/curator_state.json")
    try:
        with open(state_path) as f:
            loaded = json.load(f)
        return {**_DEFAULT_CURATOR_STATE, **loaded}
    except FileNotFoundError:
        return {**_DEFAULT_CURATOR_STATE}
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("[Glaedr curator] State file unreadable (%s), using defaults", exc)
        return {**_DEFAULT_CURATOR_STATE}


def _save_curator_state(state: dict) -> None:
    state_path = os.getenv("CURATOR_STATE_PATH", "./data/curator_state.json")
    tmp_path = state_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, state_path)
    except Exception as exc:
        logger.error("[Glaedr curator] Failed to save state: %s", exc)


def _estimate_digest_counts(digest: str) -> tuple[int, int]:
    """Estimate duplicate groups and tag sections from digest markdown."""
    duplicates = 0
    tags = 0
    in_duplicates = False
    in_tags = False
    for line in digest.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Potential Duplicates"):
            in_duplicates = True
            in_tags = False
        elif stripped.startswith("## Suggested Tags"):
            in_tags = True
            in_duplicates = False
        elif stripped.startswith("## "):
            in_duplicates = False
            in_tags = False
        elif in_duplicates and stripped.startswith("- Group"):
            duplicates += 1
        elif in_tags and stripped.startswith("### #"):
            tags += 1
    return duplicates, tags


def _build_curator_task(
    memories: list[str],
    total: int,
    new: int,
    scope: str | None,
    dry_run: bool,
    now_str: str,
    year: int,
    week: int,
) -> str:
    period = f"{year}-W{week:02d}"
    lines = [
        f"Period: {period}",
        f"Current time: {now_str}",
        f"Total memories in store: {total}",
        f"New since last curator run: {new}",
    ]
    if scope:
        lines.append(f"Scope hint: {scope}")
    if dry_run:
        lines.append("Mode: DRY RUN — do not call vault_write, just produce the digest text.")
    lines.append("")

    if memories:
        lines.append("## Memories")
        lines.append("")
        for i, mem in enumerate(memories, 1):
            lines.append(f"{i}. {mem}")
    else:
        lines.append("## Memories")
        lines.append("")
        lines.append("(no memories stored yet)")

    lines.append("")
    lines.append(
        "Analyze the memories above and produce a structured digest per the output format "
        "in your system prompt. Fill in the placeholders: period, timestamp, memory_count, "
        "new_since_last_run."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Glaedr subagent
# ---------------------------------------------------------------------------


class Glaedr(BaseSubagent):
    """
    Paměťový a znalostní specialista — subagent Prime.

    Retrieve mode: delegován přes `memory_dive` pro dotazy vyžadující syntézu
    přes více pamětí nebo vault souborů.

    Curator mode: delegován přes `memory_housekeeping` pro proaktivní
    analýzu paměti a produkci digestu. Curator nikdy nemění paměť.
    """

    def __init__(
        self,
        claude_client: ClaudeClient,
        memory_client: MemoryClient,
        vault_manager: VaultManager,
        tool_registry: ToolRegistry,
        internal_registry: ToolRegistry | None = None,
        model: str | None = None,
        notifier=None,
        telemetry_logger=None,
    ) -> None:
        self._memory = memory_client
        self._vault = vault_manager
        self._notifier = notifier

        # --- retrieve executor (whitelist: vault_read, vault_search) ---
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

        # --- curator executor (adds vault_write restricted to memory-digests/) ---
        async def curator_executor(name: str, kwargs: dict) -> object:
            if name not in GLAEDR_CURATOR_TOOL_WHITELIST:
                return (
                    f"Tool '{name}' is not available to Glaedr curator mode. "
                    f"Permitted tools: {sorted(GLAEDR_CURATOR_TOOL_WHITELIST)}"
                )
            if name == "vault_write":
                path = kwargs.get("path", "")
                if not path.startswith("memory-digests/"):
                    return (
                        f"Error: curator vault_write is restricted to the memory-digests/ "
                        f"directory. Path '{path}' rejected."
                    )
            return await tool_registry.execute(name, kwargs)

        self._curator_scoped_tools = [
            s for s in tool_registry.get_schemas()
            if s["name"] in GLAEDR_CURATOR_TOOL_WHITELIST
        ]
        self._curator_tool_executor = curator_executor

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
            telemetry_logger=telemetry_logger,
        )

    # ------------------------------------------------------------------
    # Retrieve mode
    # ------------------------------------------------------------------

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
        return await self.run(task, _method="retrieve")

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

    # ------------------------------------------------------------------
    # Curator mode
    # ------------------------------------------------------------------

    async def curate(self, scope: str | None = None, dry_run: bool = False) -> SubagentResult:
        """
        Curator mode — analýza paměti, detekce duplikátů, návrhy tagů, digest do vaultu.

        Args:
            scope: Volitelný hint: 'week' (default), 'month', 'all', nebo tag/téma.
            dry_run: Pokud True, curator analyzuje a vrátí digest bez zápisu do vaultu
                     a bez update state souboru.

        Returns:
            SubagentResult se summary obsahujícím markdown digest.
            data obsahuje: digest_path, memory_count, duplicates_found, tags_suggested,
            new_since_last_run.
        """
        trace_id = str(uuid.uuid4())
        start = time.monotonic()

        # 1. Načíst state
        state = _load_curator_state()

        # 2. Stáhnout všechny memories
        all_memories: list[str] = []
        try:
            all_memories = await self._memory.get_all()
        except Exception:
            logger.warning("[Glaedr curator:%s] get_all() failed, proceeding with empty list", trace_id)

        memory_count = len(all_memories)
        new_since_last_run = max(0, memory_count - state.get("last_memory_count", 0))

        # 3. Batch limit
        batch_size = int(os.getenv("CURATOR_BATCH_SIZE", "50"))
        if memory_count > batch_size:
            logger.warning(
                "[Glaedr curator:%s] Memory count %d > batch_size %d, using first %d",
                trace_id, memory_count, batch_size, batch_size,
            )
            processed = all_memories[:batch_size]
        else:
            processed = all_memories

        # 4. Digest cesta (ISO week)
        iso_cal = datetime.now().isocalendar()
        digest_path = f"memory-digests/{iso_cal.year}-W{iso_cal.week:02d}.md"

        # 5. Sestavit task message s memories
        now_str = datetime.now().isoformat()
        task_text = _build_curator_task(
            processed, memory_count, new_since_last_run, scope, dry_run,
            now_str, iso_cal.year, iso_cal.week,
        )

        # 6. Zavolat Claude s curator promptem
        max_iters = int(os.getenv("GLAEDR_CURATOR_MAX_ITERATIONS", "6"))
        iteration_count = 0
        tool_calls_count = 0

        async def limited_executor(name: str, kwargs: dict) -> object:
            nonlocal iteration_count, tool_calls_count
            iteration_count += 1
            tool_calls_count += 1
            if iteration_count > max_iters:
                raise RuntimeError(f"Curator iteration limit ({max_iters}) exceeded")
            return await self._curator_tool_executor(name, kwargs)

        messages = [{"role": "user", "content": task_text}]
        curator_system = [
            {"type": "text", "text": GLAEDR_CURATOR_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]
        try:
            digest_text = await self.claude_client.complete(
                system=curator_system,
                messages=messages,
                tools=self._curator_scoped_tools or None,
                tool_executor=limited_executor,
                max_tokens=4096,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.exception("[Glaedr curator:%s] Claude call failed", trace_id)
            return SubagentResult(
                success=False,
                summary="",
                error=str(exc),
                metadata={"trace_id": trace_id, "latency_ms": latency_ms},
            )

        duplicates, tags = _estimate_digest_counts(digest_text)

        if not dry_run:
            # 7. Zapsat digest do vaultu
            try:
                await asyncio.to_thread(self._vault.write, digest_path, digest_text)
                logger.info("[Glaedr curator:%s] Digest written → vault/%s", trace_id, digest_path)
            except Exception as exc:
                logger.error("[Glaedr curator:%s] vault.write failed: %s", trace_id, exc)
                return SubagentResult(
                    success=False,
                    summary="",
                    error=f"Failed to write digest to vault: {exc}",
                    metadata={"trace_id": trace_id, "latency_ms": latency_ms},
                )

            # 8. Aktualizovat state
            _save_curator_state({
                "last_run_at": now_str,
                "last_memory_count": memory_count,
                "total_runs": state.get("total_runs", 0) + 1,
                "last_digest_path": digest_path,
            })

            # 9. Telegram notifikace
            if self._notifier is not None and self._notifier.ready:
                msg = (
                    f"🧹 Glaedr dokončil memory housekeeping.\n"
                    f"Digest: vault/{digest_path}\n"
                    f"{memory_count} memories, {duplicates} potenciálních duplikátů, "
                    f"{tags} návrhů tagů."
                )
                try:
                    await self._notifier.send(msg)
                except Exception:
                    logger.warning("[Glaedr curator:%s] Telegram notification failed", trace_id)

        result = SubagentResult(
            success=True,
            summary=digest_text,
            data={
                "digest_path": digest_path if not dry_run else None,
                "memory_count": memory_count,
                "duplicates_found": duplicates,
                "tags_suggested": tags,
                "new_since_last_run": new_since_last_run,
            },
            metadata={
                "trace_id": trace_id,
                "latency_ms": latency_ms,
                "iterations": iteration_count,
                "tool_calls_count": tool_calls_count,
                "dry_run": dry_run,
            },
        )

        if self._telemetry_logger:
            from observability.telemetry import _current_session_id
            sid = _current_session_id.get("")
            try:
                await self._telemetry_logger.log_delegation(
                    subagent="glaedr",
                    method="curate",
                    task=task_text[:200],
                    result=result,
                    session_id=sid,
                )
            except Exception:
                logger.warning("[Glaedr curator] Telemetry log_delegation selhal")

        return result
