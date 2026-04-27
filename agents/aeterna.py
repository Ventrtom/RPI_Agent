"""
Aeterna — specialistka na plánování a čas, subagent Prime.

schedule() mode (Fáze 3): vytváří, upravuje a ruší calendar eventy a scheduled tasky
s conflict checkingem, parsováním časových výrazů a idempotency detektorem.

review() mode (Fáze 3): read-only health check naplánovaných věcí.

Příklad použití:
    result = await aeterna.schedule("Naplánuj týdenní Apollo review každý pátek 10:00 do konce května")
    if result.success:
        print(result.summary)   # strukturovaný brief
        print(result.data)      # {"object_type": "scheduled_task", "object_id": "42", "status": "success"}
"""

import logging
import os
import re

from agents.base import BaseSubagent, SubagentResult
from llm.claude import ClaudeClient
from tools import ToolRegistry
from tools.confirmation import DANGEROUS_TOOLS

logger = logging.getLogger(__name__)

AETERNA_TOOL_WHITELIST: frozenset[str] = frozenset({
    # scheduler CRUD
    "schedule_task",
    "list_tasks",
    "get_task_details",
    "cancel_task",
    "enable_task",
    "update_task",
    # calendar CRUD
    "get_calendar_events",
    "create_calendar_event",
    "delete_calendar_event",
    "find_free_slots",
    # contacts read-only
    "get_contacts",
    "get_contact_by_name",
    # communication (for scheduled task delivery)
    "send_email",
})

AETERNA_SYSTEM_PROMPT = """You are Aeterna, a scheduling and time specialist — a subagent serving Prime.

Your role: given a scheduling intent (natural language), execute it reliably using the scheduler, calendar, and contacts tools. You own the time dimension of Prime's work.

Core principles:
- Be deterministic and verifiable. Every action you take must be confirmable by a follow-up query (e.g., after creating a task, verify it exists).
- Check before you write. Before creating a calendar event, check for conflicts in the target window using find_free_slots or get_calendar_events. Before scheduling a recurring task, check list_tasks for duplicates.
- Parse time expressions carefully. "Next Friday" means the upcoming Friday, not Friday of next week. When ambiguous (e.g., "příští týden"), pick the most natural interpretation AND state it in your output.
- Timezone: the user's timezone is Europe/Prague unless specified otherwise. All scheduled times should be in local time.
- If you encounter a conflict (overlapping event, duplicate task), DO NOT silently override. Report the conflict in your output and let Prime decide.
- Contacts: if the intent mentions a person, try get_contact_by_name first. If no match or multiple matches, proceed without and note it in output — don't block on it.
- If the intent is ambiguous (missing date, missing duration, etc.), make the most reasonable assumption and note it clearly. Do not ask clarifying questions — there is no user present in your context.
- Idempotency: if asked to schedule something that already exists (detected via list_tasks or get_calendar_events), do NOT create a duplicate. Report it as "already exists" with the existing ID.
- Always return the ID of any object you create, so Prime can reference it later.

Output format (plain text, ~100-500 words):

## Action taken
(One sentence summary of what you did or tried to do.)

## Details
- Object type: [scheduled_task / calendar_event / review_only]
- ID: <id if created or modified>
- Time: <when, in Europe/Prague>
- Description: <what>
- Participants: <if applicable>

## Assumptions made
(List explicitly any assumptions — time interpretation, missing fields filled with defaults, etc. Skip if none.)

## Conflicts detected
(List any conflicts found. Skip if none.)

## Status
(success / partial / blocked / already_exists)
- success: everything done as intended
- partial: some action taken but something else failed (describe)
- blocked: nothing done because of conflict or missing info (Prime must decide)
- already_exists: intended action was a no-op because object already existed

Be precise. Prime will use your output to inform the user or take further action."""


