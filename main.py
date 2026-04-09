"""
Osobní AI Agent — entry point.

Použití:
  python main.py cli       # Spustí CLI interface
  python main.py telegram  # Spustí Telegram bot
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        print(f"Chyba: chybí proměnná prostředí {key}")
        sys.exit(1)
    return value


async def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("cli", "telegram"):
        print("Použití: python main.py [cli|telegram]")
        sys.exit(1)

    mode = sys.argv[1]

    # --- konfigurace ---
    anthropic_api_key = _require("ANTHROPIC_API_KEY")
    chroma_path = os.getenv("CHROMA_DB_PATH", "./data/chroma")
    mem0_user_id = os.getenv("MEM0_USER_ID", "tomas")
    session_timeout = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20251022")

    print(f"RPI Agent — model={model}  chroma={chroma_path}  mode={mode}")

    # --- závislosti ---
    from core.agent import Agent
    from core.session import SessionManager
    from llm.claude import ClaudeClient
    from memory.client import MemoryClient

    memory_client = MemoryClient(user_id=mem0_user_id, chroma_path=chroma_path)
    claude_client = ClaudeClient(api_key=anthropic_api_key, model=model)
    session_manager = SessionManager(timeout_minutes=session_timeout)
    agent = Agent(memory_client=memory_client, claude_client=claude_client, session_manager=session_manager)

    if mode == "cli":
        from interfaces.cli import run_cli
        await run_cli(agent)

    elif mode == "telegram":
        telegram_token = _require("TELEGRAM_BOT_TOKEN")
        elevenlabs_api_key = _require("ELEVENLABS_API_KEY")
        elevenlabs_voice_id = _require("ELEVENLABS_VOICE_ID")
        whisper_model = os.getenv("WHISPER_MODEL") or None
        whisper_language = os.getenv("WHISPER_LANGUAGE") or None

        from interfaces.telegram_bot import run_telegram
        from voice.stt import SpeechToText
        from voice.tts import TextToSpeech

        stt = SpeechToText(model_name=whisper_model, language=whisper_language)
        tts = TextToSpeech(api_key=elevenlabs_api_key, voice_id=elevenlabs_voice_id)

        await run_telegram(agent=agent, stt=stt, tts=tts, token=telegram_token, user_id=mem0_user_id)


if __name__ == "__main__":
    asyncio.run(main())
