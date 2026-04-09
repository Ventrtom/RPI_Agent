import asyncio
import io
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from core.agent import Agent
from voice.stt import SpeechToText
from voice.tts import TextToSpeech

logger = logging.getLogger(__name__)


def _session_id(update: Update) -> str:
    return f"telegram_{update.effective_chat.id}"


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    session_id = _session_id(update)
    user_id: str = context.bot_data["user_id"]
    await agent._sessions.get_or_create(session_id, user_id)
    await update.message.reply_text(
        "Ahoj! Jsem tvůj osobní asistent. Pošli mi zprávu nebo hlasovou zprávu."
    )


async def _cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    memories = await agent._memory.get_all()
    if memories:
        text = "Vzpomínky:\n" + "\n".join(f"- {m}" for m in memories)
    else:
        text = "Žádné vzpomínky."
    await update.message.reply_text(text)


async def _cmd_newsession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    session_id = _session_id(update)
    await agent._sessions.close_session(session_id)
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


async def run_telegram(agent: Agent, stt: SpeechToText, tts: TextToSpeech, token: str, user_id: str) -> None:
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

    app.bot_data["agent"] = agent
    app.bot_data["stt"] = stt
    app.bot_data["tts"] = tts
    app.bot_data["user_id"] = user_id

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("memory", _cmd_memory))
    app.add_handler(CommandHandler("newsession", _cmd_newsession))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_handler(MessageHandler(filters.VOICE, _handle_voice))

    logger.info("Telegram bot spuštěn")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()  # blokuj dokud není Ctrl+C
        await app.updater.stop()
        await app.stop()
