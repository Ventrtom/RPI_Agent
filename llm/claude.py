import json
import logging
import os
from collections.abc import Callable

import anthropic

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))


class ClaudeClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._session_input_tokens: int = 0
        self._session_output_tokens: int = 0
        self._session_cache_creation_tokens: int = 0
        self._session_cache_read_tokens: int = 0

    async def complete(
        self,
        system: str | list[dict],
        messages: list[dict],
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        tools: list[dict] | None = None,
        tool_executor: Callable | None = None,
    ) -> str:
        """
        Call Claude API and return text response.
        When tools and tool_executor are provided, runs the full tool use loop
        until Claude returns a final text response.
        """
        current_messages = list(messages)
        system_payload: list[dict] = (
            [{"type": "text", "text": system}] if isinstance(system, str) else system
        )

        logger.debug("complete() max_tokens=%d", max_tokens)
        while True:
            kwargs: dict = {
                "model": self._model,
                "system": system_payload,
                "messages": current_messages,
                "max_tokens": max_tokens,
            }
            if tools:
                kwargs["tools"] = tools

            response = await self._client.messages.create(**kwargs)

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cache_create = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            self._session_input_tokens += input_tokens
            self._session_output_tokens += output_tokens
            self._session_cache_creation_tokens += cache_create
            self._session_cache_read_tokens += cache_read
            logger.debug(
                "tokens in=%d out=%d cache_create=%d cache_read=%d stop=%s",
                input_tokens,
                output_tokens,
                cache_create,
                cache_read,
                response.stop_reason,
            )

            if response.stop_reason != "tool_use" or not tool_executor:
                text_blocks = [b for b in response.content if b.type == "text"]
                return text_blocks[0].text if text_blocks else ""

            # Process tool calls and loop back
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Tool call: %s(%s)", block.name, block.input)
                    try:
                        result = await tool_executor(block.name, block.input)
                        content = json.dumps(result) if isinstance(result, dict) else str(result)
                    except Exception as e:
                        logger.exception("Tool %s failed", block.name)
                        content = f"Error: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })

            current_messages = current_messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]

    def get_token_usage(self) -> dict:
        """Return cumulative token usage since process start."""
        total = self._session_input_tokens + self._session_output_tokens
        return {
            "input_tokens": self._session_input_tokens,
            "output_tokens": self._session_output_tokens,
            "total_tokens": total,
            "cache_creation_tokens": self._session_cache_creation_tokens,
            "cache_read_tokens": self._session_cache_read_tokens,
        }
