"""Home Assistant tools for the agent."""
import logging

import httpx

from tools.ha_client import HAClient

logger = logging.getLogger(__name__)

_client: HAClient | None = None


def init_ha_tools(client: HAClient) -> None:
    global _client
    _client = client


HA_LIST_ENTITIES_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": (
                "Filter entities by domain, e.g. 'light', 'switch', 'sensor', "
                "'binary_sensor', 'climate', 'media_player'. Omit to return all entities."
            ),
        },
    },
    "required": [],
}

HA_GET_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "Full entity ID, e.g. 'light.living_room' or 'sensor.temperature'.",
        },
    },
    "required": ["entity_id"],
}

HA_CALL_SERVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "Service domain, e.g. 'light', 'switch', 'climate', 'script'.",
        },
        "service": {
            "type": "string",
            "description": "Service name, e.g. 'turn_on', 'turn_off', 'toggle', 'set_temperature'.",
        },
        "entity_id": {
            "type": "string",
            "description": "Target entity ID. Required for most services.",
        },
        "service_data": {
            "type": "object",
            "description": (
                "Additional parameters for the service. Examples: "
                '{"brightness_pct": 80} for lights, '
                '{"temperature": 21} for climate. '
                "May be omitted if the service takes no extra parameters."
            ),
        },
    },
    "required": ["domain", "service"],
}

HA_GET_HISTORY_SCHEMA = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "Full entity ID to retrieve history for.",
        },
        "hours": {
            "type": "integer",
            "description": "How many hours back to fetch (default: 24, max: 168).",
        },
    },
    "required": ["entity_id"],
}


async def ha_list_entities(domain: str = "") -> dict:
    """
    List Home Assistant entities, optionally filtered by domain (e.g. light,
    switch, sensor, binary_sensor, climate, media_player). Returns entity IDs,
    current states, and friendly names. Use this to discover what devices are
    available before calling ha_get_state or ha_call_service.
    """
    if _client is None:
        return {"error": "Home Assistant tools not initialised."}
    try:
        states = await _client.list_states()
        entities = [
            {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
            }
            for s in states
            if not domain or s["entity_id"].startswith(f"{domain}.")
        ]
        return {"entities": entities, "count": len(entities)}
    except httpx.HTTPStatusError as e:
        return {"error": f"HA API error {e.response.status_code}: {e.response.text}"}
    except httpx.TimeoutException:
        return {"error": "Home Assistant did not respond in time."}
    except Exception as e:
        logger.exception("ha_list_entities failed")
        return {"error": str(e)}


async def ha_get_state(entity_id: str) -> dict:
    """
    Get the current state and all attributes of a single Home Assistant entity.
    Use this to read sensor values, check if a light is on/off, get current
    temperature, or inspect any entity's full attribute set.
    """
    if _client is None:
        return {"error": "Home Assistant tools not initialised."}
    try:
        data = await _client.get_state(entity_id)
        return {
            "entity_id": data["entity_id"],
            "state": data["state"],
            "attributes": data.get("attributes", {}),
            "last_changed": data.get("last_changed"),
            "last_updated": data.get("last_updated"),
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"Entity '{entity_id}' not found in Home Assistant."}
        return {"error": f"HA API error {e.response.status_code}: {e.response.text}"}
    except httpx.TimeoutException:
        return {"error": "Home Assistant did not respond in time."}
    except Exception as e:
        logger.exception("ha_get_state failed")
        return {"error": str(e)}


async def ha_call_service(
    domain: str,
    service: str,
    entity_id: str = "",
    service_data: dict | None = None,
) -> dict:
    """
    Call a Home Assistant service to control a device. Examples: turn lights
    on/off, adjust brightness or color temperature, toggle switches, set
    thermostat temperature, trigger scripts or automations. Requires user
    confirmation before executing — always describe what will happen.
    Common calls: domain='light' service='turn_on' entity_id='light.living_room'
    service_data={'brightness_pct': 80}; domain='switch' service='turn_off'
    entity_id='switch.coffee_machine'.
    """
    if _client is None:
        return {"error": "Home Assistant tools not initialised."}
    try:
        data = dict(service_data or {})
        if entity_id:
            data["entity_id"] = entity_id
        result = await _client.call_service(domain, service, data)
        return {"status": "ok", "affected_states": len(result)}
    except httpx.HTTPStatusError as e:
        return {"error": f"HA API error {e.response.status_code}: {e.response.text}"}
    except httpx.TimeoutException:
        return {"error": "Home Assistant did not respond in time."}
    except Exception as e:
        logger.exception("ha_call_service failed")
        return {"error": str(e)}


async def ha_get_history(entity_id: str, hours: int = 24) -> dict:
    """
    Retrieve state history for a Home Assistant entity over the past N hours
    (default 24, max 168). Returns a timeline of state changes with timestamps.
    Use this to analyse trends — e.g. when a sensor triggered, how long a light
    was on, or temperature fluctuations over the day.
    """
    if _client is None:
        return {"error": "Home Assistant tools not initialised."}
    hours = min(max(1, hours), 168)
    try:
        raw = await _client.get_history(entity_id, hours)
        if not raw or not raw[0]:
            return {"entity_id": entity_id, "history": [], "count": 0}
        timeline = [
            {"state": s["state"], "changed": s.get("last_changed")}
            for s in raw[0]
        ]
        return {"entity_id": entity_id, "history": timeline, "count": len(timeline)}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"Entity '{entity_id}' not found."}
        return {"error": f"HA API error {e.response.status_code}: {e.response.text}"}
    except httpx.TimeoutException:
        return {"error": "Home Assistant did not respond in time."}
    except Exception as e:
        logger.exception("ha_get_history failed")
        return {"error": str(e)}
