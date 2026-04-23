from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from .callback import CallbackClient, CallbackDeliveryError
from .models import AgentOutput, EventEnvelope, SessionStatus
from .runner import AgentRunner
from .state import (
    ReplyValidationError,
    SessionConflictError,
    SessionNotFoundError,
    SessionState,
    SessionStore,
)

logger = logging.getLogger('uvicorn.error')


class BuyerService:
    def __init__(
        self,
        *,
        store: SessionStore,
        callback_client: CallbackClient,
        runner: AgentRunner,
        novnc_url: str,
        default_callback_url: str,
        cdp_recovery_window_sec: float,
        cdp_recovery_interval_ms: int,
    ) -> None:
        self._store = store
        self._callback_client = callback_client
        self._runner = runner
        self._novnc_url = novnc_url
        self._default_callback_url = default_callback_url
        self._cdp_recovery_window_sec = max(cdp_recovery_window_sec, 0.0)
        self._cdp_recovery_interval_sec = max(cdp_recovery_interval_ms, 1) / 1000.0

    async def create_session(self, task: str, start_url: str, callback_url: str | None, metadata: dict[str, Any]) -> SessionState:
        state = await self._store.create_session(
            task=task,
            start_url=start_url,
            callback_url=callback_url or self._default_callback_url,
            novnc_url=self._novnc_url,
            metadata=metadata,
        )
        await self._store.set_status(state.session_id, SessionStatus.RUNNING)

        task_ref = asyncio.create_task(self._run_session(state.session_id), name=f'buyer-session-{state.session_id}')
        state.task_ref = task_ref
        return state

    async def get_session(self, session_id: str) -> SessionState:
        return await self._store.get(session_id)

    async def list_sessions(self) -> list[SessionState]:
        return await self._store.list_sessions()

    async def submit_reply(self, session_id: str, reply_id: str, message: str) -> SessionState:
        return await self._store.apply_reply(session_id, reply_id, message)

    async def _run_session(self, session_id: str) -> None:
        latest_user_reply: str | None = None
        step_index = 0
        recovery_started_at: float | None = None
        recovery_attempts = 0
        last_recovery_error_tail = 'none'
        try:
            state = await self._store.get(session_id)
            await self._emit_event(
                state,
                event_type='session_started',
                payload={
                    'message': 'Сессия buyer запущена.',
                    'start_url': state.start_url,
                    'novnc_url': state.novnc_url,
                },
                idempotency_suffix='start',
            )
            logger.info('session_started session_id=%s start_url=%s', session_id, state.start_url)

            await self._store.add_agent_memory(session_id, 'system', f'Start URL: {state.start_url}')
            await self._store.add_agent_memory(session_id, 'user', state.task)

            while True:
                state = await self._store.get(session_id)
                memory = await self._store.get_agent_memory(session_id)
                step_index += 1
                await self._emit_event(
                    state,
                    event_type='agent_step_started',
                    payload={
                        'step': step_index,
                        'message': f'Запущен шаг {step_index} через codex.',
                    },
                    idempotency_suffix=f'step-{step_index}-started',
                )
                logger.info('agent_step_started session_id=%s step=%s', session_id, step_index)
                result = await self._runner.run_step(
                    session_id=session_id,
                    step_index=step_index,
                    task=state.task,
                    start_url=state.start_url,
                    metadata=state.metadata,
                    memory=memory,
                    latest_user_reply=latest_user_reply,
                )
                await self._emit_event(
                    state,
                    event_type='agent_step_finished',
                    payload=_build_agent_step_payload(step_index=step_index, result=result),
                    idempotency_suffix=f'step-{step_index}-finished',
                )
                _log_step_result_to_container(session_id=session_id, step_index=step_index, result=result)

                await self._store.add_agent_memory(session_id, 'assistant', result.message)
                latest_user_reply = None

                if result.status == 'needs_user_input':
                    recovery_started_at = None
                    recovery_attempts = 0
                    last_recovery_error_tail = 'none'
                    reply_id = str(uuid4())
                    await self._store.set_waiting_question(session_id, result.message, reply_id)
                    state = await self._store.get(session_id)
                    await self._emit_event(
                        state,
                        event_type='ask_user',
                        payload={
                            'message': result.message,
                            'reply_id': reply_id,
                        },
                        idempotency_suffix=reply_id,
                    )
                    logger.info('session_waiting_user session_id=%s step=%s reply_id=%s', session_id, step_index, reply_id)

                    await state.wake_event.wait()
                    latest_user_reply = await self._store.pop_reply(session_id)
                    await self._store.add_agent_memory(session_id, 'user', latest_user_reply)
                    continue

                if result.status == 'completed':
                    recovery_started_at = None
                    recovery_attempts = 0
                    last_recovery_error_tail = 'none'
                    await self._handle_completed(state, result)
                    return

                if _looks_like_transient_cdp_failure(result.message, result.artifacts):
                    now = asyncio.get_running_loop().time()
                    if recovery_started_at is None:
                        recovery_started_at = now
                    elapsed = now - recovery_started_at
                    if elapsed <= self._cdp_recovery_window_sec:
                        recovery_attempts += 1
                        last_recovery_error_tail = _tail_text(result.message)
                        await self._store.add_agent_memory(
                            session_id,
                            'system',
                            (
                                '[CDP_RECOVERY_RESTART_FROM_START_URL] '
                                f'Попытка восстановления {recovery_attempts}. '
                                f'Обнаружен transient CDP-сбой: {last_recovery_error_tail}. '
                                f'Начни шаг заново с `goto --url {state.start_url}`.'
                            ),
                        )
                        await asyncio.sleep(self._cdp_recovery_interval_sec)
                        continue

                    failure_reason = (
                        f'Transient CDP-сбой не восстановился за {self._cdp_recovery_window_sec:g}с. '
                        f'Последняя ошибка: {last_recovery_error_tail}'
                    )
                    failure_artifacts = dict(result.artifacts)
                    failure_artifacts['recovery'] = {
                        'recovered_after_retry': False,
                        'attempts': recovery_attempts,
                        'window_sec': self._cdp_recovery_window_sec,
                        'last_error_tail': last_recovery_error_tail,
                    }
                    await self._handle_failed(state, failure_reason, failure_artifacts)
                    return

                await self._handle_failed(state, result.message, result.artifacts)
                return

        except (SessionNotFoundError, SessionConflictError, ReplyValidationError):
            return
        except CallbackDeliveryError as exc:
            state = await self._store.get(session_id)
            await self._store.set_status(session_id, SessionStatus.FAILED, error=str(exc))
            fallback_event = self._callback_client.build_envelope(
                session_id=session_id,
                event_type='scenario_finished',
                payload={
                    'status': 'failed',
                    'message': str(exc),
                },
                idempotency_suffix='scenario-failed-callback-delivery',
            )
            await self._store.append_event(session_id, fallback_event)
        except Exception as exc:  # noqa: BLE001 - последняя защита сессии
            state = await self._store.get(session_id)
            try:
                await self._handle_failed(state, f'Непредвиденная ошибка: {exc}', artifacts={})
            except CallbackDeliveryError as delivery_exc:
                await self._store.set_status(session_id, SessionStatus.FAILED, error=str(delivery_exc))
                fallback_event = self._callback_client.build_envelope(
                    session_id=session_id,
                    event_type='scenario_finished',
                    payload={
                        'status': 'failed',
                        'message': str(delivery_exc),
                    },
                    idempotency_suffix='scenario-failed-unhandled',
                )
                await self._store.append_event(session_id, fallback_event)

    async def _handle_completed(self, state: SessionState, result: AgentOutput) -> None:
        if result.order_id:
            await self._emit_event(
                state,
                event_type='payment_ready',
                payload={
                    'order_id': result.order_id,
                    'message': 'Получен orderId, шаг оплаты готов.',
                },
                idempotency_suffix=result.order_id,
            )
            logger.info('payment_ready session_id=%s order_id=%s', state.session_id, result.order_id)

        await self._emit_event(
            state,
            event_type='scenario_finished',
            payload={
                'status': 'completed',
                'message': result.message,
                'order_id': result.order_id,
                'artifacts': result.artifacts,
            },
            idempotency_suffix='scenario-finished',
        )
        await self._store.set_status(state.session_id, SessionStatus.COMPLETED)
        logger.info('session_completed session_id=%s', state.session_id)

    async def _handle_failed(self, state: SessionState, reason: str, artifacts: dict[str, Any]) -> None:
        await self._emit_event(
            state,
            event_type='scenario_finished',
            payload={
                'status': 'failed',
                'message': reason,
                'artifacts': artifacts,
            },
            idempotency_suffix='scenario-failed',
        )
        await self._store.set_status(state.session_id, SessionStatus.FAILED, error=reason)
        logger.error('session_failed session_id=%s reason=%s', state.session_id, _tail_text(reason, limit=700))

    async def _emit_event(
        self,
        state: SessionState,
        *,
        event_type: str,
        payload: dict[str, Any],
        idempotency_suffix: str | None = None,
    ) -> EventEnvelope:
        envelope = self._callback_client.build_envelope(
            session_id=state.session_id,
            event_type=event_type,
            payload=payload,
            idempotency_suffix=idempotency_suffix,
        )
        await self._store.append_event(state.session_id, envelope)
        await self._callback_client.deliver(state.callback_url, envelope)
        return envelope


