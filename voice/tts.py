import re

import httpx

_MARKDOWN_RE = re.compile(r"(\*{1,3}|_{1,3}|`{1,3}|#{1,6}\s?|>\s?|[-*+]\s|\d+\.\s)")


def _strip_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub("", text).strip()


class TextToSpeech:
    def __init__(self, api_key: str, voice_id: str, model: str = "eleven_multilingual_v2") -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._base_url = "https://api.elevenlabs.io/v1"

    async def synthesize(self, text: str) -> bytes:
        """Přijme text, vrátí audio jako bytes (MP3)."""
        clean_text = _strip_markdown(text)
        url = f"{self._base_url}/text-to-speech/{self._voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": clean_text,
            "model_id": self._model,
            "voice_settings": {
                "stability": 0.4,
                "similarity_boost": 0.75,
            },
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content
