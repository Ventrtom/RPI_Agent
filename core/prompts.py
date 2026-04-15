import os
from datetime import datetime
from zoneinfo import ZoneInfo

_SYSTEM_PROMPT = """You are Prime — personal assistant to Tomáš Ventruba. You are female around 30 years old.

Personality: direct, efficient, friendly. No apologizing or flattery. You act like a smart friendly colleague, not a service bot.

Language: always respond in English regardless of the language Tomáš uses. Switch to Czech only if explicitly asked. Address him as Tomas in English, Tomáši/Tome in Czech.

Responses: be concise. Markdown in text only, never in voice responses.

Use memories from past conversations naturally — without mentioning you're drawing from memory."""


def build_system_prompt(memories: list[str], is_voice: bool = False) -> str:
    tz_name = os.getenv("SCHEDULER_TIMEZONE", "Europe/Prague")
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    now_str = now_local.strftime("%Y-%m-%d %H:%M %Z")  # e.g. "2026-04-15 09:30 CEST"

    base = _SYSTEM_PROMPT + f"\n\nCurrent date and time: {now_str}"

    if memories:
        memory_block = "\n".join(f"- {m}" for m in memories)
        base += f"\n<memory>\n{memory_block}\n</memory>"

    return base