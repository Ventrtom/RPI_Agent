import asyncio
import io

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from core.agent import Agent
from voice.stt import SpeechToText
from voice.tts import TextToSpeech


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


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    user_id: str = context.bot_data["user_id"]
    session_id = _session_id(update)
    user_message = update.message.text

    response = await agent.process(user_message, session_id, user_id)
    await update.message.reply_text(response)


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
        await update.message.reply_text(f"Chyba při přepisu hlasu: {e}")
        return

    await update.message.reply_text(f"_{user_message}_", parse_mode="Markdown")

    response_text = await agent.process(user_message, session_id, user_id, is_voice=True)

    await update.message.reply_text(response_text)

    try:
        audio_bytes = await tts.synthesize(response_text)
        await update.message.reply_voice(voice=io.BytesIO(audio_bytes))
    except Exception as e:
        await update.message.reply_text(f"(Chyba TTS: {e})")


async def run_telegram(agent: Agent, stt: SpeechToText, tts: TextToSpeech, token: str, user_id: str) -> None:
    """Spustí Telegram bot."""
    app = Application.builder().token(token).build()

    app.bot_data["agent"] = agent
    app.bot_data["stt"] = stt
    app.bot_data["tts"] = tts
    app.bot_data["user_id"] = user_id

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("memory", _cmd_memory))
    app.add_handler(CommandHandler("newsession", _cmd_newsession))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_handler(MessageHandler(filters.VOICE, _handle_voice))

    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()  # blokuj dokud není Ctrl+C
        await app.updater.stop()
        await app.stop()
