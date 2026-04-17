"""
Source introspection tools: let the agent read its own codebase.

The agent can list source files and read their contents to understand
why it behaves a certain way — without needing hardcoded documentation.
All paths are relative to the agent root and confined to it (no traversal).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_AGENT_ROOT: Path | None = None

# Files/dirs to exclude from listing (noise, not useful for self-understanding)
_EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"}
_EXCLUDE_EXTS = {".pyc", ".pyo", ".egg-info"}


def init_source_tools(agent_root: str | Path) -> None:
    """Call once in main.py with the agent's root directory path."""
    global _AGENT_ROOT
    _AGENT_ROOT = Path(agent_root).resolve()


LIST_OWN_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Relative path within the agent codebase to list, e.g. 'memory', 'tools', "
                "or '' / '.' for the root. Defaults to root."
            ),
        },
    },
    "required": [],
}

READ_OWN_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Relative path to a source file within the agent codebase, "
                "e.g. 'memory/client.py' or 'core/agent.py'."
            ),
        },
    },
    "required": ["path"],
}


def _safe_resolve(rel_path: str) -> Path | None:
    """Resolve rel_path under _AGENT_ROOT, return None if outside root."""
    try:
        resolved = (_AGENT_ROOT / rel_path).resolve()
        resolved.relative_to(_AGENT_ROOT)  # raises ValueError if outside
        return resolved
    except (ValueError, Exception):
        return None


async def list_own_source(path: str = "") -> str:
    """
    List the source files and directories of this agent's codebase at the
    given relative path (default: project root). Use this to discover which
    files exist before reading them, or to understand the overall architecture.
    Returns a tree-style listing.
    """
    if _AGENT_ROOT is None:
        return "Error: source tools not initialised — call init_source_tools() first."

    target = _safe_resolve(path or ".")
    if target is None:
        return f"Error: path '{path}' is outside the agent root."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_dir():
        return f"'{path}' is a file, not a directory. Use read_own_source to read it."

    lines: list[str] = [f"{target.relative_to(_AGENT_ROOT) or '.'}/"]

    def _walk(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for i, entry in enumerate(entries):
            if entry.name in _EXCLUDE_DIRS:
                continue
            if entry.suffix in _EXCLUDE_EXTS:
                continue
            connector = "└── " if i == len(entries) - 1 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and entry.name not in _EXCLUDE_DIRS:
                extension = "    " if i == len(entries) - 1 else "│   "
                _walk(entry, prefix + extension)

    _walk(target, "")
    return "\n".join(lines)


async def read_own_source(path: str) -> str:
    """
    Read the source code of a file in this agent's codebase. Use this to
    understand how a specific module works — for example 'memory/client.py'
    to understand how memories are stored, 'core/agent.py' to see the main
    processing loop, or 'core/prompts.py' to see the system prompt template.
    Returns the full file contents with line numbers.
    """
    if _AGENT_ROOT is None:
        return "Error: source tools not initialised — call init_source_tools() first."

    target = _safe_resolve(path)
    if target is None:
        return f"Error: path '{path}' is outside the agent root."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if target.is_dir():
        return f"'{path}' is a directory. Use list_own_source to list it."

    try:
        text = target.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading '{path}': {exc}"

    numbered = "\n".join(f"{i + 1:4d}  {line}" for i, line in enumerate(text.splitlines()))
    return f"# {path}\n\n{numbered}"
