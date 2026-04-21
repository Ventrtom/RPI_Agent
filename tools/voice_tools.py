"""Voice profile tools for the agent."""
import logging
from typing import Callable

logger = logging.getLogger(__name__)

_session_manager = None
_session_id_getter: Callable[[], str | None] | None = None


def init_voice_tools(session_manager, session_id_getter: Callable[[], str | None]) -> None:
    global _session_manager, _session_id_getter
    _session_manager = session_manager
    _session_id_getter = session_id_getter


SET_VOICE_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "profile": {
            "type": "string",
            "description": (
                "Voice profile name to use for TTS responses (e.g. 'EN', 'CS'). "
                "Pass null to reset to auto-detection."
            ),
        }
    },
    "required": ["profile"],
}


async def set_voice_profile(profile: str | None = None) -> dict:
    """
    Switch the TTS voice profile for the current session. Call this when the user
    asks to change the language of spoken responses, e.g. 'speak Czech', 'switch to
    English', 'mluv česky'. Profile names correspond to VOICE_PROFILE_{NAME}_* env
    variables. Returns confirmation of the change.
    """
    if _session_manager is None or _session_id_getter is None:
        return {"error": "Voice tools not initialised."}
    session_id = _session_id_getter()
    if not session_id:
        return {"error": "No active session."}
    normalised = profile.upper() if profile else None
    await _session_manager.set_voice_profile(session_id, normalised)
    if normalised:
        return {"ok": True, "active_voice_profile": normalised}
    return {"ok": True, "active_voice_profile": None, "note": "Reset to auto-detection."}