TRANSIENT_CDP_MARKERS = (
    'cdp_connect_error',
    'cdp_transient_error',
    'execution context was destroyed',
    'target page, context or browser has been closed',
    'target closed',
    'page closed',
    'context closed',
    'browser has been closed',
)


def _looks_like_transient_cdp_failure(message: str, artifacts: dict[str, Any]) -> bool:
    chunks: list[str] = [message]
    for key in ('stderr', 'stdout'):
        value = artifacts.get(key)
        if isinstance(value, str):
            chunks.append(value)
    trace = artifacts.get('trace')
    if isinstance(trace, dict):
        for key in ('stderr_tail', 'stdout_tail'):
            value = trace.get(key)
            if isinstance(value, str):
                chunks.append(value)
    if chunks == [message]:
        chunks.append(json.dumps(artifacts, ensure_ascii=False))
    haystack = ' '.join(chunks).lower()
    return any(marker in haystack for marker in TRANSIENT_CDP_MARKERS)


def _tail_text(text: str, limit: int = 500) -> str:
    compact = ' '.join(text.replace('\n', ' ').split())
    if len(compact) <= limit:
        return compact
    return compact[-limit:]


def _build_agent_step_payload(*, step_index: int, result: AgentOutput) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'step': step_index,
        'status': result.status,
        'message': result.message,
        'order_id': result.order_id,
    }
    trace = _extract_trace_for_event(result.artifacts)
    if trace:
        payload['trace'] = trace
    return payload


