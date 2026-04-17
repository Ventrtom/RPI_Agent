"""
Google Calendar and Gmail tools for the agent.

Wraps the synchronous Google API client in asyncio.to_thread() so it fits
naturally into the async tool use loop without blocking the event loop.
"""

import asyncio
import base64
import logging
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from credentials.google_auth import get_credentials

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Input schemas for Claude tool use API
# ---------------------------------------------------------------------------

GET_CALENDAR_EVENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "days_ahead": {
            "type": "integer",
            "description": (
                "How many days ahead to look for events (default: 7)"
                "Use 1 to see the rest of today and tomorrow, 7 for a week. "
                "Never use 0 — it returns nothing."
            )
        },
    },
    "required": [],
}

CREATE_CALENDAR_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Event title / summary",
        },
        "start_datetime": {
            "type": "string",
            "description": "Event start time in ISO 8601 format, e.g. 2024-06-15T14:00:00",
        },
        "duration_minutes": {
            "type": "integer",
            "description": "Duration of the event in minutes (default: 60)",
        },
        "description": {
            "type": "string",
            "description": "Optional event description / notes",
        },
        "location": {
            "type": "string",
            "description": "Optional event location",
        },
    },
    "required": ["title", "start_datetime"],
}

SEND_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": (
                "Recipient — either an email address or a contact name. "
                "If a name is given, it will be resolved against the contact list."
            ),
        },
        "subject": {
            "type": "string",
            "description": "Email subject line",
        },
        "body": {
            "type": "string",
            "description": "Plain-text email body",
        },
    },
    "required": ["to", "subject", "body"],
}

DELETE_CALENDAR_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {
            "type": "string",
            "description": "The Google Calendar event ID to delete (from the Prime Agent calendar).",
        },
    },
    "required": ["event_id"],
}

FIND_FREE_SLOTS_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "Date to check for free slots, in YYYY-MM-DD format.",
        },
        "duration_minutes": {
            "type": "integer",
            "description": "Desired slot duration in minutes (default: 60).",
        },
    },
    "required": ["date"],
}

# ---------------------------------------------------------------------------
# Synchronous helpers (run inside asyncio.to_thread)
# ---------------------------------------------------------------------------

_PRIME_AGENT_CALENDAR = "Prime Agent"

_prime_calendar_id: str | None = None
def _get_or_create_prime_calendar(service) -> str:
    """Return the calendarId of the 'Prime Agent' calendar, creating it if needed."""
    global _prime_calendar_id
    if _prime_calendar_id:
        return _prime_calendar_id
    calendars = service.calendarList().list().execute().get("items", [])
    for cal in calendars:
        if cal.get("summary") == _PRIME_AGENT_CALENDAR:
            _prime_calendar_id = cal["id"]
            return _prime_calendar_id

    logger.info("Creating '%s' calendar", _PRIME_AGENT_CALENDAR)
    created = service.calendars().insert(body={"summary": _PRIME_AGENT_CALENDAR}).execute()
    _prime_calendar_id = created["id"]
    return _prime_calendar_id


def _fetch_calendar_events(days_ahead: int) -> dict:
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.now(tz=timezone.utc)
    time_min = now.isoformat()
    # days_ahead=0 → zbytek dnešního dne místo prázdného okna
    if days_ahead == 0:
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        time_max = end_of_day.isoformat()
    else:
        time_max = (now + timedelta(days=days_ahead)).isoformat()

    calendars = service.calendarList().list().execute().get("items", [])
    events: list[dict] = []

    for cal in calendars:
        cal_id = cal["id"]
        cal_name = cal.get("summary", cal_id)
        result = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        for item in result.get("items", []):
            start_raw = item.get("start", {})
            end_raw = item.get("end", {})
            events.append(
                {
                    "title": item.get("summary", "(no title)"),
                    "start": start_raw.get("dateTime") or start_raw.get("date", ""),
                    "end": end_raw.get("dateTime") or end_raw.get("date", ""),
                    "location": item.get("location", ""),
                    "calendar": cal_name,
                }
            )

    events.sort(key=lambda e: e["start"])
    return {"events": events, "count": len(events)}


def _create_calendar_event(
    title: str,
    start_datetime: str,
    duration_minutes: int,
    description: str,
    location: str,
) -> dict:
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    calendar_id = _get_or_create_prime_calendar(service)

    start_dt = datetime.fromisoformat(start_datetime)
    # If the datetime has no tzinfo, treat it as local time (aware)
    if start_dt.tzinfo is None:
        start_dt = start_dt.astimezone()
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    body: dict = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    return {
        "status": "created",
        "event_id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "calendar": _PRIME_AGENT_CALENDAR,
    }


def _send_email(to: str, subject: str, body: str) -> dict:
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"status": "sent", "message_id": sent.get("id")}


def _delete_calendar_event(event_id: str) -> dict:
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    calendars = service.calendarList().list().execute().get("items", [])
    calendar_id = next(
        (c["id"] for c in calendars if c.get("summary") == _PRIME_AGENT_CALENDAR),
        None
    )
    if calendar_id is None:
        return {"status": "failed", "error": "Prime Agent calendar does not exist."}

    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return {"status": "deleted", "event_id": event_id}


