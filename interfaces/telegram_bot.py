import asyncio
import io
import logging
import os

from langdetect import LangDetectException, detect
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from core.agent import Agent
from core.session import SessionManager
from interfaces.notifier import TelegramNotifier
from voice.stt import SpeechToText
from voice.tts import TextToSpeech

_HELP_TEXT = """Dostupné příkazy:

/start — nová konverzace, inicializace session
/newsession — ukončí aktuální session a začne novou
/memory — zobrazí všechny uložené vzpomínky
/feedback <text> — uloží tvoji poznámku k mému chování s kontextem posledních zpráv
/self-reflect — vytvořím sebereflexi aktuální session
/snapshot [tag] — uloží aktuální session (volitelně s tagem)
/help — tento přehled

Tip: feedback a snapshoty mi pomáhají učit se a tobě dávají data pro pozdější iterativní ladění."""

logger = logging.getLogger(__name__)

_ctx: dict = {"session_id": None}


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


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP_TEXT)


async def _cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bundle = context.bot_data.get("observability")
    session_manager: SessionManager = context.bot_data["session_manager"]
    session_id = _session_id(update)

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Použití: /feedback <tvá poznámka>")
        return

    history = await session_manager.get_history(session_id)
    if bundle is None:
        await update.message.reply_text("Observability není nakonfigurováno.")
        return

    try:
        path = await bundle.feedback.record_feedback(text, session_id, history)
        await update.message.reply_text(f"Feedback uložen: {path}")
    except Exception as e:
        logger.exception("_cmd_feedback selhal (session=%s)", session_id)
        await update.message.reply_text(f"Chyba při ukládání feedbacku: {e}")


async def _cmd_self_reflect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    bundle = context.bot_data.get("observability")
    session_id = _session_id(update)

    if bundle is None:
        await update.message.reply_text("Observability není nakonfigurováno.")
        return

    await update.message.chat.send_action("typing")
    reflection = await agent.generate_self_reflection(session_id)

    try:
        path = await bundle.feedback.save_reflection(reflection, session_id, {})
        preview = reflection[:500]
        await update.message.reply_text(f"{preview}\n\n_(Uloženo: {path})_", parse_mode="Markdown")
    except Exception as e:
        logger.exception("_cmd_self_reflect selhal (session=%s)", session_id)
        await update.message.reply_text(reflection)


async def _cmd_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bundle = context.bot_data.get("observability")
    session_manager: SessionManager = context.bot_data["session_manager"]
    session_id = _session_id(update)

    if bundle is None:
        await update.message.reply_text("Observability není nakonfigurováno.")
        return

    tag = context.args[0] if context.args else None
    session = session_manager.get_session(session_id)
    if session is None:
        await update.message.reply_text("Žádná aktivní session k uložení.")
        return

    try:
        path = await bundle.snapshots.save_snapshot(session, snapshot_type="manual", tag=tag)
        await update.message.reply_text(f"Snapshot uložen: {path}")
    except ValueError as e:
        await update.message.reply_text(f"Neplatný tag: {e}")
    except Exception as e:
        logger.exception("_cmd_snapshot selhal (session=%s)", session_id)
        await update.message.reply_text(f"Chyba při ukládání snapshotu: {e}")


async def _safe_reply(update: Update, text: str) -> None:
    """Odešle zprávu s jedním retrym při TimedOut."""
    try:
        await update.message.reply_text(text)
    except TimedOut:
        logger.warning("Telegram TimedOut při odesílání odpovědi, zkouším znovu")
        await asyncio.sleep(2)
        await update.message.reply_text(text)


async def _keep_typing(chat, stop_event: asyncio.Event, interval: float = 4.0) -> None:
    while not stop_event.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    notifier: TelegramNotifier = context.bot_data["notifier"]
    notifier.init(context.bot, update.effective_chat.id)  # keep chat_id current
    user_id: str = context.bot_data["user_id"]
    session_id = _session_id(update)
    user_message = update.message.text

    stop_typing = asyncio.Event()
    asyncio.create_task(_keep_typing(update.message.chat, stop_typing))

    engine = getattr(agent, "_reasoning", None)
    use_status = engine is not None and engine.needs_iteration(user_message)
    status_msg = await update.message.reply_text("🔄 Přemýšlím…") if use_status else None

    async def update_status(text: str) -> None:
        if status_msg is not None:
            try:
                await status_msg.edit_text(text)
            except Exception:
                pass

    _ctx["session_id"] = session_id
    try:
        response = await agent.process(
            user_message, session_id, user_id,
            progress_callback=update_status if use_status else None,
        )
    except Exception:
        logger.exception("Neočekávaná chyba při zpracování zprávy (session=%s)", session_id)
        if status_msg is not None:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await _safe_reply(update, "Omlouvám se, nastala neočekávaná chyba.")
        return
    finally:
        stop_typing.set()

    if status_msg is not None:
        try:
            await status_msg.delete()
        except Exception:
            pass
    await _safe_reply(update, response)


