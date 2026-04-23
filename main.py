"""
Osobní AI Agent — entry point.

Použití:
  python main.py cli       # Spustí CLI interface
  python main.py telegram  # Spustí Telegram bot
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from logger import setup_logging  # noqa: E402

setup_logging()

import logging  # noqa: E402

logger = logging.getLogger(__name__)


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        logger.critical("Chybí povinná proměnná prostředí: %s", key)
        sys.exit(1)
    return value


async def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("cli", "telegram"):
        logger.error("Použití: python main.py [cli|telegram]")
        sys.exit(1)

    mode = sys.argv[1]

    # --- konfigurace ---
    anthropic_api_key = _require("ANTHROPIC_API_KEY")
    chroma_path = os.getenv("CHROMA_DB_PATH", "./data/chroma")
    session_db_path = os.getenv("SESSION_DB_PATH", "./data/sessions.db")
    mem0_user_id = os.getenv("MEM0_USER_ID", "tomas")
    session_timeout = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    logger.info("RPI Agent — model=%s  chroma=%s  mode=%s", model, chroma_path, mode)

    # --- závislosti ---
    from core.agent import Agent
    from core.session import SessionManager
    from core.session_store import SessionStore
    from llm.claude import ClaudeClient
    from memory.client import MemoryClient
    from tools import ToolRegistry
    from tools.system_tools import (
        GET_AGENT_LOGS_SCHEMA,
        SHUTDOWN_SCHEMA,
        get_agent_logs,
        get_system_status,
        restart_agent_service,
        shutdown_raspberry_pi,
        )
    from tools.google_tools import (
        GET_CALENDAR_EVENTS_SCHEMA,
        CREATE_CALENDAR_EVENT_SCHEMA,
        SEND_EMAIL_SCHEMA,
        DELETE_CALENDAR_EVENT_SCHEMA,
        FIND_FREE_SLOTS_SCHEMA,
        get_calendar_events,
        create_calendar_event,
        send_email,
        delete_calendar_event,
        find_free_slots,
        )
    from tools.contact_tools import (
        GET_CONTACTS_SCHEMA,
        GET_CONTACT_BY_NAME_SCHEMA,
        ADD_CONTACT_SCHEMA,
        REMOVE_CONTACT_SCHEMA,
        get_contacts,
        get_contact_by_name,
        add_contact,
        remove_contact,
        )
    from interfaces.notifier import TelegramNotifier
    from tools.confirmation import ConfirmationGate
    from scheduler import TaskScheduler, TaskStore
    from tools.telegram_tools import (
        SEND_TELEGRAM_MESSAGE_SCHEMA,
        init_telegram_tools,
        send_telegram_message,
        )
    from tools.scheduler_tools import (
        SCHEDULE_TASK_SCHEMA,
        LIST_TASKS_SCHEMA,
        GET_TASK_DETAILS_SCHEMA,
        CANCEL_TASK_SCHEMA,
        ENABLE_TASK_SCHEMA,
        UPDATE_TASK_SCHEMA,
        init_scheduler_tools,
        schedule_task,
        list_tasks,
        get_task_details,
        cancel_task,
        enable_task,
        update_task,
        )
    from tools.web_tools import (
        WEB_SEARCH_SCHEMA,
        web_search,
        )
    from tools.self_tools import (
        GET_SELF_INFO_SCHEMA,
        init_self_tools,
        get_self_info,
        )
    from tools.source_tools import (
        LIST_OWN_SOURCE_SCHEMA,
        READ_OWN_SOURCE_SCHEMA,
        init_source_tools,
        list_own_source,
        read_own_source,
        )
    from vault.vault_manager import VaultManager
    from vault.indexer import VaultIndexer
    from vault.tools import (
        VAULT_READ_SCHEMA,
        VAULT_WRITE_SCHEMA,
        VAULT_SEARCH_SCHEMA,
        init_vault_tools,
        vault_read,
        vault_write,
        vault_search,
        )
    from reasoning.engine import ReasoningEngine
    from tools.voice_tools import SET_VOICE_PROFILE_SCHEMA, set_voice_profile

    registry = ToolRegistry()
    registry.register(get_system_status)
    registry.register(get_agent_logs, GET_AGENT_LOGS_SCHEMA)
    registry.register(restart_agent_service)
    registry.register(shutdown_raspberry_pi, SHUTDOWN_SCHEMA)
    registry.register(get_calendar_events, GET_CALENDAR_EVENTS_SCHEMA)
    registry.register(create_calendar_event, CREATE_CALENDAR_EVENT_SCHEMA)
    registry.register(send_email, SEND_EMAIL_SCHEMA)
    registry.register(get_contacts, GET_CONTACTS_SCHEMA)
    registry.register(get_contact_by_name, GET_CONTACT_BY_NAME_SCHEMA)
    registry.register(add_contact, ADD_CONTACT_SCHEMA)
    registry.register(remove_contact, REMOVE_CONTACT_SCHEMA)
    registry.register(delete_calendar_event, DELETE_CALENDAR_EVENT_SCHEMA)
    registry.register(find_free_slots, FIND_FREE_SLOTS_SCHEMA)
    registry.register(send_telegram_message, SEND_TELEGRAM_MESSAGE_SCHEMA)
    registry.register(schedule_task, SCHEDULE_TASK_SCHEMA)
    registry.register(list_tasks, LIST_TASKS_SCHEMA)
    registry.register(get_task_details, GET_TASK_DETAILS_SCHEMA)
    registry.register(cancel_task, CANCEL_TASK_SCHEMA)
    registry.register(enable_task, ENABLE_TASK_SCHEMA)
    registry.register(update_task, UPDATE_TASK_SCHEMA)
    registry.register(web_search, WEB_SEARCH_SCHEMA)
    registry.register(get_self_info, GET_SELF_INFO_SCHEMA)
    registry.register(list_own_source, LIST_OWN_SOURCE_SCHEMA)
    registry.register(read_own_source, READ_OWN_SOURCE_SCHEMA)
    registry.register(vault_read, VAULT_READ_SCHEMA)
    registry.register(vault_write, VAULT_WRITE_SCHEMA)
    registry.register(vault_search, VAULT_SEARCH_SCHEMA)
    registry.register(set_voice_profile, SET_VOICE_PROFILE_SCHEMA)
    logger.info("Tools registered: %s", [fn.__name__ for fn in registry.get_all()])

    tasks_db_path = os.getenv("TASKS_DB_PATH", "./data/tasks.db")
    scheduler_tz = os.getenv("SCHEDULER_TIMEZONE", "Europe/Prague")

    vault_path = Path(os.getenv("VAULT_PATH", "./vault"))
    vault_manager = VaultManager(base_path=vault_path)
    vault_manager.rebuild_index()
    vault_indexer = VaultIndexer(vault_manager)
    vault_indexer.start()
    init_vault_tools(vault_manager)
    logger.info("Vault initialised at %s", vault_path)

    memory_client = MemoryClient(user_id=mem0_user_id, chroma_path=chroma_path)
    claude_client = ClaudeClient(api_key=anthropic_api_key, model=model)

    from agents.glaedr import Glaedr
    from tools.subagent_tools import make_memory_dive_tool, MEMORY_DIVE_SCHEMA

    glaedr = Glaedr(
        claude_client=claude_client,
        memory_client=memory_client,
        vault_manager=vault_manager,
        tool_registry=registry,
    )
    memory_dive = make_memory_dive_tool(glaedr)
    registry.register(memory_dive, MEMORY_DIVE_SCHEMA)
    logger.info("Glaedr subagent initialised, memory_dive tool registered")

    from agents.veritas import Veritas
    from tools.subagent_tools import make_deep_research_tool, DEEP_RESEARCH_SCHEMA

    veritas = Veritas(
        claude_client=claude_client,
        memory_client=memory_client,
        vault_manager=vault_manager,
        tool_registry=registry,
    )
    deep_research = make_deep_research_tool(veritas)
    registry.register(deep_research, DEEP_RESEARCH_SCHEMA)
    logger.info("Veritas subagent initialised, deep_research tool registered")

    session_store = SessionStore(db_path=session_db_path)
    session_manager = SessionManager(timeout_minutes=session_timeout, store=session_store)

    reasoning_engine = ReasoningEngine(claude_client, vault_manager=vault_manager)
    logger.info("ReasoningEngine initialised (MAX_ITERATIONS=%d)", ReasoningEngine.MAX_ITERATIONS)

    notifier = TelegramNotifier()
    confirmation_gate = ConfirmationGate(notifier)

    from agents.aeterna import Aeterna
    from tools.subagent_tools import (
        make_plan_task_tool, PLAN_TASK_SCHEMA,
        make_review_schedule_tool, REVIEW_SCHEDULE_SCHEMA,
    )

    aeterna = Aeterna(
        claude_client=claude_client,
        tool_registry=registry,
        confirmation_gate=confirmation_gate,
    )
    plan_task = make_plan_task_tool(aeterna)
    review_my_schedule = make_review_schedule_tool(aeterna)
    registry.register(plan_task, PLAN_TASK_SCHEMA)
    registry.register(review_my_schedule, REVIEW_SCHEDULE_SCHEMA)
    logger.info("Aeterna subagent initialised, plan_task + review_my_schedule registered")

    agent = Agent(
        memory_client=memory_client,
        claude_client=claude_client,
        session_manager=session_manager,
        tool_registry=registry,
        reasoning_engine=reasoning_engine,
        confirmation_gate=confirmation_gate,
    )
    # Home Assistant (optional — silently skipped if HA_URL / HA_TOKEN not set)
    ha_url = os.getenv("HA_URL")
    ha_token = os.getenv("HA_TOKEN")
    if ha_url and ha_token:
        from tools.ha_client import HAClient
        from tools.ha_tools import (
            HA_CALL_SERVICE_SCHEMA,
            HA_GET_HISTORY_SCHEMA,
            HA_GET_STATE_SCHEMA,
            HA_LIST_ENTITIES_SCHEMA,
            ha_call_service,
            ha_get_history,
            ha_get_state,
            ha_list_entities,
            init_ha_tools,
        )
        ha_timeout = float(os.getenv("HA_TIMEOUT", "10"))
        ha_client = HAClient(base_url=ha_url, token=ha_token, timeout=ha_timeout)
        init_ha_tools(ha_client)
        registry.register(ha_list_entities, HA_LIST_ENTITIES_SCHEMA)
        registry.register(ha_get_state, HA_GET_STATE_SCHEMA)
        registry.register(ha_call_service, HA_CALL_SERVICE_SCHEMA)
        registry.register(ha_get_history, HA_GET_HISTORY_SCHEMA)
        logger.info("Home Assistant tools registered (%s)", ha_url)

    init_telegram_tools(notifier)
    init_self_tools(claude_client, memory_client, registry)
    init_source_tools(Path(__file__).parent)

    task_store = TaskStore(db_path=tasks_db_path)
    init_scheduler_tools(task_store, scheduler_tz)
    scheduler = TaskScheduler(task_store, agent, user_id=mem0_user_id, timezone_name=scheduler_tz)
    scheduler.start()
    logger.info("TaskScheduler started (tz=%s, db=%s)", scheduler_tz, tasks_db_path)

    try:
        if mode == "cli":
            from interfaces.cli import run_cli
            await run_cli(agent, user_id=mem0_user_id)

        elif mode == "telegram":
            telegram_token = _require("TELEGRAM_BOT_TOKEN")
            elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY")
            elevenlabs_voice_id = os.getenv("ELEVENLABS_VOICE_ID")
            groq_api_key = os.getenv("GROQ_API_KEY")
            elevenlabs_model = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
            whisper_model = os.getenv("WHISPER_MODEL") or None
            whisper_language = os.getenv("WHISPER_LANGUAGE") or None

            from interfaces.telegram_bot import run_telegram
            from voice.stt import SpeechToText
            from voice.tts import TextToSpeech

            stt = SpeechToText(api_key=groq_api_key, model_name=whisper_model, language=whisper_language)
            tts = TextToSpeech(api_key=elevenlabs_api_key, voice_id=elevenlabs_voice_id, model=elevenlabs_model)

            await run_telegram(
                agent=agent,
                stt=stt,
                tts=tts,
                token=telegram_token,
                user_id=mem0_user_id,
                session_manager=session_manager,
                notifier=notifier,
                confirmation_gate=confirmation_gate,
            )
    finally:
        vault_manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
