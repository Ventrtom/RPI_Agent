"""
Contact list tools for the agent.

Manages a persistent contact list stored in data/contacts.json.
All file I/O is wrapped in asyncio.to_thread() to avoid blocking the event loop.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CONTACTS_PATH = Path(__file__).parent.parent / "data" / "contacts.json"

# ---------------------------------------------------------------------------
# Input schemas for Claude tool use API
# ---------------------------------------------------------------------------

GET_CONTACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Optional search term. Filters contacts by name, email, or tag. "
                "Leave empty to return all contacts."
            ),
        },
    },
    "required": [],
}

GET_CONTACT_BY_NAME_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Full or partial name of the contact to look up.",
        },
    },
    "required": ["name"],
}

ADD_CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Full name of the contact (required).",
        },
        "email": {
            "type": "string",
            "description": "Email address of the contact.",
        },
        "phone": {
            "type": "string",
            "description": "Phone number of the contact.",
        },
        "notes": {
            "type": "string",
            "description": "Any notes or context about this contact.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of tags to categorise the contact (e.g. 'family', 'work').",
        },
    },
    "required": ["name"],
}

REMOVE_CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Full or partial name of the contact to remove.",
        },
    },
    "required": ["name"],
}

# ---------------------------------------------------------------------------
# Synchronous file helpers (run inside asyncio.to_thread)
# ---------------------------------------------------------------------------


def _load_contacts() -> list[dict]:
    """Read contacts from disk. Returns empty list if file does not exist."""
    if not _CONTACTS_PATH.exists():
        return []
    with open(_CONTACTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_contacts(contacts: list[dict]) -> None:
    """Write contacts list to disk, creating the file if necessary."""
    _CONTACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONTACTS_PATH, "w", encoding="utf-8") as f:
        json.dump(contacts, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Public async tool functions
# ---------------------------------------------------------------------------


async def get_contacts(query: str = "") -> dict:
    """
    Return all contacts or filter by name, email, or tag if a query is provided.
    Use this when looking up someone's email, phone number, or contact details,
    or when Tomas wants to list his contacts. Pass an empty query to get everyone.
    Returns a list of matching contacts with name, email, phone, notes, and tags.
    """
    try:
        contacts = await asyncio.to_thread(_load_contacts)
        if not query:
            return {"contacts": contacts, "count": len(contacts)}

        q = query.lower()
        results = [
            c for c in contacts
            if q in c.get("name", "").lower()
            or q in c.get("email", "").lower()
            or any(q in tag.lower() for tag in c.get("tags", []))
        ]
        return {"contacts": results, "count": len(results)}
    except Exception as e:
        logger.exception("get_contacts failed")
        return {"status": "failed", "error": str(e)}


async def get_contact_by_name(name: str) -> dict:
    """
    Return the single best-matching contact for a given name (case-insensitive).
    Use this before sending an email when you have a person's name but not their
    email address. Returns the contact dict on success, or an error if not found.
    Prefers exact matches, then falls back to partial (substring) matches.
    """
    try:
        contacts = await asyncio.to_thread(_load_contacts)
        name_lower = name.lower()

        # Exact match first
        for c in contacts:
            if c.get("name", "").lower() == name_lower:
                return {"contact": c}

        # Partial match
        matches = [c for c in contacts if name_lower in c.get("name", "").lower()]
        if len(matches) == 1:
            return {"contact": matches[0]}
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "message": f"Multiple contacts match '{name}'. Pick the most appropriate one based on tags and notes.",
                "matches": matches,
            }

        return {"status": "not_found", "error": f"No contact found matching '{name}'."}
    except Exception as e:
        logger.exception("get_contact_by_name failed")
        return {"status": "failed", "error": str(e)}


async def add_contact(
    name: str,
    email: str = "",
    phone: str = "",
    notes: str = "",
    tags: list = [],
) -> dict:
    """
    Add a new contact or update an existing one (matched by name, case-insensitive).
    Use this when Tomas asks to save, add, or update a contact in his contact list.
    If a contact with the same name already exists it will be overwritten with the
    new values. Returns the saved contact on success.
    """
    try:
        contacts = await asyncio.to_thread(_load_contacts)

        new_contact = {
            "name": name,
            "email": email,
            "phone": phone,
            "notes": notes,
            "tags": list(tags),
        }

        name_lower = name.lower()
        for i, c in enumerate(contacts):
            if c.get("name", "").lower() == name_lower:
                contacts[i] = new_contact
                await asyncio.to_thread(_save_contacts, contacts)
                return {"status": "updated", "contact": new_contact}

        contacts.append(new_contact)
        await asyncio.to_thread(_save_contacts, contacts)
        return {"status": "added", "contact": new_contact}
    except Exception as e:
        logger.exception("add_contact failed")
        return {"status": "failed", "error": str(e)}


async def remove_contact(name: str) -> dict:
    """
    Remove a contact by name (case-insensitive match).
    Use this when Tomas asks to delete or remove a contact from his contact list.
    Returns a confirmation message on success, or an error if the contact is not found.
    """
    try:
        contacts = await asyncio.to_thread(_load_contacts)
        name_lower = name.lower()

        updated = [c for c in contacts if c.get("name", "").lower() != name_lower]
        if len(updated) == len(contacts):
            return {"status": "not_found", "error": f"No contact found matching '{name}'."}

        await asyncio.to_thread(_save_contacts, updated)
        return {"status": "removed", "name": name}
    except Exception as e:
        logger.exception("remove_contact failed")
        return {"status": "failed", "error": str(e)}
