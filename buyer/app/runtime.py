from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import redis.asyncio as redis


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_domain_from_url(url: str) -> str | None:
    parsed = urlparse(url if '://' in url else f'https://{url}')
    hostname = (parsed.hostname or '').lower().strip('.')
    if not hostname:
        return None
    if hostname.startswith('www.'):
        hostname = hostname[4:]
    return hostname


def parse_domain_limits(raw: str) -> dict[str, int]:
    limits: dict[str, int] = {}
    for item in (raw or '').split(','):
        chunk = item.strip()
        if not chunk:
            continue
        if '=' not in chunk:
            raise ValueError(f'Некорректный лимит домена: {chunk}')
        domain, limit = chunk.split('=', 1)
        normalized = normalize_domain_from_url(domain.strip())
        if not normalized:
            raise ValueError(f'Некорректный домен в лимите: {chunk}')
        try:
            parsed_limit = int(limit.strip())
        except ValueError as exc:
            raise ValueError(f'Некорректное значение лимита домена: {chunk}') from exc
        if parsed_limit < 1:
            raise ValueError(f'Лимит домена должен быть больше 0: {chunk}')
        limits[normalized] = parsed_limit
    return limits


class RuntimeLockConflictError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class RuntimeLease:
    kind: str
    session_id: str
    token: str
    worker_id: str
    domain: str | None = None
    reason_code: str | None = None


