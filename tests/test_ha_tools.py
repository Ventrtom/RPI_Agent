"""Integration tests for Home Assistant tools — hit the real HA API."""
import os

import pytest
from dotenv import load_dotenv

from tools.ha_client import HAClient
from tools import ha_tools
from tools.ha_tools import (
    ha_list_entities,
    ha_get_state,
    ha_get_history,
)

load_dotenv()


@pytest.fixture(autouse=True)
def init_client():
    client = HAClient(os.environ["HA_URL"], os.environ["HA_TOKEN"])
    ha_tools.init_ha_tools(client)
    yield
    ha_tools._client = None


@pytest.mark.asyncio
async def test_list_entities():
    result = await ha_list_entities()
    assert "error" not in result
    assert "entities" in result
    assert result["count"] >= 0


@pytest.mark.asyncio
async def test_list_entities_filtered():
    result = await ha_list_entities(domain="sensor")
    assert "error" not in result
    for entity in result["entities"]:
        assert entity["entity_id"].startswith("sensor.")


@pytest.mark.asyncio
async def test_get_state_valid():
    listed = await ha_list_entities()
    assert listed["count"] > 0, "No entities found in HA — cannot run this test"
    entity_id = listed["entities"][0]["entity_id"]

    result = await ha_get_state(entity_id)
    assert "error" not in result
    assert result["entity_id"] == entity_id
    assert "state" in result
    assert "attributes" in result


@pytest.mark.asyncio
async def test_get_state_invalid():
    result = await ha_get_state("sensor.neexistuje_xyz_999")
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_get_history():
    listed = await ha_list_entities()
    assert listed["count"] > 0, "No entities found in HA — cannot run this test"
    entity_id = listed["entities"][0]["entity_id"]

    result = await ha_get_history(entity_id, hours=1)
    assert "error" not in result
    assert result["entity_id"] == entity_id
    assert "history" in result
    assert "count" in result


@pytest.mark.asyncio
async def test_uninitialized_client():
    ha_tools._client = None
    for coro in [
        ha_list_entities(),
        ha_get_state("light.test"),
        ha_get_history("light.test"),
    ]:
        result = await coro
        assert result == {"error": "Home Assistant tools not initialised."}