def _find_free_slots(date: str, duration_minutes: int) -> dict:
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    # Build aware datetimes for the requested day (local timezone)
    day_start = datetime.fromisoformat(f"{date}T08:00:00").astimezone()
    day_end = datetime.fromisoformat(f"{date}T20:00:00").astimezone()

    calendars = service.calendarList().list().execute().get("items", [])
    busy: list[tuple[datetime, datetime]] = []

    for cal in calendars:
        result = (
            service.events()
            .list(
                calendarId=cal["id"],
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        for item in result.get("items", []):
            start_raw = item.get("start", {}).get("dateTime")
            end_raw = item.get("end", {}).get("dateTime")
            if start_raw and end_raw:
                busy.append(
                    (
                        datetime.fromisoformat(start_raw).astimezone(),
                        datetime.fromisoformat(end_raw).astimezone(),
                    )
                )

    # Merge overlapping busy intervals
    busy.sort(key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = []
    for start, end in busy:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Find free windows
    slot_delta = timedelta(minutes=duration_minutes)
    free_slots: list[dict] = []
    cursor = day_start

    for busy_start, busy_end in merged:
        if cursor + slot_delta <= busy_start:
            free_slots.append(
                {
                    "start": cursor.strftime("%H:%M"),
                    "end": busy_start.strftime("%H:%M"),
                }
            )
        cursor = max(cursor, busy_end)

    if cursor + slot_delta <= day_end:
        free_slots.append({"start": cursor.strftime("%H:%M"), "end": "20:00"})

    return {"date": date, "duration_minutes": duration_minutes, "free_slots": free_slots}


# ---------------------------------------------------------------------------
# Public async tool functions
# ---------------------------------------------------------------------------


async def get_calendar_events(days_ahead: int = 7) -> dict:
    """
    Return upcoming calendar events from all readable calendars.
    Use this when the user asks about their schedule, upcoming meetings,
    appointments, or what is on their calendar. days_ahead controls how
    far into the future to look (default: 7 days).
    Returns a list of events with title, start time, end time, location,
    and calendar name, sorted chronologically.
    IMPORTANT: days_ahead=0 is treated as 'rest of today'. 
    Use 7 (default) for a week view, 1 for today+tomorrow.
    Never call with days_ahead=0 expecting a full day — use the default instead.
    """
    try:
        return await asyncio.to_thread(_fetch_calendar_events, days_ahead)
    except Exception as e:
        logger.exception("get_calendar_events failed")
        return {"status": "failed", "error": str(e)}


async def create_calendar_event(
    title: str,
    start_datetime: str,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
) -> dict:
    """
    Create a new event in the 'Prime Agent' calendar.
    Use this when the user asks to add, schedule, or create a calendar event
    or appointment. title is required. start_datetime must be ISO 8601
    (e.g. '2024-06-15T14:00:00'). duration_minutes defaults to 60.
    description and location are optional. Returns the created event ID
    and a link to view it in Google Calendar.
    """
    try:
        return await asyncio.to_thread(
            _create_calendar_event,
            title,
            start_datetime,
            duration_minutes,
            description,
            location,
        )
    except Exception as e:
        logger.exception("create_calendar_event failed")
        return {"status": "failed", "error": str(e)}


async def send_email(to: str, subject: str, body: str) -> dict:
    """
    Send an email via Gmail.
    Use this when the user asks to send, write, or compose an email.
    'to' can be a contact name or an email address — the contact list is
    always checked before sending. If the recipient is not in the contact
    list the send is blocked and Tomas is asked to add the contact first.
    subject is the email subject, body is the plain-text message content.
    Returns the sent message ID on success.
    """
    try:
        # Lazy import to avoid circular dependencies
        from tools.contact_tools import get_contact_by_name

        if "@" in to:
            # Looks like an email address — verify it exists in contacts
            from tools.contact_tools import get_contacts
            result = await get_contacts(query=to)
            found = any(
                c.get("email", "").lower() == to.lower()
                for c in result.get("contacts", [])
            )
            if not found:
                return {
                    "status": "blocked",
                    "reason": "Email not in contact list. Ask Tomas to add the contact first.",
                }
            resolved_email = to
        else:
            # Treat 'to' as a name — resolve via contact list
            result = await get_contact_by_name(to)
            if "contact" not in result:
                return {
                    "status": "blocked",
                    "reason": "Contact not found. Ask Tomas to add the contact first.",
                }
            resolved_email = result["contact"].get("email", "")
            if not resolved_email:
                return {
                    "status": "blocked",
                    "reason": f"Contact '{to}' has no email address. Ask Tomas to update the contact first.",
                }

        return await asyncio.to_thread(_send_email, resolved_email, subject, body)
    except Exception as e:
        logger.exception("send_email failed")
        return {"status": "failed", "error": str(e)}


async def delete_calendar_event(event_id: str) -> dict:
    """
    Delete an event from the Prime Agent calendar by its event ID.
    Only deletes events that belong to the 'Prime Agent' calendar — never
    touches events in other calendars. Use this when Tomas asks to remove,
    cancel, or delete a calendar event that Prime Agent created.
    Returns confirmation on success or an error if the event is not found.
    """
    try:
        return await asyncio.to_thread(_delete_calendar_event, event_id)
    except Exception as e:
        logger.exception("delete_calendar_event failed")
        return {"status": "failed", "error": str(e)}


async def find_free_slots(date: str, duration_minutes: int = 60) -> dict:
    """
    Find free time slots on a given date by checking all existing calendar events.
    Searches all calendars and returns available windows during working hours
    (08:00–20:00) that are at least duration_minutes long.
    date must be in YYYY-MM-DD format. duration_minutes defaults to 60.
    Use this when Tomas asks when he is free, wants to find a time to schedule
    something, or asks for available slots on a particular day.
    Returns a list of free windows with start and end times.
    IMPORTANT: all-day events like vacation or holiday might be skipped from from the filter therefore you might propo free time even if there is such event which is fine, just to know about it. 
    """
    try:
        return await asyncio.to_thread(_find_free_slots, date, duration_minutes)
    except Exception as e:
        logger.exception("find_free_slots failed")
        return {"status": "failed", "error": str(e)}
