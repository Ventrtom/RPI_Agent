"""Unit testy pro Glaedr curator mode (Fáze 4)."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.base import SubagentResult
from agents.glaedr import (
    Glaedr,
    GLAEDR_CURATOR_TOOL_WHITELIST,
    GLAEDR_TOOL_WHITELIST,
    _DEFAULT_CURATOR_STATE,
    _load_curator_state,
    _save_curator_state,
)
from tools.subagent_tools import make_memory_housekeeping_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIGEST_WITH_DUPLICATES_AND_TAGS = """\
---
summary: "Memory digest 2026-W17"
generated: "2026-04-23T02:00:00"
memory_count: 5
new_since_last_run: 3
---

# Memory Digest — 2026-W17

## Overview
Test overview.

## Key Themes

### Theme: Work
- Memory 1

## Potential Duplicates
- Group 1:
  - "User works at Acme"
  - "Tomáš works at Acme Corp"
- Group 2:
  - "User uses macOS"
  - "Tomáš has a Mac"

## Suggested Tags

### #work
- "User is preparing Q4 review"

### #project:prime
- "Agent runs on Raspberry Pi"

## Housekeeping Notes
Nothing unusual.
"""


def _make_mock_registry(include_vault_write: bool = True):
    """Vrátí mock ToolRegistry s vault_read, vault_search a volitelně vault_write."""
    schemas = [
        {"name": "vault_read", "description": "Read", "input_schema": {}},
        {"name": "vault_search", "description": "Search", "input_schema": {}},
    ]
    if include_vault_write:
        schemas.append({"name": "vault_write", "description": "Write", "input_schema": {}})

    registry = MagicMock()
    registry.get_schemas.return_value = schemas
    registry.execute = AsyncMock(return_value="tool_result")
    return registry


def _make_mock_notifier(ready: bool = True):
    notifier = MagicMock()
    notifier.ready = ready
    notifier.send = AsyncMock()
    return notifier


def _make_glaedr(
    mock_claude=None,
    mock_memory=None,
    mock_registry=None,
    mock_vault=None,
    mock_notifier=None,
):
    if mock_claude is None:
        mock_claude = AsyncMock()
        mock_claude.complete = AsyncMock(return_value=_DIGEST_WITH_DUPLICATES_AND_TAGS)
    if mock_memory is None:
        mock_memory = AsyncMock()
        mock_memory.get_all = AsyncMock(return_value=["memory 1", "memory 2"])
        mock_memory.search = AsyncMock(return_value=[])
    if mock_registry is None:
        mock_registry = _make_mock_registry()
    if mock_vault is None:
        mock_vault = MagicMock()
        mock_vault.write = MagicMock()  # sync — zabaleno asyncio.to_thread v curate()

    return Glaedr(
        claude_client=mock_claude,
        memory_client=mock_memory,
        vault_manager=mock_vault,
        tool_registry=mock_registry,
        notifier=mock_notifier,
    )


# ---------------------------------------------------------------------------
# test 1: state load default (chybí soubor)
# ---------------------------------------------------------------------------


def test_curator_state_load_default(tmp_path, monkeypatch):
    """Chybí state file → vrátí default dict s nulami."""
    monkeypatch.setenv("CURATOR_STATE_PATH", str(tmp_path / "nonexistent.json"))
    state = _load_curator_state()
    assert state == _DEFAULT_CURATOR_STATE
    assert state["last_memory_count"] == 0
    assert state["total_runs"] == 0
    assert state["last_run_at"] is None


# ---------------------------------------------------------------------------
# test 2: state save → load roundtrip
# ---------------------------------------------------------------------------


def test_curator_state_save_and_reload(tmp_path, monkeypatch):
    """save_curator_state + load_curator_state roundtrip."""
    path = str(tmp_path / "state.json")
    monkeypatch.setenv("CURATOR_STATE_PATH", path)

    data = {
        "last_run_at": "2026-04-20T02:00:00",
        "last_memory_count": 42,
        "total_runs": 3,
        "last_digest_path": "memory-digests/2026-W16.md",
    }
    _save_curator_state(data)
    loaded = _load_curator_state()

    assert loaded["last_memory_count"] == 42
    assert loaded["total_runs"] == 3
    assert loaded["last_digest_path"] == "memory-digests/2026-W16.md"
    assert loaded["last_run_at"] == "2026-04-20T02:00:00"


# ---------------------------------------------------------------------------
# test 3: curator scoped tools include vault_write; retrieve nemá
# ---------------------------------------------------------------------------


def test_curator_scoped_tools_include_vault_write():
    """Curator mode má vault_write ve scoped tools — retrieve mode nemá."""
    glaedr = _make_glaedr()

    curator_names = {t["name"] for t in glaedr._curator_scoped_tools}
    retrieve_names = {t["name"] for t in glaedr.scoped_tools}

    assert "vault_write" in curator_names
    assert "vault_write" not in retrieve_names
    assert curator_names == GLAEDR_CURATOR_TOOL_WHITELIST
    assert retrieve_names == GLAEDR_TOOL_WHITELIST


# ---------------------------------------------------------------------------
# test 4: curator vault_write restricted path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_vault_write_restricted_path():
    """Pokus zapsat mimo memory-digests/ → executor vrátí error string (ne výjimku)."""
    glaedr = _make_glaedr()

    result = await glaedr._curator_tool_executor(
        "vault_write", {"path": "osoby/evil.md", "content": "hacked"}
    )

    assert isinstance(result, str)
    assert "rejected" in result or "restricted" in result
    assert "memory-digests/" in result


@pytest.mark.asyncio
async def test_curator_vault_write_allowed_path():
    """Zápis do memory-digests/ projde (executor zavolá registry.execute)."""
    registry = _make_mock_registry(include_vault_write=True)
    glaedr = _make_glaedr(mock_registry=registry)

    result = await glaedr._curator_tool_executor(
        "vault_write", {"path": "memory-digests/2026-W17.md", "content": "digest"}
    )

    registry.execute.assert_called_once_with("vault_write", {"path": "memory-digests/2026-W17.md", "content": "digest"})
    assert result == "tool_result"


# ---------------------------------------------------------------------------
# test 5: curate dry_run → žádný zápis, žádný state update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curate_dry_run_no_writes(tmp_path, monkeypatch):
    """curate(dry_run=True) → VaultManager.write nevolán, state neaktualizován."""
    monkeypatch.setenv("CURATOR_STATE_PATH", str(tmp_path / "state.json"))

    mock_vault = MagicMock()
    mock_vault.write = MagicMock()
    glaedr = _make_glaedr(mock_vault=mock_vault)

    result = await glaedr.curate(dry_run=True)

    assert result.success is True
    mock_vault.write.assert_not_called()
    assert not (tmp_path / "state.json").exists()
    assert result.data["digest_path"] is None
    assert result.metadata["dry_run"] is True


# ---------------------------------------------------------------------------
# test 6: curate full run → digest zapsán, state aktualizován
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curate_writes_digest(tmp_path, monkeypatch):
    """Full run (dry_run=False) → VaultManager.write zavolán s cestou memory-digests/, state aktualizován."""
    monkeypatch.setenv("CURATOR_STATE_PATH", str(tmp_path / "state.json"))

    mock_vault = MagicMock()
    mock_vault.write = MagicMock()
    mock_memory = AsyncMock()
    mock_memory.get_all = AsyncMock(return_value=["m1", "m2", "m3"])
    mock_memory.search = AsyncMock(return_value=[])

    glaedr = _make_glaedr(mock_vault=mock_vault, mock_memory=mock_memory)
    result = await glaedr.curate(dry_run=False)

    assert result.success is True
    mock_vault.write.assert_called_once()
    path_arg = mock_vault.write.call_args[0][0]
    assert path_arg.startswith("memory-digests/")

    state_file = tmp_path / "state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["total_runs"] == 1
    assert state["last_memory_count"] == 3
    assert state["last_digest_path"] == path_arg


# ---------------------------------------------------------------------------
# test 7: prázdná paměť → curator zvládne, success=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curate_handles_empty_memory(tmp_path, monkeypatch):
    """get_all() vrátí [] → curator doběhne bez pádu, digest řekne o prázdné paměti."""
    monkeypatch.setenv("CURATOR_STATE_PATH", str(tmp_path / "state.json"))

    mock_memory = AsyncMock()
    mock_memory.get_all = AsyncMock(return_value=[])
    mock_memory.search = AsyncMock(return_value=[])

    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(return_value="## Overview\nMemory is empty.\n")

    glaedr = _make_glaedr(mock_memory=mock_memory, mock_claude=mock_claude)
    result = await glaedr.curate(dry_run=False)

    assert result.success is True
    assert result.data["memory_count"] == 0
    assert result.data["new_since_last_run"] == 0


# ---------------------------------------------------------------------------
# test 8: Claude selže → success=False, state neaktualizován
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curate_handles_claude_failure(tmp_path, monkeypatch):
    """Pokud Claude selže, curate vrátí success=False a state se neaktualizuje."""
    monkeypatch.setenv("CURATOR_STATE_PATH", str(tmp_path / "state.json"))

    mock_claude = AsyncMock()
    mock_claude.complete = AsyncMock(side_effect=RuntimeError("API timeout"))

    glaedr = _make_glaedr(mock_claude=mock_claude)
    result = await glaedr.curate(dry_run=False)

    assert result.success is False
    assert result.error is not None
    assert "API timeout" in result.error
    assert not (tmp_path / "state.json").exists()


# ---------------------------------------------------------------------------
# test 9: notifier zavolán po úspěšném runu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curate_notifier_called_on_success(tmp_path, monkeypatch):
    """Po successful run s dry_run=False se zavolá notifier.send()."""
    monkeypatch.setenv("CURATOR_STATE_PATH", str(tmp_path / "state.json"))

    mock_notifier = _make_mock_notifier(ready=True)
    glaedr = _make_glaedr(mock_notifier=mock_notifier)

    result = await glaedr.curate(dry_run=False)

    assert result.success is True
    mock_notifier.send.assert_called_once()
    sent_text = mock_notifier.send.call_args[0][0]
    assert "🧹" in sent_text
    assert "memory-digests/" in sent_text


# ---------------------------------------------------------------------------
# test 10: notifier nevolán při dry_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curate_notifier_not_called_on_dry_run(tmp_path, monkeypatch):
    """Při dry_run=True notifier.send() nevolán."""
    monkeypatch.setenv("CURATOR_STATE_PATH", str(tmp_path / "state.json"))

    mock_notifier = _make_mock_notifier(ready=True)
    glaedr = _make_glaedr(mock_notifier=mock_notifier)

    result = await glaedr.curate(dry_run=True)

    assert result.success is True
    mock_notifier.send.assert_not_called()


# ---------------------------------------------------------------------------
# test 11: memory_housekeeping tool — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_housekeeping_tool_success():
    """make_memory_housekeeping_tool vrátí callable; při úspěchu Glaedra vrátí dict success=True."""
    mock_glaedr = MagicMock()
    mock_glaedr.curate = AsyncMock(
        return_value=SubagentResult(
            success=True,
            summary=_DIGEST_WITH_DUPLICATES_AND_TAGS,
            data={"digest_path": "memory-digests/2026-W17.md", "memory_count": 5,
                  "duplicates_found": 2, "tags_suggested": 2, "new_since_last_run": 3},
            metadata={"latency_ms": 1200, "trace_id": "abc"},
        )
    )

    tool = make_memory_housekeeping_tool(mock_glaedr)
    result = await tool()

    assert result["success"] is True
    assert "summary" in result
    assert "data" in result
    assert result["data"]["memory_count"] == 5
    assert "error" not in result


# ---------------------------------------------------------------------------
# test 12: memory_housekeeping tool — dry_run flag propagován
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_housekeeping_tool_dry_run_flag_passed():
    """dry_run=True je správně předán do glaedr.curate()."""
    mock_glaedr = MagicMock()
    mock_glaedr.curate = AsyncMock(
        return_value=SubagentResult(
            success=True,
            summary="dry run digest",
            data={"digest_path": None, "memory_count": 0,
                  "duplicates_found": 0, "tags_suggested": 0, "new_since_last_run": 0},
            metadata={},
        )
    )

    tool = make_memory_housekeeping_tool(mock_glaedr)
    await tool(scope="week", dry_run=True)

    mock_glaedr.curate.assert_called_once_with("week", True)
