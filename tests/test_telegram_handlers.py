"""
Unit tests for Telegram command handlers and the text message handler.

Each handler is a module-level function that reads agent/session from
context.bot_data — easy to test by injecting mocks there.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interfaces.telegram_bot import (
    _cmd_newsession,
    _cmd_start,
    _handle_text,
    _session_id,
)
from interfaces.notifier import TelegramNotifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(chat_id: int = 12345, text: str = "hello"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.chat.send_action = AsyncMock()
    return update


def _make_agent():
    agent = MagicMock()
    agent.process = AsyncMock(return_value="Agent response")
    agent.open_session = AsyncMock()
    agent.close_session = AsyncMock()
    agent.get_all_memories = AsyncMock(return_value=[])
    agent._reasoning = None  # disables reasoning status message
    return agent


def _make_context(agent=None, user_id="tomas", notifier=None):
    ctx = MagicMock()
    ctx.bot_data = {
        "agent": agent or _make_agent(),
        "user_id": user_id,
        "notifier": notifier or MagicMock(),
        "session_manager": MagicMock(),
        "observability": None,
        "confirmation_gate": None,
    }
    ctx.args = []
    return ctx


# ---------------------------------------------------------------------------
# _session_id helper
# ---------------------------------------------------------------------------


def test_session_id_uses_chat_id():
    update = MagicMock()
    update.effective_chat.id = 99
    assert _session_id(update) == "telegram_99"


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_start_opens_session():
    update = _make_update(chat_id=42)
    agent = _make_agent()
    ctx = _make_context(agent=agent)

    with patch.object(TelegramNotifier, "save_chat_id"):
        await _cmd_start(update, ctx)

    agent.open_session.assert_called_once_with("telegram_42", "tomas")


@pytest.mark.asyncio
async def test_cmd_start_sends_greeting():
    update = _make_update()
    agent = _make_agent()
    ctx = _make_context(agent=agent)

    with patch.object(TelegramNotifier, "save_chat_id"):
        await _cmd_start(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert len(text) > 0


@pytest.mark.asyncio
async def test_cmd_start_initialises_notifier():
    update = _make_update(chat_id=55)
    notifier = MagicMock()
    ctx = _make_context(notifier=notifier)

    with patch.object(TelegramNotifier, "save_chat_id"):
        await _cmd_start(update, ctx)

    notifier.init.assert_called_once_with(ctx.bot_data["notifier"].init.call_args[0][0], 55)


# ---------------------------------------------------------------------------
# /newsession
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_newsession_closes_session():
    update = _make_update(chat_id=42)
    agent = _make_agent()
    ctx = _make_context(agent=agent)

    await _cmd_newsession(update, ctx)

    agent.close_session.assert_called_once_with("telegram_42")


@pytest.mark.asyncio
async def test_cmd_newsession_sends_confirmation():
    update = _make_update()
    ctx = _make_context()
    await _cmd_newsession(update, ctx)
    update.message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_text_calls_agent_process():
    update = _make_update(chat_id=42, text="What is the weather?")
    agent = _make_agent()
    ctx = _make_context(agent=agent)

    await _handle_text(update, ctx)

    agent.process.assert_called_once_with(
        "What is the weather?",
        "telegram_42",
        "tomas",
        progress_callback=None,
    )


@pytest.mark.asyncio
async def test_handle_text_sends_response_back():
    update = _make_update(text="Hello")
    agent = _make_agent()
    agent.process = AsyncMock(return_value="Agent says hi")
    ctx = _make_context(agent=agent)

    await _handle_text(update, ctx)

    update.message.reply_text.assert_called_once_with("Agent says hi")


@pytest.mark.asyncio
async def test_handle_text_exception_sends_error_message():
    update = _make_update(text="Crash?")
    agent = _make_agent()
    agent.process = AsyncMock(side_effect=RuntimeError("unexpected crash"))
    ctx = _make_context(agent=agent)

    await _handle_text(update, ctx)

    # Must not propagate the exception; must reply with an error
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "chyba" in text.lower() or "omlouvám" in text.lower()


@pytest.mark.asyncio
async def test_handle_text_sends_typing_action():
    update = _make_update(text="Hi")
    ctx = _make_context()
    await _handle_text(update, ctx)
    update.message.chat.send_action.assert_called_once()


# ---------------------------------------------------------------------------
# TelegramNotifier
# ---------------------------------------------------------------------------


def test_notifier_not_ready_when_uninitialised():
    notifier = TelegramNotifier()
    assert notifier.ready is False


def test_notifier_ready_after_init():
    notifier = TelegramNotifier()
    notifier.init(bot=MagicMock(), chat_id=12345)
    assert notifier.ready is True


@pytest.mark.asyncio
async def test_notifier_send_raises_when_not_ready():
    notifier = TelegramNotifier()
    with pytest.raises(RuntimeError):
        await notifier.send("hello")


def test_notifier_load_chat_id_returns_none_without_file(tmp_path, monkeypatch):
    # Point the notifier's data path to a temp dir with no file
    monkeypatch.chdir(tmp_path)
    result = TelegramNotifier.load_chat_id()
    assert result is None
