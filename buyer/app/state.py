from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
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
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    waiting_deadline_at: datetime | None = None
    runtime_worker_id: str | None = None
    runtime_claim_token: str | None = None
    runtime_heartbeat_at: datetime | None = None
    browser_slot_id: str | None = None
    browser_cdp_endpoint: str | None = None
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


@dataclass(frozen=True)
class ReplySubmissionResult:
    state: SessionState
    accepted: bool
    reason_code: str | None = None


class SessionRepository(Protocol):
    async def initialize(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def create_session(self, state: SessionState) -> None:
        pass

    async def get_session(self, session_id: str) -> SessionState | None:
        pass

    async def list_sessions(self) -> list[SessionState]:
        pass

    async def update_session(self, state: SessionState) -> None:
        pass

    async def delete_sessions(self, session_ids: list[str]) -> None:
        pass

    async def update_auth_context(self, session_id: str, context: dict[str, Any]) -> None:
        pass

    async def replace_artifacts(self, session_id: str, artifacts: list[dict[str, Any]]) -> None:
        pass

    async def mark_event_delivery(self, event_id: str, status: str, error: str | None = None) -> None:
        pass

    async def claim_next_queued_session(
        self,
        *,
        worker_id: str,
        claim_token: str,
        claimed_at: datetime,
    ) -> SessionState | None:
        pass

    async def fail_stale_runtime_sessions(self, *, failed_at: datetime, reason: str) -> int:
        pass


class InMemorySessionRepository:
    def __init__(self, *, persist_auth_payload: bool = True) -> None:
        self._persist_auth_payload = persist_auth_payload
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionState] = {}

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def create_session(self, state: SessionState) -> None:
        async with self._lock:
            self._sessions[state.session_id] = _clone_state(state, persist_auth=self._persist_auth_payload)

    async def get_session(self, session_id: str) -> SessionState | None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            return _clone_state(state)

    async def list_sessions(self) -> list[SessionState]:
        async with self._lock:
            return [_clone_state(state) for state in self._sessions.values()]

    async def update_session(self, state: SessionState) -> None:
        async with self._lock:
            self._sessions[state.session_id] = _clone_state(state, persist_auth=self._persist_auth_payload)

    async def delete_sessions(self, session_ids: list[str]) -> None:
        async with self._lock:
            for session_id in session_ids:
                self._sessions.pop(session_id, None)

    async def update_auth_context(self, session_id: str, context: dict[str, Any]) -> None:
        _ = session_id, context
        return

    async def replace_artifacts(self, session_id: str, artifacts: list[dict[str, Any]]) -> None:
        _ = session_id, artifacts
        return

    async def mark_event_delivery(self, event_id: str, status: str, error: str | None = None) -> None:
        _ = event_id, status, error
        return

    async def claim_next_queued_session(
        self,
        *,
        worker_id: str,
        claim_token: str,
        claimed_at: datetime,
    ) -> SessionState | None:
        async with self._lock:
            queued = sorted(
                (state for state in self._sessions.values() if state.status == SessionStatus.QUEUED),
                key=lambda item: (item.queued_at or item.created_at, item.session_id),
            )
            if not queued:
                return None
            state = _clone_state(queued[0], persist_auth=self._persist_auth_payload)
            state.status = SessionStatus.RUNNING
            state.started_at = state.started_at or claimed_at
            state.updated_at = claimed_at
            state.runtime_worker_id = worker_id
            state.runtime_claim_token = claim_token
            state.runtime_heartbeat_at = claimed_at
            self._sessions[state.session_id] = _clone_state(state, persist_auth=self._persist_auth_payload)
            return _clone_state(state)

    async def fail_stale_runtime_sessions(self, *, failed_at: datetime, reason: str) -> int:
        async with self._lock:
            count = 0
            for state in list(self._sessions.values()):
                if state.status not in {SessionStatus.RUNNING, SessionStatus.WAITING_USER}:
                    continue
                if not state.runtime_claim_token:
                    continue
                failed = _clone_state(state, persist_auth=self._persist_auth_payload)
                failed.status = SessionStatus.FAILED
                failed.last_error = reason
                failed.finished_at = failed_at
                failed.updated_at = failed_at
                failed.runtime_worker_id = None
                failed.runtime_claim_token = None
                failed.runtime_heartbeat_at = None
                failed.waiting_deadline_at = None
                self._sessions[failed.session_id] = _clone_state(failed, persist_auth=self._persist_auth_payload)
                count += 1
            return count