class RuntimeCoordinator(Protocol):
    async def initialize(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def acquire_session_runner(self, *, session_id: str, start_url: str) -> RuntimeLease:
        pass

    async def release_session_runner(self, lease: RuntimeLease) -> None:
        pass

    async def mark_browser_context_active(self, session_id: str, *, lease: RuntimeLease) -> None:
        pass

    async def clear_browser_context_active(self, session_id: str) -> None:
        pass

    async def acquire_handoff(self, session_id: str, *, reason_code: str) -> RuntimeLease:
        pass

    async def release_handoff(self, lease: RuntimeLease) -> None:
        pass

    async def record_callback_attempt(
        self,
        *,
        session_id: str,
        event_id: str,
        event_type: str,
        attempt: int,
        attempts_total: int,
    ) -> None:
        pass

    async def clear_callback_attempt(self, event_id: str) -> None:
        pass

    async def get_marker(self, kind: str, key: str) -> dict[str, Any] | None:
        pass


class InMemoryRuntimeCoordinator:
    def __init__(
        self,
        *,
        worker_id: str = 'local',
        max_active_jobs_per_worker: int = 4,
        max_handoff_sessions: int = 1,
        domain_active_limit_default: int | None = None,
        domain_active_limits: dict[str, int] | None = None,
        lock_ttl_sec: int = 3600,
        marker_ttl_sec: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._worker_id = worker_id
        self._max_active_jobs_per_worker = max_active_jobs_per_worker
        self._max_handoff_sessions = max_handoff_sessions
        self._domain_active_limit_default = domain_active_limit_default
        self._domain_active_limits = domain_active_limits or {}
        self._lock_ttl_sec = lock_ttl_sec
        self._marker_ttl_sec = marker_ttl_sec
        self._clock = clock
        self._lock = asyncio.Lock()
        self._runner_leases: dict[str, tuple[RuntimeLease, float]] = {}
        self._handoff_leases: dict[str, tuple[RuntimeLease, float]] = {}
        self._markers: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def acquire_session_runner(self, *, session_id: str, start_url: str) -> RuntimeLease:
        domain = normalize_domain_from_url(start_url)
        async with self._lock:
            self._purge_expired_locked()
            if session_id in self._runner_leases:
                raise RuntimeLockConflictError(
                    'session_runner_locked',
                    f'Сессия {session_id} уже выполняется другим runner.',
                )
            if len(self._runner_leases) >= self._max_active_jobs_per_worker:
                raise RuntimeLockConflictError(
                    'worker_active_limit',
                    'Достигнут лимит активных задач текущего worker.',
                )
            domain_limit = self._domain_limit(domain)
            if domain and domain_limit is not None:
                active_for_domain = sum(
                    1
                    for lease, _ in self._runner_leases.values()
                    if lease.domain == domain
                )
                if active_for_domain >= domain_limit:
                    raise RuntimeLockConflictError(
                        'domain_active_limit',
                        f'Достигнут лимит активных задач для домена {domain}.',
                    )

            lease = RuntimeLease(
                kind='session_runner',
                session_id=session_id,
                token=str(uuid4()),
                worker_id=self._worker_id,
                domain=domain,
            )
            self._runner_leases[session_id] = (lease, self._expires_at(self._lock_ttl_sec))
            return lease

    async def release_session_runner(self, lease: RuntimeLease) -> None:
        async with self._lock:
            current = self._runner_leases.get(lease.session_id)
            if current and current[0].token == lease.token:
                self._runner_leases.pop(lease.session_id, None)

    async def mark_browser_context_active(self, session_id: str, *, lease: RuntimeLease) -> None:
        await self._set_marker(
            'browser_context',
            session_id,
            {
                'session_id': session_id,
                'worker_id': lease.worker_id,
                'lease_token': lease.token,
                'domain': lease.domain,
                'updated_at': utcnow_iso(),
            },
        )

    async def clear_browser_context_active(self, session_id: str) -> None:
        await self._clear_marker('browser_context', session_id)

    async def acquire_handoff(self, session_id: str, *, reason_code: str) -> RuntimeLease:
        async with self._lock:
            self._purge_expired_locked()
            if session_id in self._handoff_leases:
                raise RuntimeLockConflictError(
                    'handoff_session_locked',
                    f'Сессия {session_id} уже находится в handoff.',
                )
            if len(self._handoff_leases) >= self._max_handoff_sessions:
                raise RuntimeLockConflictError(
                    'handoff_active_limit',
                    'Достигнут лимит активных handoff-сессий.',
                )
            lease = RuntimeLease(
                kind='handoff',
                session_id=session_id,
                token=str(uuid4()),
                worker_id=self._worker_id,
                reason_code=reason_code,
            )
            self._handoff_leases[session_id] = (lease, self._expires_at(self._marker_ttl_sec))
            self._markers[('handoff', session_id)] = (
                {
                    'session_id': session_id,
                    'worker_id': self._worker_id,
                    'lease_token': lease.token,
                    'reason_code': reason_code,
                    'updated_at': utcnow_iso(),
                },
                self._expires_at(self._marker_ttl_sec),
            )
            return lease

    async def release_handoff(self, lease: RuntimeLease) -> None:
        async with self._lock:
            current = self._handoff_leases.get(lease.session_id)
            if current and current[0].token == lease.token:
                self._handoff_leases.pop(lease.session_id, None)
                self._markers.pop(('handoff', lease.session_id), None)

    async def record_callback_attempt(
        self,
        *,
        session_id: str,
        event_id: str,
        event_type: str,
        attempt: int,
        attempts_total: int,
    ) -> None:
        await self._set_marker(
            'callback_attempt',
            event_id,
            {
                'session_id': session_id,
                'event_id': event_id,
                'event_type': event_type,
                'attempt': attempt,
                'attempts_total': attempts_total,
                'worker_id': self._worker_id,
                'updated_at': utcnow_iso(),
            },
        )

    async def clear_callback_attempt(self, event_id: str) -> None:
        await self._clear_marker('callback_attempt', event_id)

    async def get_marker(self, kind: str, key: str) -> dict[str, Any] | None:
        async with self._lock:
            self._purge_expired_locked()
            item = self._markers.get((kind, key))
            if item is None:
                return None
            return dict(item[0])

    async def _set_marker(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            self._purge_expired_locked()
            self._markers[(kind, key)] = (dict(payload), self._expires_at(self._marker_ttl_sec))

    async def _clear_marker(self, kind: str, key: str) -> None:
        async with self._lock:
            self._markers.pop((kind, key), None)

    def _domain_limit(self, domain: str | None) -> int | None:
        if domain is None:
            return None
        return self._domain_active_limits.get(domain, self._domain_active_limit_default)

    def _expires_at(self, ttl_sec: int) -> float:
        return self._clock() + max(ttl_sec, 1)

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        self._runner_leases = {
            session_id: item
            for session_id, item in self._runner_leases.items()
            if item[1] > now
        }
        self._handoff_leases = {
            session_id: item
            for session_id, item in self._handoff_leases.items()
            if item[1] > now
        }
        self._markers = {
            key: item
            for key, item in self._markers.items()
            if item[1] > now
        }


class RedisRuntimeCoordinator:
    _ACQUIRE_RUNNER_SCRIPT = """
    redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[2])
    if KEYS[3] ~= '' then
        redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', ARGV[2])
    end
    if redis.call('EXISTS', KEYS[1]) == 1 then
        return 'session_runner_locked'
    end
    if tonumber(ARGV[5]) >= 0 and redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[5]) then
        return 'worker_active_limit'
    end
    if KEYS[3] ~= '' and tonumber(ARGV[6]) >= 0 and redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[6]) then
        return 'domain_active_limit'
    end
    local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[4])
    if not ok then
        return 'session_runner_locked'
    end
    redis.call('ZADD', KEYS[2], ARGV[3], ARGV[7])
    redis.call('PEXPIRE', KEYS[2], ARGV[4])
    if KEYS[3] ~= '' then
        redis.call('ZADD', KEYS[3], ARGV[3], ARGV[7])
        redis.call('PEXPIRE', KEYS[3], ARGV[4])
    end
    return 'ok'
    """

    _RELEASE_RUNNER_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        redis.call('DEL', KEYS[1])
    end
    redis.call('ZREM', KEYS[2], ARGV[2])
    if KEYS[3] ~= '' then
        redis.call('ZREM', KEYS[3], ARGV[2])
    end
    return 'ok'
    """

    _ACQUIRE_HANDOFF_SCRIPT = """
    redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[2])
    if redis.call('EXISTS', KEYS[1]) == 1 then
        return 'handoff_session_locked'
    end
    if tonumber(ARGV[5]) >= 0 and redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[5]) then
        return 'handoff_active_limit'
    end
    local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[4])
    if not ok then
        return 'handoff_session_locked'
    end
    redis.call('ZADD', KEYS[2], ARGV[3], ARGV[6])
    redis.call('PEXPIRE', KEYS[2], ARGV[4])
    redis.call('SET', KEYS[3], ARGV[7], 'PX', ARGV[4])
    return 'ok'
    """

    _RELEASE_HANDOFF_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        redis.call('DEL', KEYS[1])
    end
    redis.call('ZREM', KEYS[2], ARGV[2])
    redis.call('DEL', KEYS[3])
    return 'ok'
    """

    def __init__(
        self,
        *,
        redis_url: str,
        key_prefix: str = 'buyer:runtime',
        worker_id: str = 'local',
        max_active_jobs_per_worker: int = 4,
        max_handoff_sessions: int = 1,
        domain_active_limit_default: int | None = None,
        domain_active_limits: dict[str, int] | None = None,
        lock_ttl_sec: int = 3600,
        marker_ttl_sec: int = 300,
    ) -> None:
        self._redis_url = redis_url
        self._key_prefix = key_prefix.rstrip(':')
        self._worker_id = worker_id
        self._max_active_jobs_per_worker = max_active_jobs_per_worker
        self._max_handoff_sessions = max_handoff_sessions
        self._domain_active_limit_default = domain_active_limit_default
        self._domain_active_limits = domain_active_limits or {}
        self._lock_ttl_ms = max(lock_ttl_sec, 1) * 1000
        self._marker_ttl_ms = max(marker_ttl_sec, 1) * 1000
        self._client: redis.Redis | None = None

    async def initialize(self) -> None:
        if self._client is not None:
            return
        self._client = redis.from_url(self._redis_url, decode_responses=True)
        await self._client.ping()

    async def aclose(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None

    async def acquire_session_runner(self, *, session_id: str, start_url: str) -> RuntimeLease:
        client = await self._ensure_client()
        domain = normalize_domain_from_url(start_url)
        token = str(uuid4())
        expires_at_ms = self._now_ms() + self._lock_ttl_ms
        domain_key = self._domain_active_key(domain) if domain else ''
        domain_limit = self._domain_limit(domain)
        result = await client.eval(
            self._ACQUIRE_RUNNER_SCRIPT,
            3,
            self._runner_lock_key(session_id),
            self._worker_active_key(),
            domain_key,
            token,
            self._now_ms(),
            expires_at_ms,
            self._lock_ttl_ms,
            self._max_active_jobs_per_worker,
            domain_limit if domain_limit is not None else -1,
            session_id,
        )
        if result != 'ok':
            raise RuntimeLockConflictError(str(result), _runtime_conflict_message(str(result), domain=domain, session_id=session_id))
        return RuntimeLease(
            kind='session_runner',
            session_id=session_id,
            token=token,
            worker_id=self._worker_id,
            domain=domain,
        )

    async def release_session_runner(self, lease: RuntimeLease) -> None:
        client = await self._ensure_client()
        await client.eval(
            self._RELEASE_RUNNER_SCRIPT,
            3,
            self._runner_lock_key(lease.session_id),
            self._worker_active_key(),
            self._domain_active_key(lease.domain) if lease.domain else '',
            lease.token,
            lease.session_id,
        )

    async def mark_browser_context_active(self, session_id: str, *, lease: RuntimeLease) -> None:
        await self._set_marker(
            'browser_context',
            session_id,
            {
                'session_id': session_id,
                'worker_id': lease.worker_id,
                'lease_token': lease.token,
                'domain': lease.domain,
                'updated_at': utcnow_iso(),
            },
        )

    async def clear_browser_context_active(self, session_id: str) -> None:
        await self._clear_marker('browser_context', session_id)

    async def acquire_handoff(self, session_id: str, *, reason_code: str) -> RuntimeLease:
        client = await self._ensure_client()
        token = str(uuid4())
        expires_at_ms = self._now_ms() + self._marker_ttl_ms
        payload = {
            'session_id': session_id,
            'worker_id': self._worker_id,
            'lease_token': token,
            'reason_code': reason_code,
            'updated_at': utcnow_iso(),
        }
        result = await client.eval(
            self._ACQUIRE_HANDOFF_SCRIPT,
            3,
            self._handoff_lock_key(session_id),
            self._handoff_active_key(),
            self._marker_key('handoff', session_id),
            token,
            self._now_ms(),
            expires_at_ms,
            self._marker_ttl_ms,
            self._max_handoff_sessions,
            session_id,
            json.dumps(payload, ensure_ascii=False),
        )
        if result != 'ok':
            raise RuntimeLockConflictError(str(result), _runtime_conflict_message(str(result), session_id=session_id))
        return RuntimeLease(
            kind='handoff',
            session_id=session_id,
            token=token,
            worker_id=self._worker_id,
            reason_code=reason_code,
        )

    async def release_handoff(self, lease: RuntimeLease) -> None:
        client = await self._ensure_client()
        await client.eval(
            self._RELEASE_HANDOFF_SCRIPT,
            3,
            self._handoff_lock_key(lease.session_id),
            self._handoff_active_key(),
            self._marker_key('handoff', lease.session_id),
            lease.token,
            lease.session_id,
        )

    async def record_callback_attempt(
        self,
        *,
        session_id: str,
        event_id: str,
        event_type: str,
        attempt: int,
        attempts_total: int,
    ) -> None:
        await self._set_marker(
            'callback_attempt',
            event_id,
            {
                'session_id': session_id,
                'event_id': event_id,
                'event_type': event_type,
                'attempt': attempt,
                'attempts_total': attempts_total,
                'worker_id': self._worker_id,
                'updated_at': utcnow_iso(),
            },
        )

    async def clear_callback_attempt(self, event_id: str) -> None:
        await self._clear_marker('callback_attempt', event_id)

    async def get_marker(self, kind: str, key: str) -> dict[str, Any] | None:
        client = await self._ensure_client()
        raw = await client.get(self._marker_key(kind, key))
        if raw is None:
            return None
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else None

    async def _set_marker(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        client = await self._ensure_client()
        await client.set(
            self._marker_key(kind, key),
            json.dumps(payload, ensure_ascii=False),
            px=self._marker_ttl_ms,
        )

    async def _clear_marker(self, kind: str, key: str) -> None:
        client = await self._ensure_client()
        await client.delete(self._marker_key(kind, key))

    async def _ensure_client(self) -> redis.Redis:
        if self._client is None:
            await self.initialize()
        if self._client is None:
            raise RuntimeError('Redis runtime coordinator is not initialized.')
        return self._client

    def _domain_limit(self, domain: str | None) -> int | None:
        if domain is None:
            return None
        return self._domain_active_limits.get(domain, self._domain_active_limit_default)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _runner_lock_key(self, session_id: str) -> str:
        return f'{self._key_prefix}:session:{session_id}:runner'

    def _worker_active_key(self) -> str:
        return f'{self._key_prefix}:worker:{self._worker_id}:active_sessions'

    def _domain_active_key(self, domain: str | None) -> str:
        return f'{self._key_prefix}:domain:{domain}:active_sessions'

    def _handoff_lock_key(self, session_id: str) -> str:
        return f'{self._key_prefix}:session:{session_id}:handoff'

    def _handoff_active_key(self) -> str:
        return f'{self._key_prefix}:handoff:active_sessions'

    def _marker_key(self, kind: str, key: str) -> str:
        return f'{self._key_prefix}:marker:{kind}:{key}'


def _runtime_conflict_message(reason_code: str, *, domain: str | None = None, session_id: str | None = None) -> str:
    if reason_code == 'session_runner_locked':
        return f'Сессия {session_id} уже выполняется другим runner.'
    if reason_code == 'worker_active_limit':
        return 'Достигнут лимит активных задач текущего worker.'
    if reason_code == 'domain_active_limit':
        return f'Достигнут лимит активных задач для домена {domain}.'
    if reason_code == 'handoff_active_limit':
        return 'Достигнут лимит активных handoff-сессий.'
    if reason_code == 'handoff_session_locked':
        return f'Сессия {session_id} уже находится в handoff.'
    return f'Runtime lock отклонил операцию: {reason_code}.'
