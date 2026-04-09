import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

from faster_whisper import WhisperModel


class SpeechToText:
    def __init__(self, model_name: str = "base", language: str = "cs"):
        self._language = language
        self._model = WhisperModel(model_name, device="cpu", compute_type="int8")
        self._executor = ThreadPoolExecutor(max_workers=1)

    def _transcribe_sync(self, audio_path: str) -> str:
        segments, _ = self._model.transcribe(audio_path, language=self._language)
        return "".join(segment.text for segment in segments).strip()

    async def transcribe(self, audio_bytes: bytes) -> str:
        """
        Přijme audio jako bytes (OGG/MP3/WAV), vrátí přepsaný text.
        Běží v ThreadPoolExecutor aby neblokovalo asyncio.
        """
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(self._executor, self._transcribe_sync, tmp_path)
            return text
        finally:
            os.unlink(tmp_path)
