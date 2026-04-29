from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from .models import EventEnvelope, SessionStatus
from .state import SessionState, utcnow


SCHEMA_MIGRATIONS: tuple[tuple[str, str], ...] = (
    (
        '001_persistent_state',
        """
        CREATE TABLE IF NOT EXISTS buyer_sessions (
            session_id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            start_url TEXT NOT NULL,
            callback_url TEXT NOT NULL,
            novnc_url TEXT NOT NULL,
            status TEXT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE INDEX IF NOT EXISTS buyer_sessions_status_idx ON buyer_sessions (status);
        CREATE INDEX IF NOT EXISTS buyer_sessions_updated_at_idx ON buyer_sessions (updated_at);

        CREATE TABLE IF NOT EXISTS buyer_events (
            event_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES buyer_sessions(session_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            delivery_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS buyer_events_session_position_idx ON buyer_events (session_id, position);
        CREATE INDEX IF NOT EXISTS buyer_events_type_idx ON buyer_events (event_type);

        CREATE TABLE IF NOT EXISTS buyer_replies (
            reply_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES buyer_sessions(session_id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            message TEXT,
            status TEXT NOT NULL,
            reason_code TEXT,
            context JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            answered_at TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS buyer_replies_session_status_idx ON buyer_replies (session_id, status, created_at DESC);

        CREATE TABLE IF NOT EXISTS buyer_artifacts (
            artifact_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES buyer_sessions(session_id) ON DELETE CASCADE,
            artifact_type TEXT NOT NULL,
            uri TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS buyer_artifacts_session_idx ON buyer_artifacts (session_id, created_at);

        CREATE TABLE IF NOT EXISTS buyer_auth_context (
            session_id TEXT PRIMARY KEY REFERENCES buyer_sessions(session_id) ON DELETE CASCADE,
            provider TEXT,
            domain TEXT,
            mode TEXT,
            path TEXT,
            reason_code TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            context_prepared BOOLEAN NOT NULL DEFAULT false,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS buyer_agent_memory (
            memory_id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES buyer_sessions(session_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS buyer_agent_memory_session_position_idx
            ON buyer_agent_memory (session_id, position);
        """,
    ),
)


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str
    uri: str | None
    metadata: dict[str, Any]