async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    notifier: TelegramNotifier = context.bot_data["notifier"]
    notifier.init(context.bot, update.effective_chat.id)  # keep chat_id current
    stt: SpeechToText = context.bot_data["stt"]
    tts: TextToSpeech = context.bot_data["tts"]
    user_id: str = context.bot_data["user_id"]
    session_id = _session_id(update)

    stop_typing = asyncio.Event()
    asyncio.create_task(_keep_typing(update.message.chat, stop_typing))

    try:
        voice_file = await update.message.voice.get_file()
        audio_bytes = await voice_file.download_as_bytearray()
        user_message = await stt.transcribe(bytes(audio_bytes))
    except Exception as e:
        stop_typing.set()
        logger.exception("Chyba při přepisu hlasu (session=%s)", session_id)
        await update.message.reply_text(f"Chyba při přepisu hlasu: {e}")
        return

    await update.message.reply_text(f"_{user_message}_", parse_mode="Markdown")

    _ctx["session_id"] = session_id
    try:
        response_text = await agent.process(user_message, session_id, user_id, is_voice=True)
    except Exception:
        logger.exception("Neočekávaná chyba při zpracování hlasové zprávy (session=%s)", session_id)
        await _safe_reply(update, "Omlouvám se, nastala neočekávaná chyba.")
        return
    finally:
        stop_typing.set()

    await _safe_reply(update, response_text)

    try:
        detected_lang = detect(response_text)
    except LangDetectException:
        logger.warning("TTS: language detection failed (session=%s)", session_id)
        detected_lang = None

    session_manager: SessionManager = context.bot_data["session_manager"]
    override = await session_manager.get_voice_profile(session_id)
    profile = override if override is not None else detected_lang

    try:
        audio_bytes = await tts.synthesize(response_text, voice_profile=profile)
        await update.message.reply_voice(voice=io.BytesIO(audio_bytes))
    except Exception as e:
        logger.warning("Chyba TTS (session=%s): %s", session_id, e)
        await update.message.reply_text(f"(Chyba TTS: {e})")


async def _handle_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline keyboard button taps to the ConfirmationGate."""
    query = update.callback_query
    await query.answer()

    gate = context.bot_data.get("confirmation_gate")
    if gate is None:
        await query.edit_message_text("No confirmation gate configured.")
        return

    data: str = query.data
    if data.startswith("confirm_"):
        token = data[len("confirm_"):]
        approved = True
    elif data.startswith("deny_"):
        token = data[len("deny_"):]
        approved = False
    else:
        logger.warning("Unknown callback_data: %s", data)
        return

    if not gate.has_pending(token):
        await query.edit_message_text("This request has already expired or been resolved.")
        return

    gate.resolve(token, approved)
    action = "APPROVED" if approved else "DENIED"
    original_lines = query.message.text.splitlines()
    tool_line = original_lines[2] if len(original_lines) > 2 else ""
    await query.edit_message_text(f"{action}: {tool_line}")


async def run_telegram(
    agent: Agent,
    stt: SpeechToText,
    tts: TextToSpeech,
    token: str,
    user_id: str,
    session_manager: SessionManager,
    notifier: TelegramNotifier | None = None,
    confirmation_gate=None,  # ConfirmationGate | None
    observability=None,  # ObservabilityBundle | None
) -> None:
    """Spustí Telegram bot."""
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
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
    app.bot_data["confirmation_gate"] = confirmation_gate
    app.bot_data["session_manager"] = session_manager
    app.bot_data["observability"] = observability

    from tools.voice_tools import init_voice_tools
    init_voice_tools(session_manager, lambda: _ctx["session_id"])

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("memory", _cmd_memory))
    app.add_handler(CommandHandler("newsession", _cmd_newsession))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("feedback", _cmd_feedback))
    app.add_handler(CommandHandler("self_reflect", _cmd_self_reflect))
    app.add_handler(CommandHandler("snapshot", _cmd_snapshot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_handler(MessageHandler(filters.VOICE, _handle_voice))
    app.add_handler(CallbackQueryHandler(_handle_confirmation_callback))

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
