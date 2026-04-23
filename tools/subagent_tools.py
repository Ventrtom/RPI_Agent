"""
Tools for delegating tasks from Prime to subagents (Glaedr, Aeterna, Veritas).

- Phase 1: memory_dive (Glaedr retrieve)           ← implementováno
- Phase 2: deep_research (Veritas)                  ← implementováno
- Phase 3: plan_task, review_my_schedule (Aeterna)  ← implementováno
- Phase 4: memory_housekeeping (Glaedr curator)     ← implementováno
"""

from collections.abc import Callable

from agents.aeterna import Aeterna
from agents.glaedr import Glaedr
from agents.veritas import Veritas

MEMORY_DIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "The question or topic to research in long-term memory and vault. "
                "Be specific — Glaedr performs deep search, not a quick lookup. "
                "Use this for complex queries that need synthesis across multiple "
                "memories/notes, not for simple fact retrieval."
            ),
        },
        "scope": {
            "type": "string",
            "description": (
                "Optional scope hint: 'vault', 'memory', or a tag/topic to narrow "
                "the search. Omit for global search."
            ),
        },
    },
    "required": ["query"],
}


def make_memory_dive_tool(glaedr: Glaedr) -> Callable:
    """
    Vytvoří tool funkci memory_dive vázanou na danou instanci Glaedra.

    Factory pattern zaručuje, že tool má referenci na konkrétní instanci
    a může být registrován v ToolRegistry bez module-level singletonu.
    """

    async def memory_dive(query: str, scope: str | None = None) -> dict:
        """
        Delegate a deep memory/knowledge query to Glaedr, the memory specialist subagent.

        Use this when:
        - The user asks a complex question that requires searching and synthesizing across multiple memories or notes
        - You need to recall detailed context about a past project, person, or topic
        - A simple memory lookup (already provided in your system prompt) is insufficient

        Do NOT use this for:
        - Simple factual recall that's already visible in your memory context
        - Quick vault_read when you know the exact file path — use vault_read directly

        Glaedr returns a structured brief with findings, sources, and confidence level.
        You can then decide what to do with the information.
        """
        result = await glaedr.retrieve(query, scope)
        if result.success:
            return {
                "success": True,
                "summary": result.summary,
                "metadata": result.metadata,
            }
        return {
            "success": False,
            "error": result.error or "Glaedr failed without a specific error",
            "metadata": result.metadata,
        }

    return memory_dive


DEEP_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": (
                "The research question or topic. Be specific and actionable — "
                "Veritas performs multi-source deep research, not a single quick lookup. "
                "Good topic: 'Current best practices for memory curation in personal AI agents 2026'. "
                "Poor topic: 'tell me about AI'."
            ),
        },
        "scope": {
            "type": "string",
            "description": (
                "Optional scope: 'web' (external only), 'internal' (vault+memory only), "
                "or a topical tag. Omit for combined multi-source research."
            ),
        },
    },
    "required": ["topic"],
}


def make_deep_research_tool(veritas: Veritas) -> Callable:
    """
    Vytvoří tool funkci deep_research vázanou na danou instanci Veritase.

    Factory pattern zaručuje, že tool má referenci na konkrétní instanci
    a může být registrován v ToolRegistry bez module-level singletonu.
    """

    async def deep_research(topic: str, scope: str | None = None) -> dict:
        """
        Delegate a complex research task to Veritas, the research & synthesis specialist subagent.

        Use this when:
        - The user asks a question requiring synthesis across MULTIPLE sources (web + internal knowledge)
        - You need current/external information combined with existing context from vault or memory
        - The topic requires comparing perspectives, validating claims, or triangulating across source types
        - A single web_search + reading snippets would not be enough

        Do NOT use this for:
        - Simple current-info lookups (weather, time, single fact) — use web_search directly
        - Memory-only queries — use memory_dive (Glaedr) instead
        - Things you already have adequate context for

        Veritas returns a structured brief with findings, sources, conflicts, and confidence level.
        Expect latency of 5-15 seconds due to multiple searches.
        """
        result = await veritas.research(topic, scope)
        if result.success:
            return {
                "success": True,
                "summary": result.summary,
                "metadata": result.metadata,
            }
        return {
            "success": False,
            "error": result.error or "Veritas failed without a specific error",
            "metadata": result.metadata,
        }

    return deep_research


PLAN_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "description": (
                "Natural language description of the scheduling action. Examples: "
                "'Schedule weekly project review every Friday 10am until end of May', "
                "'Add calendar event tomorrow 3pm dentist appointment', "
                "'Cancel the recurring task with id 42', "
                "'Move my 2pm meeting to 4pm'. "
                "Be specific about time, recurrence, participants, and duration when relevant."
            ),
        },
        "context": {
            "type": "object",
            "description": (
                "Optional precomputed context. Use when you already know specific details "
                "Aeterna would otherwise need to look up. "
                "Example: {'contact_email': 'jana@example.com', 'related_event_id': 'abc123'}. "
                "Omit when Aeterna should figure it out herself."
            ),
        },
    },
    "required": ["intent"],
}


