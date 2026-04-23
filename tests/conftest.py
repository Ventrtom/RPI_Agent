import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _clear_subagent_model_overrides(monkeypatch):
    """Zabrání přepsání mock klienta skutečným ClaudeClientem v testech subagentů.

    test_ha_tools.py volá load_dotenv() na úrovni modulu, čímž nastaví
    GLAEDR_MODEL a VERITAS_MODEL. Tato fixture je smaže před každým testem,
    aby subagentní unit testy vždy používaly předané mock klienty.
    """
    monkeypatch.delenv("GLAEDR_MODEL", raising=False)
    monkeypatch.delenv("VERITAS_MODEL", raising=False)
    monkeypatch.delenv("AETERNA_MODEL", raising=False)