def _log_step_result_to_container(*, session_id: str, step_index: int, result: AgentOutput) -> None:
    logger.info(
        'agent_step_finished session_id=%s step=%s status=%s order_id=%s message=%s',
        session_id,
        step_index,
        result.status,
        result.order_id,
        _head_text(result.message, limit=300),
    )
    trace = _extract_trace_for_event(result.artifacts)
    if not trace:
        return

    logger.info(
        'agent_step_trace session_id=%s step=%s trace_file=%s prompt_path=%s actions_total=%s',
        session_id,
        step_index,
        trace.get('trace_file'),
        trace.get('prompt_path'),
        trace.get('browser_actions_total'),
    )

    actions_tail = trace.get('browser_actions_tail')
    if isinstance(actions_tail, list):
        for item in actions_tail[-10:]:
            if not isinstance(item, dict):
                continue
            logger.info(
                'agent_step_action session_id=%s step=%s action=%s',
                session_id,
                step_index,
                json.dumps(item, ensure_ascii=False),
            )


def _extract_trace_for_event(artifacts: dict[str, Any]) -> dict[str, Any]:
    raw_trace = artifacts.get('trace')
    if not isinstance(raw_trace, dict):
        return {}

    fields = (
        'prompt_path',
        'prompt_sha256',
        'trace_file',
        'browser_actions_log_path',
        'browser_actions_total',
        'duration_ms',
        'codex_returncode',
    )
    trace: dict[str, Any] = {}
    for field in fields:
        value = raw_trace.get(field)
        if value is not None:
            trace[field] = value

    prompt_preview = raw_trace.get('prompt_preview')
    if isinstance(prompt_preview, str) and prompt_preview.strip():
        trace['prompt_preview'] = _head_text(prompt_preview, limit=1200)

    stdout_tail = raw_trace.get('stdout_tail')
    if isinstance(stdout_tail, str) and stdout_tail.strip():
        trace['stdout_tail'] = _tail_text(stdout_tail, limit=1000)

    stderr_tail = raw_trace.get('stderr_tail')
    if isinstance(stderr_tail, str) and stderr_tail.strip():
        trace['stderr_tail'] = _tail_text(stderr_tail, limit=1000)

    browser_actions_tail = raw_trace.get('browser_actions_tail')
    if isinstance(browser_actions_tail, list):
        trace['browser_actions_tail'] = browser_actions_tail[-10:]

    return trace


def _head_text(text: str, limit: int = 500) -> str:
    compact = ' '.join(text.replace('\n', ' ').split())
    if len(compact) <= limit:
        return compact
    return f'{compact[:limit]}...'