class SessionStore:
    _TERMINAL_STATUSES = {SessionStatus.COMPLETED, SessionStatus.FAILED}

    def __init__(
        self,
        max_active_sessions: int = 1,
        status_ttl_sec: int | None = None,
        clock: Callable[[], datetime] = utcnow,
        repository: SessionRepository | None = None,
    ) -> None:
        self._max_active_sessions = max_active_sessions
        self._status_ttl_sec = status_ttl_sec
        self._clock = clock
        self._lock = asyncio.Lock()
        self._repository = repository or InMemorySessionRepository()
        self._wake_events: dict[str, asyncio.Event] = {}
        self._task_refs: dict[str, asyncio.Task[None]] = {}
        self._runtime_auth: dict[str, TaskAuthPayload] = {}
        self._runtime_sessions: set[str] = set()

    async def initialize(self) -> None:
        await self._repository.initialize()

    async def aclose(self) -> None:
        await self._repository.aclose()

    def set_task_ref(self, session_id: str, task_ref: asyncio.Task[None]) -> None:
        self._task_refs[session_id] = task_ref
        self._runtime_sessions.add(session_id)

    async def claim_next_queued_session(self, *, worker_id: str, claim_token: str) -> SessionState | None:
        async with self._lock:
            state = await self._repository.claim_next_queued_session(
                worker_id=worker_id,
                claim_token=claim_token,
                claimed_at=self._clock(),
            )
            if state is None:
                return None
            self._runtime_sessions.add(state.session_id)
            return self._attach_runtime(state)

    async def fail_stale_runtime_sessions(self, *, reason: str) -> int:
        async with self._lock:
            count = await self._repository.fail_stale_runtime_sessions(failed_at=self._clock(), reason=reason)
            if count:
                sessions = await self._repository.list_sessions()
                stale_ids = {
                    state.session_id
                    for state in sessions
                    if state.status == SessionStatus.FAILED and state.last_error == reason
                }
                for session_id in stale_ids:
                    self._wake_events.pop(session_id, None)
                    self._task_refs.pop(session_id, None)
                    self._runtime_auth.pop(session_id, None)
                    self._runtime_sessions.discard(session_id)
            return count

    async def assign_browser_slot(
        self,
        session_id: str,
        *,
        slot_id: str,
        cdp_endpoint: str,
        novnc_url: str,
    ) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            state.browser_slot_id = slot_id
            state.browser_cdp_endpoint = cdp_endpoint
            state.novnc_url = novnc_url
            self._touch_locked(state)
            await self._repository.update_session(state)
            return self._attach_runtime(state)

    async def requeue_session(self, session_id: str) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            state.status = SessionStatus.QUEUED
            state.started_at = None
            state.runtime_worker_id = None
            state.runtime_claim_token = None
            state.runtime_heartbeat_at = None
            state.browser_slot_id = None
            state.browser_cdp_endpoint = None
            state.novnc_url = ''
            self._touch_locked(state)
            await self._repository.update_session(state)
            self._runtime_sessions.discard(session_id)
            self._task_refs.pop(session_id, None)
            return self._attach_runtime(state)

    async def set_auth_context(self, session_id: str, context: dict[str, Any]) -> None:
        async with self._lock:
            await self._get_locked(session_id)
            await self._repository.update_auth_context(session_id, context)

    async def record_artifacts(self, session_id: str, artifacts: list[dict[str, Any]]) -> None:
        async with self._lock:
            await self._get_locked(session_id)
            await self._repository.replace_artifacts(session_id, artifacts)

    async def mark_event_delivery(self, event_id: str, status: str, error: str | None = None) -> None:
        await self._repository.mark_event_delivery(event_id, status, error)

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
            sessions = await self._prune_and_list_locked()
            active = [
                session
                for session in sessions
                if self._is_active_in_current_runtime(session)
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
                status=SessionStatus.QUEUED,
            )
            now = self._clock()
            state.created_at = now
            state.updated_at = now
            state.queued_at = now
            if auth is not None:
                self._runtime_auth[session_id] = auth
            await self._repository.create_session(state)
            self._runtime_sessions.add(session_id)
            return self._attach_runtime(state)

    async def set_auth(self, session_id: str, auth: TaskAuthPayload | None) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            state.auth = auth
            if auth is None:
                self._runtime_auth.pop(session_id, None)
            else:
                self._runtime_auth[session_id] = auth
            self._touch_locked(state)
            await self._repository.update_session(state)
            return self._attach_runtime(state)

    async def get(self, session_id: str) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            return self._attach_runtime(state)

    async def set_status(self, session_id: str, status: SessionStatus, *, error: str | None = None) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            state.status = status
            if error is not None:
                state.last_error = error
            if status == SessionStatus.RUNNING:
                state.started_at = state.started_at or self._clock()
                state.runtime_heartbeat_at = self._clock()
            if status in self._TERMINAL_STATUSES:
                state.finished_at = state.finished_at or self._clock()
                state.runtime_worker_id = None
                state.runtime_claim_token = None
                state.runtime_heartbeat_at = None
                state.waiting_deadline_at = None
            self._touch_locked(state)
            await self._repository.update_session(state)
            if status in self._TERMINAL_STATUSES:
                self._runtime_sessions.discard(session_id)
            return self._attach_runtime(state)

    async def set_waiting_question(
        self,
        session_id: str,
        question: str,
        reply_id: str,
        *,
        deadline_at: datetime | None = None,
    ) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            state.waiting_reply_id = reply_id
            state.waiting_question = question
            state.waiting_deadline_at = deadline_at
            state.pending_reply_text = None
            self._wake_for(session_id).clear()
            state.status = SessionStatus.WAITING_USER
            self._touch_locked(state)
            await self._repository.update_session(state)
            return self._attach_runtime(state)

    async def apply_reply(self, session_id: str, reply_id: str, message: str) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            if state.status != SessionStatus.WAITING_USER:
                raise ReplyValidationError('Сессия сейчас не ожидает ответ пользователя.')
            if state.waiting_reply_id != reply_id:
                raise ReplyValidationError('Передан неверный reply_id для текущего уточнения.')
            if state.waiting_deadline_at is not None and self._clock() >= state.waiting_deadline_at:
                raise ReplyValidationError('waiting_user_timeout')
            if not self._is_active_in_current_runtime(state):
                raise ReplyValidationError(
                    'Сессия ожидает ответ, но в текущем процессе нет активного runner. '
                    'Продолжение после рестарта пока не поддерживается.'
                )
            state.pending_reply_text = message
            state.waiting_reply_id = None
            state.waiting_question = None
            state.waiting_deadline_at = None
            state.status = SessionStatus.RUNNING
            self._touch_locked(state)
            await self._repository.update_session(state)
            self._wake_for(session_id).set()
            return self._attach_runtime(state)

    async def expire_waiting_reply_if_deadline_passed(self, session_id: str, *, reason: str) -> SessionState | None:
        async with self._lock:
            state = await self._get_locked(session_id)
            if state.status != SessionStatus.WAITING_USER or state.waiting_deadline_at is None:
                return None
            if self._clock() < state.waiting_deadline_at:
                return None
            state.status = SessionStatus.FAILED
            state.last_error = reason
            state.finished_at = self._clock()
            state.waiting_deadline_at = None
            state.runtime_worker_id = None
            state.runtime_claim_token = None
            state.runtime_heartbeat_at = None
            self._touch_locked(state)
            await self._repository.update_session(state)
            self._runtime_sessions.discard(session_id)
            self._wake_for(session_id).set()
            return self._attach_runtime(state)

    async def pop_reply(self, session_id: str) -> str:
        async with self._lock:
            state = await self._get_locked(session_id)
            if not state.pending_reply_text:
                raise ReplyValidationError('Ответ пользователя отсутствует.')
            value = state.pending_reply_text
            state.pending_reply_text = None
            self._touch_locked(state)
            await self._repository.update_session(state)
            return value

    async def append_event(self, session_id: str, event: EventEnvelope) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            state.events.append(event)
            self._touch_locked(state)
            await self._repository.update_session(state)
            return self._attach_runtime(state)

    async def add_agent_memory(self, session_id: str, role: str, text: str) -> SessionState:
        async with self._lock:
            state = await self._get_locked(session_id)
            state.agent_memory.append({'role': role, 'text': text})
            self._touch_locked(state)
            await self._repository.update_session(state)
            return self._attach_runtime(state)

    async def get_agent_memory(self, session_id: str) -> list[dict[str, str]]:
        async with self._lock:
            state = await self._get_locked(session_id)
            return deepcopy(state.agent_memory)

    async def list_sessions(self) -> list[SessionState]:
        async with self._lock:
            sessions = await self._prune_and_list_locked()
            return [self._attach_runtime(state) for state in sessions]

    async def _get_locked(self, session_id: str) -> SessionState:
        state = await self._repository.get_session(session_id)
        if state is None:
            raise SessionNotFoundError(f'Сессия {session_id} не найдена.')
        return state

    async def _prune_and_list_locked(self) -> list[SessionState]:
        sessions = await self._repository.list_sessions()
        if self._status_ttl_sec is None:
            return sessions
        deadline = self._clock().timestamp() - max(self._status_ttl_sec, 0)
        expired = [
            state.session_id
            for state in sessions
            if state.status in self._TERMINAL_STATUSES and state.updated_at.timestamp() < deadline
        ]
        if expired:
            await self._repository.delete_sessions(expired)
            expired_set = set(expired)
            for session_id in expired:
                self._wake_events.pop(session_id, None)
                self._task_refs.pop(session_id, None)
                self._runtime_auth.pop(session_id, None)
                self._runtime_sessions.discard(session_id)
            sessions = [s for s in sessions if s.session_id not in expired_set]
        return sessions

    def _is_active_in_current_runtime(self, state: SessionState) -> bool:
        if state.status not in {SessionStatus.RUNNING, SessionStatus.WAITING_USER}:
            return False
        return state.session_id in self._runtime_sessions

    def _touch_locked(self, state: SessionState) -> None:
        state.updated_at = self._clock()

    def _wake_for(self, session_id: str) -> asyncio.Event:
        event = self._wake_events.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._wake_events[session_id] = event
        return event

    def _attach_runtime(self, state: SessionState) -> SessionState:
        state.wake_event = self._wake_for(state.session_id)
        state.task_ref = self._task_refs.get(state.session_id)
        auth = self._runtime_auth.get(state.session_id)
        if auth is not None:
            state.auth = auth
        return state


def _clone_state(state: SessionState, *, persist_auth: bool = True) -> SessionState:
    auth = state.auth.model_copy(deep=True) if persist_auth and state.auth is not None else None
    return SessionState(
        session_id=state.session_id,
        task=state.task,
        start_url=state.start_url,
        callback_url=state.callback_url,
        novnc_url=state.novnc_url,
        metadata=deepcopy(state.metadata),
        auth=auth,
        status=state.status,
        created_at=state.created_at,
        updated_at=state.updated_at,
        queued_at=state.queued_at,
        started_at=state.started_at,
        finished_at=state.finished_at,
        waiting_deadline_at=state.waiting_deadline_at,
        runtime_worker_id=state.runtime_worker_id,
        runtime_claim_token=state.runtime_claim_token,
        runtime_heartbeat_at=state.runtime_heartbeat_at,
        browser_slot_id=state.browser_slot_id,
        browser_cdp_endpoint=state.browser_cdp_endpoint,
        waiting_reply_id=state.waiting_reply_id,
        waiting_question=state.waiting_question,
        last_error=state.last_error,
        events=[event.model_copy(deep=True) for event in state.events],
        agent_memory=deepcopy(state.agent_memory),
        pending_reply_text=state.pending_reply_text,
    )