def _make_confirmed_executor(raw_executor, is_scheduled: bool, gate):
    """Identický pattern jako core/agent.py:_make_confirmed_executor.

    Duplikace záměrně — extrakce do sdílené utility přijde v budoucím refaktoru.
    """
    async def confirmed_execute(name: str, kwargs: dict) -> object:
        if name not in DANGEROUS_TOOLS:
            return await raw_executor(name, kwargs)

        if is_scheduled:
            logger.warning("Dangerous tool '%s' blocked in scheduled context (Aeterna)", name)
            return {
                "error": (
                    f"Tool '{name}' requires human confirmation and cannot be "
                    "executed in an automated/scheduled context. "
                    "No human is present to approve this action."
                )
            }

        if gate is None:
            logger.warning("Dangerous tool '%s' called but no confirmation gate configured (Aeterna)", name)
            return {
                "error": (
                    f"Tool '{name}' requires human confirmation, but the "
                    "confirmation system is not configured in this mode."
                )
            }

        try:
            approved = await gate.request(name, kwargs)
        except RuntimeError as exc:
            logger.warning("ConfirmationGate.request() raised in Aeterna: %s", exc)
            return {"error": str(exc)}

        if not approved:
            logger.info("Dangerous tool '%s' denied by user (or timed out) in Aeterna", name)
            return {
                "error": (
                    f"Tool '{name}' was denied by the user (or the 60-second "
                    "confirmation window expired). No action was taken."
                )
            }

        logger.info("Dangerous tool '%s' approved by user, executing (Aeterna)", name)
        return await raw_executor(name, kwargs)

    return confirmed_execute


def _parse_schedule_output(summary: str) -> dict | None:
    """Extrahuje structured data z Aternina výstupu. Failuje tiše — vrátí None."""
    data: dict = {}

    m = re.search(r"^- ID:\s*(.+)$", summary, re.MULTILINE)
    if m:
        data["object_id"] = m.group(1).strip()

    m = re.search(r"^- Object type:\s*(.+)$", summary, re.MULTILINE)
    if m:
        data["object_type"] = m.group(1).strip()

    m = re.search(
        r"^## Status\s*\n\(?(success|partial|blocked|already_exists)",
        summary,
        re.MULTILINE | re.IGNORECASE,
    )
    if m:
        data["status"] = m.group(1).strip().lower()

    return data or None


