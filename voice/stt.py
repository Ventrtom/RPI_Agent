import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

from groq import Groq


class SpeechToText:
    def __init__(self, model_name: str = "whisper-large-v3-turbo", language: str = None) -> None:
        self._model_name = model_name or "whisper-large-v3-turbo"
        self._language = language  # None = auto-detect
        self._client = Groq(api_key=os.environ["GROQ_API_KEY"])
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _transcribe_sync(self, audio_path: str) -> str:
        with open(audio_path, "rb") as f:
            kwargs = {
                "file": (os.path.basename(audio_path), f, "audio/ogg"),
                "model": self._model_name,
                "response_format": "text",
            }
            if self._language:
                kwargs["language"] = self._language
            return self._client.audio.transcriptions.create(**kwargs).strip()

    async def transcribe(self, audio_bytes: bytes) -> str:
        """Přijme audio jako bytes (OGG/MP3/WAV), vrátí přepsaný text."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg", dir="/tmp") as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, self._transcribe_sync, tmp_path)
        finally:
            os.unlink(tmp_path)
