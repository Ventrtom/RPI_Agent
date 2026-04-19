"""
ConfirmationGate — intercepts calls to dangerous tools and requests
human approval via Telegram inline keyboard.

All methods run in the same asyncio event loop that owns the Telegram
Application, so plain asyncio.Future is safe throughout.
"""
import asyncio
import logging
import secrets
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DANGEROUS_TOOLS: frozenset[str] = frozenset({
    "restart_agent_service",
    "shutdown_raspberry_pi",
})

_TOOL_LABELS: dict[str, str] = {
    "restart_agent_service": "Restart agent service",
    "shutdown_raspberry_pi": "Shutdown / reboot Raspberry Pi",
}

_TIMEOUT_SECONDS = 60


@dataclass
class _PendingRequest:
    tool_name: str
    params: dict
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


class ConfirmationGate:
    """
    Manages pending human-approval requests for dangerous tool calls.

    Lifecycle:
      1. agent.process() calls gate.request(tool_name, params)
         → sends inline keyboard to Telegram, suspends until resolved or timeout
      2. Telegram CallbackQueryHandler calls gate.resolve(token, approved)
         → future is set, gate.request() returns True or False
    """

    def __init__(self, notifier) -> None:
        self._notifier = notifier
        self._pending: dict[str, _PendingRequest] = {}

    async def request(self, tool_name: str, params: dict) -> bool:
        """
        Send confirmation keyboard and wait up to 60 s for user response.

        Returns True if approved, False if denied or timed out.
        Raises RuntimeError if the notifier is not yet ready (no chat_id).
        """
        if not self._notifier.ready:
            raise RuntimeError(
                "ConfirmationGate: notifier not ready — no chat_id known yet. "
                "Ask the user to send /start to the bot first."
            )

        token = secrets.token_hex(8)
        req = _PendingRequest(tool_name=tool_name, params=params)
        self._pending[token] = req

        label = _TOOL_LABELS.get(tool_name, tool_name)
        param_summary = _format_params(params)
        text = (
            f"CONFIRMATION REQUIRED\n\n"
            f"Tool: {label}\n"
            f"{param_summary}"
            f"Approve execution?"
        )

        try:
            await self._notifier.send_with_keyboard(text, token)
        except Exception:
            logger.exception("ConfirmationGate: failed to send keyboard for token=%s", token)
            self._pending.pop(token, None)
            return False

        logger.info("ConfirmationGate: awaiting approval token=%s tool=%s", token, tool_name)

        try:
            approved: bool = await asyncio.wait_for(
                asyncio.shield(req.future),
                timeout=_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("ConfirmationGate: token=%s timed out after %ds", token, _TIMEOUT_SECONDS)
            approved = False
        finally:
            self._pending.pop(token, None)

        logger.info("ConfirmationGate: token=%s approved=%s tool=%s", token, approved, tool_name)
        return approved

    def resolve(self, token: str, approved: bool) -> None:
        """Called by the Telegram callback handler when the user taps a button."""
        req = self._pending.get(token)
        if req is None:
            logger.warning("ConfirmationGate: resolve() called for unknown/expired token=%s", token)
            return
        if not req.future.done():
            req.future.set_result(approved)

    def has_pending(self, token: str) -> bool:
        return token in self._pending


def _format_params(params: dict) -> str:
    if not params:
        return ""
    lines = [f"  {k}: {v}" for k, v in params.items()]
    return "Params:\n" + "\n".join(lines) + "\n"
