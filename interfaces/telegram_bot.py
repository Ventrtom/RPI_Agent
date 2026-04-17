import asyncio
import io
import logging
import os

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from core.agent import Agent
from interfaces.notifier import TelegramNotifier
from voice.stt import SpeechToText
from voice.tts import TextToSpeech

logger = logging.getLogger(__name__)


def _session_id(update: Update) -> str:
    return f"telegram_{update.effective_chat.id}"


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    notifier: TelegramNotifier = context.bot_data["notifier"]
    session_id = _session_id(update)
    user_id: str = context.bot_data["user_id"]
    await agent.open_session(session_id, user_id)

    # Persist chat_id and initialise notifier on every /start
    chat_id: int = update.effective_chat.id
    TelegramNotifier.save_chat_id(chat_id)
    notifier.init(context.bot, chat_id)  # always refresh — heals any bad state

    await update.message.reply_text(
        "Ahoj! Jsem tvůj osobní asistent. Pošli mi zprávu nebo hlasovou zprávu."
    )


async def _cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    memories = await agent.get_all_memories()
    if memories:
        text = "Vzpomínky:\n" + "\n".join(f"- {m}" for m in memories)
    else:
        text = "Žádné vzpomínky."
    await update.message.reply_text(text)


async def _cmd_newsession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    session_id = _session_id(update)
    await agent.close_session(session_id)
    await update.message.reply_text("Session ukončena. Začínáme znovu.")


async def _safe_reply(update: Update, text: str) -> None:
    """Odešle zprávu s jedním retrym při TimedOut."""
    try:
        await update.message.reply_text(text)
    except TimedOut:
        logger.warning("Telegram TimedOut při odesílání odpovědi, zkouším znovu")
        await asyncio.sleep(2)
        await update.message.reply_text(text)


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    notifier: TelegramNotifier = context.bot_data["notifier"]
    notifier.init(context.bot, update.effective_chat.id)  # keep chat_id current
    user_id: str = context.bot_data["user_id"]
    session_id = _session_id(update)
    user_message = update.message.text

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        response = await agent.process(user_message, session_id, user_id)
    except Exception:
        logger.exception("Neočekávaná chyba při zpracování zprávy (session=%s)", session_id)
        await _safe_reply(update, "Omlouvám se, nastala neočekávaná chyba.")
        return

    await _safe_reply(update, response)


async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    notifier: TelegramNotifier = context.bot_data["notifier"]
    notifier.init(context.bot, update.effective_chat.id)  # keep chat_id current
    stt: SpeechToText = context.bot_data["stt"]
    tts: TextToSpeech = context.bot_data["tts"]
    user_id: str = context.bot_data["user_id"]
    session_id = _session_id(update)

    try:
        voice_file = await update.message.voice.get_file()
        audio_bytes = await voice_file.download_as_bytearray()
        user_message = await stt.transcribe(bytes(audio_bytes))
    except Exception as e:
        logger.exception("Chyba při přepisu hlasu (session=%s)", session_id)
        await update.message.reply_text(f"Chyba při přepisu hlasu: {e}")
        return

    await update.message.reply_text(f"_{user_message}_", parse_mode="Markdown")

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        response_text = await agent.process(user_message, session_id, user_id, is_voice=True)
    except Exception:
        logger.exception("Neočekávaná chyba při zpracování hlasové zprávy (session=%s)", session_id)
        await _safe_reply(update, "Omlouvám se, nastala neočekávaná chyba.")
        return

    await _safe_reply(update, response_text)

    try:
        audio_bytes = await tts.synthesize(response_text)
        await update.message.reply_voice(voice=io.BytesIO(audio_bytes))
    except Exception as e:
        logger.warning("Chyba TTS (session=%s): %s", session_id, e)
        await update.message.reply_text(f"(Chyba TTS: {e})")


async def run_telegram(
    agent: Agent,
    stt: SpeechToText,
    tts: TextToSpeech,
    token: str,
    user_id: str,
    notifier: TelegramNotifier | None = None,
) -> None:
    """Spustí Telegram bot."""
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    if notifier is None:
        notifier = TelegramNotifier()

    app.bot_data["agent"] = agent
    app.bot_data["stt"] = stt
    app.bot_data["tts"] = tts
    app.bot_data["user_id"] = user_id
    app.bot_data["notifier"] = notifier

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("memory", _cmd_memory))
    app.add_handler(CommandHandler("newsession", _cmd_newsession))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_handler(MessageHandler(filters.VOICE, _handle_voice))

    logger.info("Telegram bot spuštěn")
    async with app:
        await app.start()

        # Initialise notifier from env var or persisted file so proactive
        # messaging works immediately without waiting for a /start message.
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        startup_chat_id: int | None = (
            int(env_chat_id) if env_chat_id else TelegramNotifier.load_chat_id()
        )
        if startup_chat_id is not None:
            notifier.init(app.bot, startup_chat_id)  # file is best-guess; real messages override

        await app.updater.start_polling()
        await asyncio.Event().wait()  # blokuj dokud není Ctrl+C
        await app.updater.stop()
        await app.stop()
