"""Async HTTP client for the Home Assistant REST API."""
import httpx
from datetime import datetime, timedelta, timezone


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    async def list_states(self) -> list[dict]:
        """GET /api/states — all entity states."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(f"{self._base_url}/api/states", headers=self._headers)
            r.raise_for_status()
            return r.json()

    async def get_state(self, entity_id: str) -> dict:
        """GET /api/states/{entity_id}."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(
                f"{self._base_url}/api/states/{entity_id}", headers=self._headers
            )
            r.raise_for_status()
            return r.json()

    async def call_service(self, domain: str, service: str, service_data: dict) -> list[dict]:
        """POST /api/services/{domain}/{service}."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._base_url}/api/services/{domain}/{service}",
                headers=self._headers,
                json=service_data,
            )
            r.raise_for_status()
            return r.json()

    async def get_history(self, entity_id: str, hours: int) -> list[list[dict]]:
        """GET /api/history/period/{start_time}?filter_entity_id={entity_id}."""
        start = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(
                f"{self._base_url}/api/history/period/{start.isoformat()}",
                headers=self._headers,
                params={"filter_entity_id": entity_id, "minimal_response": "true"},
            )
            r.raise_for_status()
            return r.json()
