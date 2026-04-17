"""
End-to-end test for proactive Telegram message sending.

Tests the full stack:
  TelegramNotifier.init() → send() → Telegram API → your chat

Run from the project root:
  .venv/bin/python tests/test_telegram_send.py

Requires:
  - TELEGRAM_BOT_TOKEN in .env
  - data/telegram_chat_id to exist (written automatically by the bot on /start)
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env manually (avoid dotenv stdin issue)
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

# Change cwd so relative paths (data/) resolve correctly
os.chdir(ROOT)


def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        print(f"SKIP: {key} not set in .env")
        sys.exit(0)
    return val


async def main() -> None:
    from telegram import Bot
    from telegram.error import TelegramError

    from interfaces.notifier import TelegramNotifier
    from tools.telegram_tools import init_telegram_tools, send_telegram_message

    token = _require_env("TELEGRAM_BOT_TOKEN")

    # ── 1. Load chat_id ──────────────────────────────────────────────────
    chat_id = TelegramNotifier.load_chat_id()
    if chat_id is None:
        print("FAIL: data/telegram_chat_id not found.")
        print("      Send /start to the bot first so it can learn your chat ID.")
        sys.exit(1)
    print(f"chat_id      : {chat_id}")

    # ── 2. Verify bot identity ───────────────────────────────────────────
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        print(f"bot identity : @{me.username} (id={me.id})")
    except TelegramError as e:
        print(f"FAIL get_me  : {e}")
        sys.exit(1)

    # ── 3. Raw API send (proves token + chat_id are valid) ───────────────
    try:
        msg = await bot.send_message(chat_id=chat_id, text="[TEST 1/2] Raw API send — if you see this, token & chat_id are correct.")
        print(f"raw API send : OK (message_id={msg.message_id})")
    except TelegramError as e:
        print(f"FAIL raw send: {type(e).__name__}: {e}")
        sys.exit(1)

    # ── 4. Full stack: TelegramNotifier → tool → API ─────────────────────
    notifier = TelegramNotifier()
    notifier.init(bot, chat_id)
    assert notifier.ready, "Notifier should be ready after init()"
    print(f"notifier     : ready={notifier.ready}")

    init_telegram_tools(notifier)

    result = await send_telegram_message("[TEST 2/2] Full-stack tool send — agent proactive messaging works.")
    if result.get("success"):
        print(f"tool send    : OK (chars_sent={result['chars_sent']})")
    else:
        print(f"FAIL tool    : {result.get('error')}")
        sys.exit(1)

    print("\nAll tests passed. Check your Telegram for 2 test messages.")


if __name__ == "__main__":
    asyncio.run(main())
