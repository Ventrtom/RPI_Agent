from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from core.session_store import SessionStore

if TYPE_CHECKING:
    from observability.snapshots import SessionSnapshotManager
    from observability.telemetry import TelemetryLogger

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL = int(os.getenv("SESSION_CLEANUP_INTERVAL", "300"))


@dataclass
class Session:
    session_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    status: str = "active"  # "active" | "closed"
    active_voice_profile: str | None = None


class SessionManager:
    def __init__(
        self,
        timeout_minutes: int = 30,
        store: SessionStore | None = None,
        snapshot_manager: SessionSnapshotManager | None = None,
        telemetry_logger: TelemetryLogger | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._timeout = timedelta(minutes=timeout_minutes)
        self._store = store
        self._snapshot_manager = snapshot_manager
        self._telemetry_logger = telemetry_logger
        if store:
            self._load_from_store(store)
        asyncio.get_event_loop().create_task(self._cleanup_loop())

    def _load_from_store(self, store: SessionStore) -> None:
        for data in store.load_all_active():
            s = Session(
                session_id=data["session_id"],
                user_id=data["user_id"],
                messages=data["messages"],
                created_at=datetime.fromisoformat(data["created_at"]),
                last_activity=datetime.fromisoformat(data["last_activity"]),
            )
            self._sessions[s.session_id] = s
        logger.info("SessionManager: obnoveno %d sessions", len(self._sessions))

    def get_session(self, session_id: str) -> Session | None:
        """Vrátí Session bez vytvoření. None pokud neexistuje nebo je closed."""
        return self._sessions.get(session_id)

    async def get_or_create(self, session_id: str, user_id: str) -> Session:
        is_new = session_id not in self._sessions or self._sessions[session_id].status == "closed"
        if is_new:
            session = Session(session_id=session_id, user_id=user_id)
            self._sessions[session_id] = session
            if self._store:
                self._store.save_session(session_id, user_id, session.created_at, session.last_activity)
            if self._telemetry_logger:
                interface = "telegram" if session_id.startswith("telegram_") else "cli"
                asyncio.ensure_future(
                    self._telemetry_logger.log_session_start(session_id, user_id, interface)
                )
        session = self._sessions[session_id]
        session.last_activity = datetime.utcnow()
        return session

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        session = self._sessions.get(session_id)
        if session and session.status == "active":
            session.messages.append({"role": role, "content": content})
            session.last_activity = datetime.utcnow()
            if self._store:
                self._store.save_message(session_id, role, content)

    async def get_history(self, session_id: str) -> list[dict]:
        session = self._sessions.get(session_id)
        if session and session.status == "active":
            return list(session.messages)
        return []

    async def set_voice_profile(self, session_id: str, profile: str | None) -> None:
        session = self._sessions.get(session_id)
        if session and session.status == "active":
            session.active_voice_profile = profile

    async def get_voice_profile(self, session_id: str) -> str | None:
        session = self._sessions.get(session_id)
        if session and session.status == "active":
            return session.active_voice_profile
        return None

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            # Snapshot před smazáním zpráv
            if self._snapshot_manager:
                try:
                    await self._snapshot_manager.save_snapshot(session, snapshot_type="auto")
                except Exception:
                    logger.exception("Auto-snapshot selhal (session=%s)", session_id)

            msg_count = len(session.messages)
            session.status = "closed"
            session.messages = []
            if self._store:
                self._store.mark_closed(session_id)

            if self._telemetry_logger:
                asyncio.ensure_future(
                    self._telemetry_logger.log_session_end(
                        session_id,
                        {"message_count": msg_count},
                    )
                )

    async def cleanup_expired(self) -> None:
        now = datetime.utcnow()
        expired = [
            sid
            for sid, session in self._sessions.items()
            if session.status == "active" and (now - session.last_activity) > self._timeout
        ]
        for sid in expired:
            logger.info("Session %s expirovala, uzavírám", sid)
            await self.close_session(sid)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL)
            try:
                await self.cleanup_expired()
            except Exception:
                logger.exception("Chyba při čištění sessions")