class PostgresSessionRepository:
    def __init__(
        self,
        *,
        database_url: str,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
    ) -> None:
        self._database_url = database_url
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._database_url,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
        )
        await run_migrations(self._pool)

    async def aclose(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def create_session(self, state: SessionState) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _upsert_session(conn, state)
                await _sync_session_related(conn, state)

    async def get_session(self, session_id: str) -> SessionState | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            return await _load_session(conn, session_id)

    async def list_sessions(self) -> list[SessionState]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            return await _load_all_sessions(conn)

    async def update_session(self, state: SessionState) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _upsert_session(conn, state)
                await _sync_session_related(conn, state)

    async def delete_sessions(self, session_ids: list[str]) -> None:
        if not session_ids:
            return
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute('DELETE FROM buyer_sessions WHERE session_id = ANY($1::text[])', session_ids)

    async def update_auth_context(self, session_id: str, context: dict[str, Any]) -> None:
        pool = await self._ensure_pool()
        metadata = _sanitize_auth_context(context)
        _ensure_json_safe(metadata)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO buyer_auth_context (
                    session_id, provider, domain, mode, path, reason_code,
                    attempts, context_prepared, metadata, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
                ON CONFLICT (session_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    domain = EXCLUDED.domain,
                    mode = EXCLUDED.mode,
                    path = EXCLUDED.path,
                    reason_code = EXCLUDED.reason_code,
                    attempts = EXCLUDED.attempts,
                    context_prepared = EXCLUDED.context_prepared,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                session_id,
                _str_or_none(context.get('provider')),
                _str_or_none(context.get('domain')),
                _str_or_none(context.get('mode')),
                _str_or_none(context.get('path')),
                _str_or_none(context.get('reason_code')),
                _int_or_zero(context.get('attempts')),
                bool(context.get('context_prepared')),
                json.dumps(metadata, ensure_ascii=False),
                utcnow(),
            )

    async def replace_artifacts(self, session_id: str, artifacts: list[dict[str, Any]]) -> None:
        refs = _build_artifact_refs(session_id=session_id, artifacts=artifacts)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('DELETE FROM buyer_artifacts WHERE session_id = $1', session_id)
                if refs:
                    await conn.executemany(
                        """
                        INSERT INTO buyer_artifacts (artifact_id, session_id, artifact_type, uri, metadata)
                        VALUES ($1, $2, $3, $4, $5::jsonb)
                        """,
                        [
                            (
                                ref.artifact_id,
                                session_id,
                                ref.artifact_type,
                                ref.uri,
                                json.dumps(_sanitize_persistent_metadata(ref.metadata), ensure_ascii=False),
                            )
                            for ref in refs
                        ],
                    )

    async def mark_event_delivery(self, event_id: str, status: str, error: str | None = None) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE buyer_events
                SET delivery_status = $2,
                    delivery_error = $3
                WHERE event_id = $1
                """,
                event_id,
                status,
                error,
            )

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            await self.initialize()
        if self._pool is None:
            raise RuntimeError('Postgres connection pool is not initialized.')
        return self._pool


async def run_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buyer_schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            rows = await conn.fetch('SELECT migration_id FROM buyer_schema_migrations')
            applied = {row['migration_id'] for row in rows}
            for migration_id, sql in SCHEMA_MIGRATIONS:
                if migration_id in applied:
                    continue
                await conn.execute(sql)
                await conn.execute('INSERT INTO buyer_schema_migrations (migration_id) VALUES ($1)', migration_id)


async def _upsert_session(conn: asyncpg.Connection, state: SessionState) -> None:
    await conn.execute(
        """
        INSERT INTO buyer_sessions (
            session_id, task, start_url, callback_url, novnc_url,
            status, metadata, last_error, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
        ON CONFLICT (session_id) DO UPDATE SET
            task = EXCLUDED.task,
            start_url = EXCLUDED.start_url,
            callback_url = EXCLUDED.callback_url,
            novnc_url = EXCLUDED.novnc_url,
            status = EXCLUDED.status,
            metadata = EXCLUDED.metadata,
            last_error = EXCLUDED.last_error,
            updated_at = EXCLUDED.updated_at
        """,
        state.session_id,
        state.task,
        state.start_url,
        state.callback_url,
        state.novnc_url,
        state.status.value,
        json.dumps(state.metadata, ensure_ascii=False),
        state.last_error,
        state.created_at,
        state.updated_at,
    )


async def _sync_session_related(conn: asyncpg.Connection, state: SessionState) -> None:
    existing_delivery = {
        row['event_id']: {
            'delivery_status': row['delivery_status'],
            'delivery_error': row['delivery_error'],
        }
        for row in await conn.fetch(
            """
            SELECT event_id, delivery_status, delivery_error
            FROM buyer_events
            WHERE session_id = $1
            """,
            state.session_id,
        )
    }
    await conn.execute('DELETE FROM buyer_events WHERE session_id = $1', state.session_id)
    if state.events:
        await conn.executemany(
            """
            INSERT INTO buyer_events (
                event_id, session_id, position, event_type, occurred_at,
                idempotency_key, payload, delivery_status, delivery_error
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            """,
            [
                (
                    event.event_id,
                    state.session_id,
                    index,
                    event.event_type,
                    event.occurred_at,
                    event.idempotency_key,
                    json.dumps(_serialize_event_payload_for_storage(event.payload), ensure_ascii=False),
                    existing_delivery.get(event.event_id, {}).get('delivery_status', 'pending'),
                    existing_delivery.get(event.event_id, {}).get('delivery_error'),
                )
                for index, event in enumerate(state.events)
            ],
        )

    await conn.execute('DELETE FROM buyer_agent_memory WHERE session_id = $1', state.session_id)
    if state.agent_memory:
        await conn.executemany(
            """
            INSERT INTO buyer_agent_memory (session_id, position, role, text)
            VALUES ($1, $2, $3, $4)
            """,
            [
                (
                    state.session_id,
                    index,
                    str(item.get('role', '')),
                    _sanitize_reply_or_memory_text(str(item.get('text', ''))),
                )
                for index, item in enumerate(state.agent_memory)
            ],
        )

    if state.waiting_reply_id:
        await conn.execute(
            """
            INSERT INTO buyer_replies (reply_id, session_id, question, status)
            VALUES ($1, $2, $3, 'waiting')
            ON CONFLICT (reply_id) DO UPDATE SET
                question = EXCLUDED.question,
                status = 'waiting',
                message = NULL,
                answered_at = NULL
            """,
            state.waiting_reply_id,
            state.session_id,
            state.waiting_question or '',
        )
    elif state.pending_reply_text:
        await conn.execute(
            """
            UPDATE buyer_replies
            SET status = 'answered',
                message = $2,
                answered_at = COALESCE(answered_at, $3)
            WHERE reply_id = (
                SELECT reply_id
                FROM buyer_replies
                WHERE session_id = $1 AND status = 'waiting'
                ORDER BY created_at DESC
                LIMIT 1
            )
            """,
            state.session_id,
            _sanitize_reply_or_memory_text(state.pending_reply_text),
            state.updated_at,
        )
    else:
        await conn.execute(
            """
            UPDATE buyer_replies
            SET status = 'consumed'
            WHERE session_id = $1 AND status = 'answered'
            """,
            state.session_id,
        )


async def _load_session(conn: asyncpg.Connection, session_id: str) -> SessionState | None:
    row = await conn.fetchrow(
        """
        SELECT session_id, task, start_url, callback_url, novnc_url,
               status, metadata, last_error, created_at, updated_at
        FROM buyer_sessions
        WHERE session_id = $1
        """,
        session_id,
    )
    if row is None:
        return None

    events = [
        EventEnvelope(
            event_id=event_row['event_id'],
            session_id=event_row['session_id'],
            event_type=event_row['event_type'],
            occurred_at=event_row['occurred_at'],
            idempotency_key=event_row['idempotency_key'],
            payload=_json_dict(event_row['payload']),
        )
        for event_row in await conn.fetch(
            """
            SELECT event_id, session_id, event_type, occurred_at, idempotency_key, payload
            FROM buyer_events
            WHERE session_id = $1
            ORDER BY position ASC, occurred_at ASC
            """,
            session_id,
        )
    ]
    memory = [
        {'role': memory_row['role'], 'text': memory_row['text']}
        for memory_row in await conn.fetch(
            """
            SELECT role, text
            FROM buyer_agent_memory
            WHERE session_id = $1
            ORDER BY position ASC, memory_id ASC
            """,
            session_id,
        )
    ]
    reply = await conn.fetchrow(
        """
        SELECT reply_id, question, message, status
        FROM buyer_replies
        WHERE session_id = $1 AND status IN ('waiting', 'answered')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        session_id,
    )
    waiting_reply_id = None
    waiting_question = None
    pending_reply_text = None
    if reply is not None and reply['status'] == 'waiting':
        waiting_reply_id = reply['reply_id']
        waiting_question = reply['question']
    elif reply is not None and reply['status'] == 'answered':
        pending_reply_text = reply['message']

    return SessionState(
        session_id=row['session_id'],
        task=row['task'],
        start_url=row['start_url'],
        callback_url=row['callback_url'],
        novnc_url=row['novnc_url'],
        metadata=_json_dict(row['metadata']),
        auth=None,
        status=SessionStatus(row['status']),
        created_at=row['created_at'],
        updated_at=row['updated_at'],
        waiting_reply_id=waiting_reply_id,
        waiting_question=waiting_question,
        last_error=row['last_error'],
        events=events,
        agent_memory=memory,
        pending_reply_text=pending_reply_text,
    )


async def _load_all_sessions(conn: asyncpg.Connection) -> list[SessionState]:
    session_rows = await conn.fetch(
        """
        SELECT session_id, task, start_url, callback_url, novnc_url,
               status, metadata, last_error, created_at, updated_at
        FROM buyer_sessions
        ORDER BY created_at ASC, session_id ASC
        """
    )
    if not session_rows:
        return []
    session_ids = [row['session_id'] for row in session_rows]
    event_rows = await conn.fetch(
        """
        SELECT event_id, session_id, event_type, occurred_at, idempotency_key, payload
        FROM buyer_events
        WHERE session_id = ANY($1::text[])
        ORDER BY session_id ASC, position ASC, occurred_at ASC
        """,
        session_ids,
    )
    memory_rows = await conn.fetch(
        """
        SELECT session_id, role, text
        FROM buyer_agent_memory
        WHERE session_id = ANY($1::text[])
        ORDER BY session_id ASC, position ASC, memory_id ASC
        """,
        session_ids,
    )
    reply_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (session_id) session_id, reply_id, question, message, status
        FROM buyer_replies
        WHERE session_id = ANY($1::text[]) AND status IN ('waiting', 'answered')
        ORDER BY session_id ASC, created_at DESC
        """,
        session_ids,
    )

    events_by_sid: dict[str, list[Any]] = {}
    for row in event_rows:
        events_by_sid.setdefault(row['session_id'], []).append(row)
    memory_by_sid: dict[str, list[Any]] = {}
    for row in memory_rows:
        memory_by_sid.setdefault(row['session_id'], []).append(row)
    reply_by_sid: dict[str, Any] = {row['session_id']: row for row in reply_rows}

    states: list[SessionState] = []
    for row in session_rows:
        sid = row['session_id']
        events = [
            EventEnvelope(
                event_id=er['event_id'],
                session_id=er['session_id'],
                event_type=er['event_type'],
                occurred_at=er['occurred_at'],
                idempotency_key=er['idempotency_key'],
                payload=_json_dict(er['payload']),
            )
            for er in events_by_sid.get(sid, [])
        ]
        memory = [{'role': mr['role'], 'text': mr['text']} for mr in memory_by_sid.get(sid, [])]
        reply = reply_by_sid.get(sid)
        waiting_reply_id = None
        waiting_question = None
        pending_reply_text = None
        if reply is not None and reply['status'] == 'waiting':
            waiting_reply_id = reply['reply_id']
            waiting_question = reply['question']
        elif reply is not None and reply['status'] == 'answered':
            pending_reply_text = reply['message']
        states.append(SessionState(
            session_id=row['session_id'],
            task=row['task'],
            start_url=row['start_url'],
            callback_url=row['callback_url'],
            novnc_url=row['novnc_url'],
            metadata=_json_dict(row['metadata']),
            auth=None,
            status=SessionStatus(row['status']),
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            waiting_reply_id=waiting_reply_id,
            waiting_question=waiting_question,
            last_error=row['last_error'],
            events=events,
            agent_memory=memory,
            pending_reply_text=pending_reply_text,
        ))
    return states


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def _ensure_json_safe(value: dict[str, Any]) -> None:
    json.dumps(value, ensure_ascii=False)


AUTH_REPLY_MARKER = '[SBERID_AUTH_RECEIVED]'


def summarize_sberid_auth_reply(raw: str) -> str:
    summary = _summarize_storage_state_reply(raw)
    if summary is not None:
        return summary
    return f'{AUTH_REPLY_MARKER} status=unparseable'


def _sanitize_reply_or_memory_text(raw: str) -> str:
    summary = _summarize_storage_state_reply(raw)
    if summary is not None:
        return summary
    return raw


def _summarize_storage_state_reply(raw: str) -> str | None:
    try:
        payload = json.loads((raw or '').strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    candidate = payload.get('auth') if isinstance(payload.get('auth'), dict) else payload
    if not isinstance(candidate, dict):
        return None

    has_storage_state_key = 'storageState' in candidate or 'storage_state' in candidate
    storage_state = candidate.get('storageState') if 'storageState' in candidate else candidate.get('storage_state')
    if storage_state is None and (
        isinstance(candidate.get('cookies'), list) or isinstance(candidate.get('origins'), list)
    ):
        storage_state = candidate
    if not has_storage_state_key and storage_state is not candidate:
        return None

    provider = _safe_summary_value(candidate.get('provider') or 'sberid')
    status = 'valid_shape' if _has_playwright_storage_state_shape(storage_state) else 'invalid_shape'
    state_entries = _count_list_field(storage_state, 'cookies')
    origin_entries = _count_list_field(storage_state, 'origins')
    origin_storage_entries = _count_origin_storage_entries(storage_state)
    return (
        f'{AUTH_REPLY_MARKER} provider={provider} status={status} '
        f'state_entries={state_entries} origin_entries={origin_entries} '
        f'origin_storage_entries={origin_storage_entries}'
    )


def _safe_summary_value(value: Any) -> str:
    text = str(value or '').strip().lower()
    cleaned = ''.join(ch for ch in text if ch.isalnum() or ch in {'-', '_', '.'})
    return (cleaned or 'unknown')[:40]


def _has_playwright_storage_state_shape(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get('cookies'), list) and isinstance(value.get('origins'), list)


def _count_list_field(value: Any, key: str) -> int:
    if not isinstance(value, dict):
        return 0
    items = value.get(key)
    return len(items) if isinstance(items, list) else 0


def _count_origin_storage_entries(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    origins = value.get('origins')
    if not isinstance(origins, list):
        return 0
    total = 0
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        entries = origin.get('localStorage')
        if isinstance(entries, list):
            total += len(entries)
    return total


_AUTH_CONTEXT_BLOCKED: frozenset[str] = frozenset({
    'storagestate',
    'storagestatepath',
    'cookies',
    'cookie',
    'origins',
    'localstorage',
    'authorization',
})
_AUTH_CONTEXT_MARKERS: tuple[str, ...] = (
    'token',
    'secret',
    'password',
    'apikey',
    'authorizationcode',
)
_PERSISTENT_METADATA_BLOCKED: frozenset[str] = _AUTH_CONTEXT_BLOCKED | {
    'stdout',
    'stdouttail',
    'stderr',
    'stderrtail',
    'promptpreview',
}
_PERSISTENT_METADATA_MARKERS: tuple[str, ...] = _AUTH_CONTEXT_MARKERS + ('stdout', 'stderr')


def _normalized_sensitive_key(key: Any) -> str:
    return str(key).replace('_', '').replace('-', '').replace(' ', '').lower()


def _is_blocked_key(key: Any, *, blocked_keys: frozenset[str], blocked_markers: tuple[str, ...]) -> bool:
    compact = _normalized_sensitive_key(key)
    return compact in blocked_keys or any(marker in compact for marker in blocked_markers)


def _sanitize(
    value: Any,
    *,
    blocked_keys: frozenset[str],
    blocked_markers: tuple[str, ...],
    depth: int = 0,
) -> Any:
    if depth > 8:
        return '[truncated]'
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _is_blocked_key(key, blocked_keys=blocked_keys, blocked_markers=blocked_markers):
                continue
            sanitized[str(key)] = _sanitize(
                item,
                blocked_keys=blocked_keys,
                blocked_markers=blocked_markers,
                depth=depth + 1,
            )
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize(item, blocked_keys=blocked_keys, blocked_markers=blocked_markers, depth=depth + 1)
            for item in value[:100]
        ]
    return value


def _sanitize_auth_context(value: Any, *, depth: int = 0) -> Any:
    return _sanitize(
        value,
        blocked_keys=_AUTH_CONTEXT_BLOCKED,
        blocked_markers=_AUTH_CONTEXT_MARKERS,
        depth=depth,
    )


def _build_artifact_refs(*, session_id: str, artifacts: list[dict[str, Any]]) -> list[ArtifactRef]:
    refs: list[ArtifactRef] = []
    for index, artifact in enumerate(artifacts):
        metadata = _sanitize_persistent_metadata(artifact)
        refs.append(
            ArtifactRef(
                artifact_id=f'{session_id}:payload:{index}',
                artifact_type='payload',
                uri=None,
                metadata=metadata,
            )
        )
        for path_index, (path_key, path_value) in enumerate(_iter_artifact_paths(artifact)):
            refs.append(
                ArtifactRef(
                    artifact_id=f'{session_id}:path:{index}:{path_index}',
                    artifact_type=path_key,
                    uri=path_value,
                    metadata={'source_payload_index': index, 'path_key': path_key},
                )
            )
    return refs


def _iter_artifact_paths(value: Any, *, depth: int = 0, key: str = 'artifact') -> list[tuple[str, str]]:
    if depth > 4:
        return []
    if isinstance(value, dict):
        refs: list[tuple[str, str]] = []
        for item_key, item_value in value.items():
            normalized = str(item_key).lower()
            compact = normalized.replace('_', '').replace('-', '')
            if compact in {'storagestatepath', 'storagestate', 'cookies', 'localstorage'}:
                continue
            if isinstance(item_value, str) and (
                normalized in {'path', 'uri'}
                or normalized.endswith('_path')
                or normalized.endswith('_file')
                or normalized.endswith('_url')
            ):
                refs.append((str(item_key), item_value))
            refs.extend(_iter_artifact_paths(item_value, depth=depth + 1, key=str(item_key)))
        return refs
    if isinstance(value, list):
        refs = []
        for item in value[:50]:
            refs.extend(_iter_artifact_paths(item, depth=depth + 1, key=key))
        return refs
    return []


def _sanitize_persistent_metadata(value: Any, *, depth: int = 0) -> Any:
    return _sanitize(
        value,
        blocked_keys=_PERSISTENT_METADATA_BLOCKED,
        blocked_markers=_PERSISTENT_METADATA_MARKERS,
        depth=depth,
    )


def _serialize_event_payload_for_storage(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_persistent_metadata(payload)
    return sanitized if isinstance(sanitized, dict) else {}


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
