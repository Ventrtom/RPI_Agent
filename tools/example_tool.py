from datetime import datetime, timezone

from tools import tool_registry


async def get_current_datetime() -> dict:
    """
    Ukázkový nástroj: vrátí aktuální datum a čas.
    Slouží jako šablona pro budoucí nástroje.

    Returns:
        dict s klíči: datetime (str), timezone (str)
    """
    now = datetime.now(tz=timezone.utc)
    return {
        "datetime": now.isoformat(),
        "timezone": "UTC",
    }


tool_registry.register(get_current_datetime)
