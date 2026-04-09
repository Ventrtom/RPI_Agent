import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class Session:
    session_id: str
    user_id: str
    messages: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    status: str = "active"  # "active" | "closed"


class SessionManager:
    def __init__(self, timeout_minutes: int = 30):
        self._sessions: dict[str, Session] = {}
        self._timeout = timedelta(minutes=timeout_minutes)
        asyncio.get_event_loop().create_task(self._cleanup_loop())

    async def get_or_create(self, session_id: str, user_id: str) -> Session:
        if session_id not in self._sessions or self._sessions[session_id].status == "closed":
            self._sessions[session_id] = Session(session_id=session_id, user_id=user_id)
        session = self._sessions[session_id]
        session.last_activity = datetime.utcnow()
        return session

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        session = self._sessions.get(session_id)
        if session and session.status == "active":
            session.messages.append({"role": role, "content": content})
            session.last_activity = datetime.utcnow()

    async def get_history(self, session_id: str) -> list[dict]:
        session = self._sessions.get(session_id)
        if session and session.status == "active":
            return list(session.messages)
        return []

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.status = "closed"
            session.messages = []

    async def cleanup_expired(self) -> None:
        now = datetime.utcnow()
        expired = [
            sid
            for sid, session in self._sessions.items()
            if session.status == "active" and (now - session.last_activity) > self._timeout
        ]
        for sid in expired:
            await self.close_session(sid)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            await self.cleanup_expired()
