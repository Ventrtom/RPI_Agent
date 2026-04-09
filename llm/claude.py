import logging
import os

import anthropic

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))


class ClaudeClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        """Zavolá Claude API, vrátí textovou odpověď."""
        response = await self._client.messages.create(
            model=self._model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        )
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        logger.debug("tokens in=%d out=%d total=%d", input_tokens, output_tokens, input_tokens + output_tokens)

        return response.content[0].text
