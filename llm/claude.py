import anthropic


class ClaudeClient:
    def __init__(self, api_key: str, model: str = None):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model or os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    async def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
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
        print(f"[tokens] in={input_tokens} out={output_tokens} total={input_tokens + output_tokens}")

        return response.content[0].text
