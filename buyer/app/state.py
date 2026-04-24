from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .models import EventEnvelope, SessionStatus, TaskAuthPayload


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionState:
    session_id: str
    task: str
    start_url: str
    callback_url: str
    novnc_url: str
    metadata: dict[str, Any]
    auth: TaskAuthPayload | None = None
    status: SessionStatus = SessionStatus.CREATED
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    waiting_reply_id: str | None = None
    waiting_question: str | None = None
    last_error: str | None = None
    events: list[EventEnvelope] = field(default_factory=list)
    agent_memory: list[dict[str, str]] = field(default_factory=list)
    pending_reply_text: str | None = None
    task_ref: asyncio.Task[None] | None = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)

    def touch(self) -> None:
        self.updated_at = utcnow()


class SessionConflictError(RuntimeError):
    pass


class SessionNotFoundError(RuntimeError):
    pass


class ReplyValidationError(RuntimeError):
    pass


class SessionStore:
    _TERMINAL_STATUSES = {SessionStatus.COMPLETED, SessionStatus.FAILED}

    def __init__(self, max_active_sessions: int = 1, status_ttl_sec: int | None = None) -> None:
        self._max_active_sessions = max_active_sessions
        self._status_ttl_sec = status_ttl_sec
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionState] = {}

    async def create_session(
        self,
        *,
        task: str,
        start_url: str,
        callback_url: str,
        novnc_url: str,
        metadata: dict[str, Any],
        auth: TaskAuthPayload | None,
    ) -> SessionState:
        async with self._lock:
            self._prune_expired_locked()
            active = [
                session
                for session in self._sessions.values()
                if session.status in {SessionStatus.CREATED, SessionStatus.RUNNING, SessionStatus.WAITING_USER}
            ]
            if len(active) >= self._max_active_sessions:
                raise SessionConflictError('Доступен только один активный сценарий одновременно.')

            session_id = str(uuid4())
            state = SessionState(
                session_id=session_id,
                task=task,
                start_url=start_url,
                callback_url=callback_url,
                novnc_url=novnc_url,
                metadata=metadata,
                auth=auth,
                status=SessionStatus.CREATED,
            )
            self._sessions[session_id] = state
            return state

    async def set_auth(self, session_id: str, auth: TaskAuthPayload | None) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            state.auth = auth
            state.touch()
            return state

    async def get(self, session_id: str) -> SessionState:
        async with self._lock:
            self._prune_expired_locked()
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            return state

    async def set_status(self, session_id: str, status: SessionStatus, *, error: str | None = None) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            state.status = status
            if error is not None:
                state.last_error = error
            state.touch()
            return state

    async def set_waiting_question(self, session_id: str, question: str, reply_id: str) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            state.waiting_reply_id = reply_id
            state.waiting_question = question
            state.pending_reply_text = None
            state.wake_event.clear()
            state.status = SessionStatus.WAITING_USER
            state.touch()
            return state

    async def apply_reply(self, session_id: str, reply_id: str, message: str) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            if state.status != SessionStatus.WAITING_USER:
                raise ReplyValidationError('Сессия сейчас не ожидает ответ пользователя.')
            if state.waiting_reply_id != reply_id:
                raise ReplyValidationError('Передан неверный reply_id для текущего уточнения.')
            state.pending_reply_text = message
            state.waiting_reply_id = None
            state.waiting_question = None
            state.status = SessionStatus.RUNNING
            state.touch()
            state.wake_event.set()
            return state

    async def pop_reply(self, session_id: str) -> str:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            if not state.pending_reply_text:
                raise ReplyValidationError('Ответ пользователя отсутствует.')
            value = state.pending_reply_text
            state.pending_reply_text = None
            state.touch()
            return value

    async def append_event(self, session_id: str, event: EventEnvelope) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            state.events.append(event)
            state.touch()
            return state

    async def add_agent_memory(self, session_id: str, role: str, text: str) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            state.agent_memory.append({'role': role, 'text': text})
            state.touch()
            return state

    async def get_agent_memory(self, session_id: str) -> list[dict[str, str]]:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
            return list(state.agent_memory)

    async def list_sessions(self) -> list[SessionState]:
        async with self._lock:
            self._prune_expired_locked()
            return list(self._sessions.values())

    def _prune_expired_locked(self) -> None:
        if self._status_ttl_sec is None:
            return

        deadline = utcnow().timestamp() - max(self._status_ttl_sec, 0)
        expired = [
            session_id
            for session_id, state in self._sessions.items()
            if state.status in self._TERMINAL_STATUSES and state.updated_at.timestamp() < deadline
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)
