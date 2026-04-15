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

    async def complete(
        self,
        system: str,
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

        while True:
            kwargs: dict = {
                "model": self._model,
                "system": system,
                "messages": current_messages,
                "max_tokens": max_tokens,
            }
            if tools:
                kwargs["tools"] = tools

            response = await self._client.messages.create(**kwargs)

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            logger.debug(
                "tokens in=%d out=%d total=%d stop=%s",
                input_tokens,
                output_tokens,
                input_tokens + output_tokens,
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
