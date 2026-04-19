"""
TelegramNotifier — allows the agent to send proactive messages to the user.

Lifecycle:
  1. Instantiated in main.py (before any interface starts).
  2. run_telegram() calls notifier.init(bot, chat_id) once the bot is up.
  3. Tools (send_telegram_message) call notifier.send(text) at any time.

In CLI mode the notifier is never initialised; tools that call send() will
receive a clear error rather than crashing.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CHAT_ID_FILE = Path("data/telegram_chat_id")


class TelegramNotifier:
    def __init__(self) -> None:
        self._bot = None
        self._chat_id: int | None = None

    def init(self, bot, chat_id: int) -> None:
        """Called by run_telegram() once the bot and chat_id are known."""
        self._bot = bot
        self._chat_id = chat_id
        logger.info("TelegramNotifier ready (chat_id=%d)", chat_id)

    @property
    def ready(self) -> bool:
        return self._bot is not None and self._chat_id is not None

    async def send(self, text: str) -> None:
        """Send a text message to the user. Raises RuntimeError if not ready."""
        if not self.ready:
            raise RuntimeError(
                "Cannot send Telegram message — chat_id not known yet. "
                "Ask Tomas to send /start to the bot first."
            )
        await self._bot.send_message(chat_id=self._chat_id, text=text)

    async def send_with_keyboard(self, text: str, token: str) -> None:
        """Send a message with Approve / Deny inline keyboard for tool confirmation."""
        if not self.ready:
            raise RuntimeError(
                "Cannot send keyboard — chat_id not known yet. "
                "Ask Tomas to send /start to the bot first."
            )
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # lazy import keeps CLI clean
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"confirm_{token}"),
            InlineKeyboardButton("Deny",    callback_data=f"deny_{token}"),
        ]])
        await self._bot.send_message(chat_id=self._chat_id, text=text, reply_markup=keyboard)

    # ------------------------------------------------------------------
    # Chat ID persistence
    # ------------------------------------------------------------------

    @staticmethod
    def load_chat_id() -> int | None:
        """Read persisted chat_id from data/telegram_chat_id, or None."""
        try:
            return int(_CHAT_ID_FILE.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def save_chat_id(chat_id: int) -> None:
        """Persist chat_id so it survives process restarts."""
        _CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CHAT_ID_FILE.write_text(str(chat_id))