def make_plan_task_tool(aeterna: Aeterna) -> Callable:
    """
    Vytvoří tool funkci plan_task vázanou na danou instanci Aeterny.

    Factory pattern zaručuje, že tool má referenci na konkrétní instanci
    a může být registrován v ToolRegistry bez module-level singletonu.
    """

    async def plan_task(intent: str, context: dict | None = None) -> dict:
        """
        Delegate a scheduling/calendar action to Aeterna, the time specialist subagent.

        Use this when:
        - The user asks to schedule, reschedule, cancel, or modify a task or calendar event
        - The request involves parsing natural-language time expressions ("next Friday", "every other week", "by end of month")
        - The action involves conflict checking, recurrence logic, or multi-step scheduling
        - You want to verify a scheduling operation completed correctly (Aeterna checks after writing)

        Do NOT use this for:
        - Simple read-only queries about schedule — use get_calendar_events or list_tasks directly
        - Single one-off calendar lookups — use the atomic tool
        - Actions that don't involve time/scheduling

        Aeterna returns a structured result including the ID of any object created. If the action
        was blocked by a conflict or is ambiguous, the status will reflect that.
        """
        result = await aeterna.schedule(intent, context)
        if result.success:
            return {
                "success": True,
                "summary": result.summary,
                "data": result.data,
                "metadata": result.metadata,
            }
        return {
            "success": False,
            "error": result.error or "Aeterna failed without a specific error",
            "metadata": result.metadata,
        }

    return plan_task


REVIEW_SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "description": (
                "Optional scope filter. Examples: 'today', 'this week', 'tasks', "
                "'calendar', 'overdue', 'next 3 days'. "
                "Omit for a general overview of active tasks and upcoming events."
            ),
        },
    },
    "required": [],
}


def make_review_schedule_tool(aeterna: Aeterna) -> Callable:
    """
    Vytvoří tool funkci review_my_schedule vázanou na danou instanci Aeterny.

    Factory pattern zaručuje, že tool má referenci na konkrétní instanci
    a může být registrován v ToolRegistry bez module-level singletonu.
    """

    async def review_my_schedule(scope: str | None = None) -> dict:
        """
        Delegate a schedule review/health-check to Aeterna. Returns a structured overview
        of active tasks and upcoming events, including any issues (overdue tasks, conflicts,
        tasks near end_at expiry).

        Use this when:
        - The user asks for an overview of their schedule ("what's on my plate this week?")
        - The user wants to check for problems ("any issues with my scheduled tasks?")
        - You need comprehensive time-context before making a decision

        Do NOT use this for:
        - Fetching a specific event or task — use the atomic tool
        - When you already have the context from a recent query

        Read-only: this never modifies anything.
        """
        result = await aeterna.review(scope)
        if result.success:
            return {
                "success": True,
                "summary": result.summary,
                "data": result.data,
                "metadata": result.metadata,
            }
        return {
            "success": False,
            "error": result.error or "Aeterna failed without a specific error",
            "metadata": result.metadata,
        }

    return review_my_schedule


MEMORY_HOUSEKEEPING_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "description": (
                "Optional scope: 'week' (default, memories since last curator run), "
                "'month' (last 30 days worth, approx), 'all' (entire memory), "
                "or a specific tag/topic. Omit for default weekly scope."
            ),
        },
        "dry_run": {
            "type": "boolean",
            "description": (
                "If true, curator analyzes and returns digest without writing to vault "
                "or updating state. Use for preview. Default false."
            ),
            "default": False,
        },
    },
    "required": [],
}


def make_memory_housekeeping_tool(glaedr: Glaedr) -> Callable:
    """
    Vytvoří tool funkci memory_housekeeping vázanou na danou instanci Glaedra.

    Factory pattern zaručuje, že tool má referenci na konkrétní instanci
    a může být registrován v ToolRegistry bez module-level singletonu.
    """

    async def memory_housekeeping(scope: str | None = None, dry_run: bool = False) -> dict:
        """
        Trigger Glaedr's curator mode — a manual memory housekeeping run. Glaedr reviews
        the long-term memory, identifies themes, detects duplicates/conflicts, suggests tags,
        and writes a structured digest to the vault under memory-digests/.

        Use this when:
        - The user explicitly asks for a memory review or housekeeping
          ("jak to vypadá v paměti?", "udělej kontrolu paměti", "dej mi přehled toho, co si pamatuješ")
        - You want a dry_run preview before applying anything (set dry_run=true)
        - The user suspects stale or conflicting info in memory

        Do NOT use this for:
        - Simple memory recall — use memory_dive (Glaedr retrieve) instead
        - Routine queries — the curator runs automatically weekly in the background

        Returns a digest (markdown path + inline summary). The digest contains PROPOSALS only —
        no memory is modified autonomously.
        """
        result = await glaedr.curate(scope, dry_run)
        if result.success:
            return {
                "success": True,
                "summary": result.summary,
                "data": result.data,
                "metadata": result.metadata,
            }
        return {
            "success": False,
            "error": result.error or "Curator failed without a specific error",
            "metadata": result.metadata,
        }

    return memory_housekeeping
