import asyncio
import logging

from vault.vault_manager import VaultManager

logger = logging.getLogger(__name__)

_vault_manager: VaultManager | None = None


def init_vault_tools(vm: VaultManager) -> None:
    """Call once in main.py before registering vault tools."""
    global _vault_manager
    _vault_manager = vm


VAULT_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Relative path to the vault file, e.g. 'about-me.md' or "
                "'projekty/prime-agent.md'. Use list_files (vault_search with empty query) "
                "first if you are unsure of the exact path."
            ),
        }
    },
    "required": ["path"],
}

VAULT_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Relative path of the file to create or overwrite, "
                "e.g. 'osoby/jan-novak.md'. Parent directories are created automatically."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "Full file content including YAML frontmatter if desired. "
                "The 'updated' frontmatter field is set automatically."
            ),
        },
    },
    "required": ["path", "content"],
}

VAULT_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Full-text search query (case-insensitive substring match). "
                "Pass an empty string to list all vault files."
            ),
        }
    },
    "required": ["query"],
}


async def vault_read(path: str) -> str:
    """
    Read a file from the vault knowledge base by its relative path.
    Returns the full file content including frontmatter. Use this to recall
    stored information about the user, ongoing projects, people, or instructions.
    """
    if _vault_manager is None:
        return "Error: vault tools not initialised."
    try:
        return await asyncio.to_thread(_vault_manager.read, path)
    except FileNotFoundError:
        return f"File not found in vault: {path!r}. Use vault_search to discover available files."
    except Exception as exc:
        logger.exception("vault_read failed: %s", path)
        return f"Error reading vault file: {exc}"


async def vault_write(path: str, content: str) -> str:
    """
    Write or overwrite a file in the vault knowledge base.
    Creates parent directories automatically. The 'updated' frontmatter field
    is refreshed to today's date. Use this to store notes, update project status,
    record decisions, or save anything worth remembering long-term.
    Returns a confirmation string.
    """
    if _vault_manager is None:
        return "Error: vault tools not initialised."
    try:
        await asyncio.to_thread(_vault_manager.write, path, content)
        return f"Vault file written: {path}"
    except Exception as exc:
        logger.exception("vault_write failed: %s", path)
        return f"Error writing vault file: {exc}"


async def vault_search(query: str) -> str:
    """
    Full-text search across all vault files (case-insensitive).
    Pass an empty string to list all files. Returns file paths, summaries,
    and matching snippets. Use this to discover what knowledge is stored before
    reading specific files.
    """
    if _vault_manager is None:
        return "Error: vault tools not initialised."
    try:
        if not query.strip():
            return await asyncio.to_thread(_vault_manager.get_index)

        results = await asyncio.to_thread(_vault_manager.search, query)
        if not results:
            return f"No vault files found matching: {query!r}"

        lines = [f"Vault search results for {query!r}:"]
        for r in results:
            summary = f" — {r['summary']}" if r.get("summary") else ""
            snippet = f"\n    {r['snippet']}" if r.get("snippet") else ""
            lines.append(f"  {r['path']}{summary}{snippet}")
        return "\n".join(lines)
    except Exception as exc:
        logger.exception("vault_search failed: %s", query)
        return f"Error searching vault: {exc}"


VAULT_PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Relative path to an existing vault file to edit. The file must "
                "already exist — use vault_write to create new files."
            ),
        },
        "heading": {
            "type": "string",
            "description": (
                "Exact text of the Markdown section heading to replace "
                "(without the # prefix), e.g. 'Pracovní oblast' or 'Koníčky'. "
                "The match is case-insensitive. If the section does not exist it will be appended."
            ),
        },
        "new_content": {
            "type": "string",
            "description": (
                "New body text for this section (everything after the heading line). "
                "Do NOT include the heading itself. Supports full Markdown."
            ),
        },
    },
    "required": ["path", "heading", "new_content"],
}


async def vault_patch(path: str, heading: str, new_content: str) -> str:
    """
    Edit a single Markdown section in an existing vault file without touching
    the rest of the document. Use this instead of vault_write whenever you are
    updating or adding one section of an existing file. Safe for critical files
    like about-me.md — all other sections are preserved exactly as-is.
    """
    if _vault_manager is None:
        return "Error: vault tools not initialised."
    try:
        await asyncio.to_thread(_vault_manager.patch_section, path, heading, new_content)
        return f"Vault section '{heading}' updated in {path}"
    except FileNotFoundError:
        return "File not found — use vault_write to create it first"
    except Exception as exc:
        logger.exception("vault_patch failed: %s", path)
        return f"Error patching vault file: {exc}"
