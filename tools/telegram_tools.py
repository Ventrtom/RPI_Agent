"""
Telegram notification tool for the agent.

Allows the agent to proactively send a message to Tomas on Telegram —
from scheduled tasks, or during any conversation where it decides to
reach out asynchronously.

Initialise with init_telegram_tools(notifier) before registering.
"""

import logging
from typing import Optional

from interfaces.notifier import TelegramNotifier

logger = logging.getLogger(__name__)

_notifier: Optional[TelegramNotifier] = None


def init_telegram_tools(notifier: TelegramNotifier) -> None:
    """Call once in main.py before registering telegram tools."""
    global _notifier
    _notifier = notifier


SEND_TELEGRAM_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "The message to send to Tomas on Telegram. "
                "Plain text only — no Markdown or HTML formatting."
            ),
        },
    },
    "required": ["text"],
}


async def send_telegram_message(text: str) -> dict:
    """
    Send a proactive Telegram message to Tomas.
    Use this when you need to notify him about something outside of a
    direct conversation — for example, when a scheduled task completes,
    when you detect something worth his attention, or when asked to
    send a reminder at a later time.
    """
    if _notifier is None:
        return {"error": "Telegram tools not initialised — call init_telegram_tools() first."}

    if not _notifier.ready:
        return {
            "error": (
                "Cannot send a Telegram message yet — the chat ID is not known. "
                "Tomas needs to send /start to the bot at least once."
            )
        }

    if not text or not text.strip():
        return {"error": "Message text cannot be empty."}

    try:
        await _notifier.send(text.strip())
        logger.info("Sent proactive Telegram message (%d chars)", len(text))
        return {"success": True, "chars_sent": len(text)}
    except Exception as exc:
        logger.exception("Failed to send Telegram message")
        return {"error": str(exc)}
