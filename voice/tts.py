import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_MARKDOWN_RE = re.compile(r"(\*{1,3}|_{1,3}|`{1,3}|#{1,6}\s?|>\s?|[-*+]\s|\d+\.\s)")


def _strip_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub("", text).strip()


class TextToSpeech:
    def __init__(self, api_key: str | None, voice_id: str | None, model: str = "eleven_multilingual_v2") -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._base_url = "https://api.elevenlabs.io/v1"

    async def synthesize(self, text: str, voice_profile: str | None = None) -> bytes:
        """Přijme text, vrátí audio jako bytes (MP3)."""
        if voice_profile is not None:
            profile_key = voice_profile.upper()
            profile_id = os.environ.get(f"VOICE_PROFILE_{profile_key}_ID")
            if profile_id:
                voice_id = profile_id
                model = os.environ.get(f"VOICE_PROFILE_{profile_key}_MODEL", self._model)
                logger.debug("TTS: using profile '%s' (voice_id=%s, model=%s)", profile_key, voice_id, model)
            else:
                voice_id = self._voice_id
                model = self._model
                logger.debug("TTS: profile '%s' not found in env, falling back to default", profile_key)
        else:
            voice_id = self._voice_id
            model = self._model
            logger.debug("TTS: no profile requested, using default (voice_id=%s, model=%s)", voice_id, model)

        if not self._api_key or not voice_id:
            raise RuntimeError(
                "Text-to-speech není dostupný: ELEVENLABS_API_KEY nebo ELEVENLABS_VOICE_ID není nastaven v .env. "
                "Klíč a Voice ID získáš na: https://elevenlabs.io/"
            )
        clean_text = _strip_markdown(text)
        url = f"{self._base_url}/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": clean_text,
            "model_id": model,
            "voice_settings": {
                "stability": 0.4,
                "similarity_boost": 0.75,
            },
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content
