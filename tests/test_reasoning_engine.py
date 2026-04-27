"""Unit testy pro reasoning/engine.py — veřejné API a needs_iteration logika.

Pokrývají interface který volá main.py a core/agent.py, aby budoucí
refaktory (přidání/odebrání tříd. atributů) okamžitě selhaly tady,
ne až za runtime při startu agenta.
"""
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from reasoning.engine import ReasoningEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(claude_response: str = "OK") -> ReasoningEngine:
    claude = MagicMock()
    claude.complete = AsyncMock(return_value=claude_response)
    return ReasoningEngine(claude_client=claude)


# ---------------------------------------------------------------------------
# test 1: instantiace — žádné chybějící atributy při inicializaci
# ---------------------------------------------------------------------------


def test_reasoning_engine_instantiates():
    """ReasoningEngine se inicializuje bez výjimky — zachytí AttributeError jako MAX_ITERATIONS."""
    engine = _make_engine()
    assert engine is not None


# ---------------------------------------------------------------------------
# test 2: veřejné API — metody očekávané v main.py / agent_core
# ---------------------------------------------------------------------------


def test_reasoning_engine_public_api():
    """Třída má metody needs_iteration a process — main.py a agent_core je volají."""
    engine = _make_engine()
    assert callable(engine.needs_iteration)
    assert callable(engine.process)


# ---------------------------------------------------------------------------
# test 3: žádná třídní konstanta MAX_ITERATIONS (byla odstraněna)
# ---------------------------------------------------------------------------


def test_reasoning_engine_no_max_iterations_class_attr():
    """MAX_ITERATIONS byl odstraněn; reference v main.py způsobila runtime crash.

    Tento test zabrání zpětnému přidání bez aktualizace main.py — nebo naopak:
    pokud se konstanta vrátí, musí se vrátit i reference v main.py.
    Pokud test selže, zkontroluj main.py a synchronizuj.
    """
    assert not hasattr(ReasoningEngine, "MAX_ITERATIONS"), (
        "MAX_ITERATIONS byl přidán zpět do ReasoningEngine — "
        "aktualizuj main.py aby na něj odkazoval (nebo test uprav)."
    )


# ---------------------------------------------------------------------------
# test 4: needs_iteration — krátký dotaz bez klíčových slov → False
# ---------------------------------------------------------------------------


def test_needs_iteration_short_simple_returns_false(monkeypatch):
    monkeypatch.delenv("SKIP_REASONING", raising=False)
    engine = _make_engine()
    assert engine.needs_iteration("Ahoj") is False
    assert engine.needs_iteration("Co je 2+2?") is False


# ---------------------------------------------------------------------------
# test 5: needs_iteration — dlouhý text → True
# ---------------------------------------------------------------------------


def test_needs_iteration_long_text_returns_true(monkeypatch):
    monkeypatch.delenv("SKIP_REASONING", raising=False)
    engine = _make_engine()
    long_text = "a" * 151
    assert engine.needs_iteration(long_text) is True


# ---------------------------------------------------------------------------
# test 6: needs_iteration — více otazníků → True
# ---------------------------------------------------------------------------


def test_needs_iteration_multiple_questions_returns_true(monkeypatch):
    monkeypatch.delenv("SKIP_REASONING", raising=False)
    engine = _make_engine()
    assert engine.needs_iteration("Kdy? Jak? Proč?") is True


# ---------------------------------------------------------------------------
# test 7: needs_iteration — SKIP_REASONING env → vždy False
# ---------------------------------------------------------------------------


def test_needs_iteration_skip_env_always_false(monkeypatch):
    monkeypatch.setenv("SKIP_REASONING", "true")
    engine = _make_engine()
    assert engine.needs_iteration("a" * 200) is False
    assert engine.needs_iteration("Kdy? Jak? Proč?") is False


# ---------------------------------------------------------------------------
# test 8: process — přímá odpověď pro jednoduchý dotaz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_direct_response_for_simple_input(monkeypatch):
    """Krátký dotaz bez iterace → process vrátí Claude odpověď přímo."""
    monkeypatch.delenv("SKIP_REASONING", raising=False)
    monkeypatch.setenv("REASONING_CONFIDENCE_PROBE", "false")

    engine = _make_engine(claude_response="Přímá odpověď")
    result = await engine.process(
        user_input="Ahoj",
        system_prompt="Jsi asistent.",
        messages=[{"role": "user", "content": "Ahoj"}],
        tools=None,
        tool_executor=None,
    )
    assert result == "Přímá odpověď"
