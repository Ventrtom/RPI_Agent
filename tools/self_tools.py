"""
Self-introspection tool: lets the agent report its own configuration,
capabilities, memory stats, and session token usage.

Call init_self_tools(claude_client, memory_client, tool_registry) in main.py
before registering get_self_info.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_claude_client = None
_memory_client = None
_tool_registry = None

_OPTIONAL_FEATURES = {
    "web_search": {
        "env": "TAVILY_API_KEY",
        "url": "https://app.tavily.com/",
    },
    "voice_stt": {
        "env": "GROQ_API_KEY",
        "url": "https://console.groq.com/keys",
    },
    "voice_tts_key": {
        "env": "ELEVENLABS_API_KEY",
        "url": "https://elevenlabs.io/app/settings/api-keys",
    },
    "voice_tts_voice": {
        "env": "ELEVENLABS_VOICE_ID",
        "url": "https://elevenlabs.io/app/voice-lab",
    },
}

_GOOGLE_OAUTH_FILE = os.getenv("GOOGLE_OAUTH_CREDENTIALS", "credentials/google_oauth.json")


def init_self_tools(claude_client, memory_client, tool_registry) -> None:
    """Call once in main.py after creating all dependencies."""
    global _claude_client, _memory_client, _tool_registry
    _claude_client = claude_client
    _memory_client = memory_client
    _tool_registry = tool_registry


GET_SELF_INFO_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
}


async def get_self_info() -> dict:
    """
    Return information about my own configuration and current runtime state:
    which LLM model I run on, how many memories I have stored, the size of
    the memory database, how many tokens we have used this session, which
    tools I have available, and which optional features are enabled or
    disabled (with setup instructions for disabled ones).
    Use this when the user asks what model you are, about your memory,
    token usage, capabilities, tools, or whether a feature is available.
    """
    if _claude_client is None or _memory_client is None or _tool_registry is None:
        return {"error": "Self tools not initialised — call init_self_tools() first."}

    # LLM / model info
    model_info = {
        "model": _claude_client._model,
        "max_tokens_per_response": int(os.getenv("CLAUDE_MAX_TOKENS", "1024")),
    }

    # Memory stats
    try:
        memory_stats = await _memory_client.get_stats()
    except Exception as exc:
        logger.warning("get_self_info: memory stats failed: %s", exc)
        memory_stats = {"memory_count": None, "db_size_mb": None, "error": str(exc)}

    # Session token usage
    token_usage = _claude_client.get_token_usage()

    # Registered tools
    tool_names = [fn.__name__ for fn in _tool_registry.get_all()]

    # Optional features
    features: dict = {}
    for feature, cfg in _OPTIONAL_FEATURES.items():
        enabled = bool(os.getenv(cfg["env"]))
        entry: dict = {"enabled": enabled}
        if not enabled:
            entry["setup_url"] = cfg["url"]
        features[feature] = entry

    # Group TTS key + voice_id into one "voice_tts" entry
    tts_enabled = features["voice_tts_key"]["enabled"] and features["voice_tts_voice"]["enabled"]
    tts_entry: dict = {"enabled": tts_enabled}
    if not tts_enabled:
        missing = []
        if not features["voice_tts_key"]["enabled"]:
            missing.append("ELEVENLABS_API_KEY (https://elevenlabs.io/app/settings/api-keys)")
        if not features["voice_tts_voice"]["enabled"]:
            missing.append("ELEVENLABS_VOICE_ID (https://elevenlabs.io/app/voice-lab)")
        tts_entry["missing"] = missing
    del features["voice_tts_key"]
    del features["voice_tts_voice"]
    features["voice_tts"] = tts_entry

    # Google OAuth
    google_enabled = Path(_GOOGLE_OAUTH_FILE).exists()
    google_entry: dict = {"enabled": google_enabled}
    if not google_enabled:
        google_entry["setup_url"] = "https://console.cloud.google.com/apis/credentials"
    features["google_oauth"] = google_entry

    return {
        "model": model_info,
        "memory": memory_stats,
        "session_tokens": token_usage,
        "tools": tool_names,
        "optional_features": features,
    }