class Aeterna(BaseSubagent):
    """
    Specialistka na plánování a čas — subagent Prime.

    Volá se přes Prime tools `plan_task` a `review_my_schedule`. Oproti Glaedrovi
    a Veritasovi je write-capable: smí vytvářet, upravovat a rušit calendar eventy
    a scheduled tasky. ConfirmationGate je zapojena pro případ, že by v budoucnu
    některý z jejích tools přibyl do DANGEROUS_TOOLS.

    Scoped tools (whitelist):
        scheduler CRUD: schedule_task, list_tasks, get_task_details, cancel_task,
                        enable_task, update_task
        calendar CRUD:  get_calendar_events, create_calendar_event,
                        delete_calendar_event, find_free_slots
        contacts RO:    get_contacts, get_contact_by_name

    Aeterna nemá memory_client — kontext dostává od Prime v intent stringu.
    """

    def __init__(
        self,
        claude_client: ClaudeClient,
        tool_registry: ToolRegistry,
        internal_registry: ToolRegistry | None = None,
        confirmation_gate=None,  # tools.confirmation.ConfirmationGate | None
        model: str | None = None,
        is_scheduled_context: bool = False,
        telemetry_logger=None,
    ) -> None:
        scoped_tools = []
        for name in AETERNA_TOOL_WHITELIST:
            schema = tool_registry.get_schema(name)
            if schema is None and internal_registry is not None:
                schema = internal_registry.get_schema(name)
            if schema is None:
                logger.warning("Aeterna: tool %s not found in any registry", name)
                continue
            scoped_tools.append(schema)

        async def raw_execute(name: str, kwargs: dict) -> object:
            if tool_registry.has_tool(name):
                return await tool_registry.execute(name, kwargs)
            if internal_registry is not None and internal_registry.has_tool(name):
                return await internal_registry.execute(name, kwargs)
            raise ValueError(f"Unknown tool: {name}")

        confirmed_execute = _make_confirmed_executor(raw_execute, is_scheduled_context, confirmation_gate)

        async def scoped_executor(name: str, kwargs: dict) -> object:
            if name not in AETERNA_TOOL_WHITELIST:
                return (
                    f"Tool '{name}' is not available to Aeterna. "
                    f"Permitted tools: {sorted(AETERNA_TOOL_WHITELIST)}"
                )
            return await confirmed_execute(name, kwargs)

        aeterna_model_override = model or os.getenv("AETERNA_MODEL")
        if aeterna_model_override:
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            effective_client: ClaudeClient = ClaudeClient(api_key=api_key, model=aeterna_model_override)
        else:
            effective_client = claude_client

        max_iterations = int(os.getenv("AETERNA_MAX_ITERATIONS", "12"))

        super().__init__(
            claude_client=effective_client,
            scoped_tools=scoped_tools,
            tool_executor=scoped_executor,
            name="aeterna",
            system_prompt=AETERNA_SYSTEM_PROMPT,
            max_iterations=max_iterations,
            telemetry_logger=telemetry_logger,
        )

    async def schedule(self, intent: str, context: dict | None = None) -> SubagentResult:
        """
        Provede scheduling intent: vytvoří/upraví/zruší task nebo calendar event.

        Args:
            intent: Přirozený popis toho, co naplánovat. Příklady:
                    "Schedule weekly project review every Friday 10am until end of May"
                    "Cancel the recurring task with id 42"
                    "Move my 2pm meeting to 4pm"
            context: Volitelný dict s předpočítanými informacemi
                     (např. {'contact_email': 'jana@example.com', 'related_event_id': 'abc'}).

        Returns:
            SubagentResult se summary (Aternin output) a data dict
            {"object_type", "object_id", "status"} pokud se podaří parsovat.
            data=None pokud výstup neodpovídá očekávanému formátu.
        """
        if not intent.strip():
            return SubagentResult(success=False, summary="", error="Empty intent")

        task_prompt = self._build_schedule_task(intent, context)
        result = await self.run(task_prompt, _method="schedule")

        if result.success:
            result.data = _parse_schedule_output(result.summary)

        return result

    async def review(self, scope: str | None = None) -> SubagentResult:
        """
        Read-only přehled naplánovaných tasků a nadcházejících calendar eventů.

        Args:
            scope: Volitelný filtr — "today", "this week", "tasks", "calendar",
                   "overdue", "next 3 days". Bez filtru = obecný přehled.

        Returns:
            SubagentResult se summary (lidsky čitelný přehled).
            data = {"tasks_count": N, "events_count": M, "issues": [...]} pokud parsovatelné.
        """
        task_prompt = self._build_review_task(scope)
        result = await self.run(task_prompt, _method="review")
        return result

    def _build_schedule_task(self, intent: str, context: dict | None) -> str:
        lines = [f"Scheduling intent: {intent}"]

        if context:
            lines.append("")
            lines.append("Precomputed context (use directly, no need to look up):")
            for k, v in context.items():
                lines.append(f"  {k}: {v}")

        lines.append("")
        lines.append("Execute the intent. Check for duplicates/conflicts before writing.")
        return "\n".join(lines)

    def _build_review_task(self, scope: str | None) -> str:
        lines = ["Review the current schedule and upcoming calendar events."]

        if scope:
            lines.append(f"Scope filter: {scope}")

        lines.append("")
        lines.append(
            "Do not modify anything. Only report. "
            "List active tasks and upcoming events, flag any issues "
            "(overdue tasks, conflicts, tasks near end_at expiry)."
        )
        return "\n".join(lines)
