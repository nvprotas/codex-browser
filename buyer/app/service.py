from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from ._utils import head_text as _head_text, tail_text as _tail_text
from .auth_scripts import (
    AUTH_FAILED_INVALID_SESSION,
    AUTH_FAILED_PAYLOAD,
    AUTH_FAILED_REDIRECT_LOOP,
    AUTH_OK,
    AUTH_REFRESH_REQUESTED,
    SberIdScriptRunner,
    domain_from_url,
    is_domain_in_allowlist,
)
from .callback import CallbackClient, CallbackDeliveryError
from .knowledge_analyzer import PostSessionAnalysisSnapshot, PostSessionKnowledgeAnalyzer
from .models import AgentOutput, EventEnvelope, SessionStatus, TaskAuthPayload
from .purchase_scripts import PurchaseScriptRunner
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
        sberid_allowlist: set[str],
        sberid_auth_retry_budget: int,
        auth_script_runner: SberIdScriptRunner,
        purchase_script_allowlist: set[str] | None = None,
        purchase_script_runner: PurchaseScriptRunner | None = None,
        knowledge_analyzer: PostSessionKnowledgeAnalyzer | None = None,
    ) -> None:
        self._store = store
        self._callback_client = callback_client
        self._runner = runner
        self._novnc_url = novnc_url
        self._default_callback_url = default_callback_url
        self._cdp_recovery_window_sec = max(cdp_recovery_window_sec, 0.0)
        self._cdp_recovery_interval_sec = max(cdp_recovery_interval_ms, 1) / 1000.0
        self._sberid_allowlist = {item for item in sberid_allowlist if item}
        self._sberid_auth_retry_budget = max(sberid_auth_retry_budget, 0)
        self._auth_script_runner = auth_script_runner
        self._purchase_script_allowlist = {item for item in (purchase_script_allowlist or set()) if item}
        self._purchase_script_runner = purchase_script_runner
        self._knowledge_analyzer = knowledge_analyzer
        self._post_session_analysis_tasks: set[asyncio.Task[None]] = set()
        self._post_session_analysis_semaphore = asyncio.Semaphore(1)

    async def create_session(
        self,
        task: str,
        start_url: str,
        callback_url: str | None,
        metadata: dict[str, Any],
        auth: TaskAuthPayload | None,
    ) -> SessionState:
        state = await self._store.create_session(
            task=task,
            start_url=start_url,
            callback_url=callback_url or self._default_callback_url,
            novnc_url=self._novnc_url,
            metadata=metadata,
            auth=auth,
        )
        state = await self._store.set_status(state.session_id, SessionStatus.RUNNING)

        task_ref = asyncio.create_task(self._run_session(state.session_id), name=f'buyer-session-{state.session_id}')
        state.task_ref = task_ref
        self._store.set_task_ref(state.session_id, task_ref)
        return state

    async def get_session(self, session_id: str) -> SessionState:
        return await self._store.get(session_id)

    async def list_sessions(self) -> list[SessionState]:
        return await self._store.list_sessions()

    async def submit_reply(self, session_id: str, reply_id: str, message: str) -> SessionState:
        return await self._store.apply_reply(session_id, reply_id, message)

    async def wait_for_post_session_analysis(self) -> None:
        if not self._post_session_analysis_tasks:
            return
        await asyncio.gather(*list(self._post_session_analysis_tasks), return_exceptions=True)

    async def shutdown_post_session_analysis(self) -> None:
        tasks = list(self._post_session_analysis_tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_session(self, session_id: str) -> None:
        latest_user_reply: str | None = None
        auth_summary: dict[str, Any] = {}
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
            auth_summary = await self._run_sberid_auth_flow(state)
            if auth_summary:
                await self._store.set_auth_context(session_id, auth_summary)
                await self._store.add_agent_memory(
                    session_id,
                    'system',
                    f'[SBERID_AUTH_SUMMARY] {json.dumps(auth_summary, ensure_ascii=False)}',
                )

            purchase_result = await self._run_purchase_script_flow(state)
            if purchase_result is not None:
                await self._handle_completed(state, purchase_result, auth_summary=auth_summary)
                return

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
                    auth=state.auth,
                    auth_context=auth_summary,
                    memory=memory,
                    latest_user_reply=latest_user_reply,
                    stream_callback=lambda payload, current_state=state: self._emit_stream_event_best_effort(
                        current_state,
                        payload,
                    ),
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
                    latest_user_reply = await self._ask_user_for_reply(
                        state,
                        result.message,
                        reason_code=None,
                        extra_context={'step': step_index},
                    )
                    continue

                if result.status == 'completed':
                    recovery_started_at = None
                    recovery_attempts = 0
                    last_recovery_error_tail = 'none'
                    await self._handle_completed(state, result, auth_summary=auth_summary)
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
                    await self._handle_failed(state, failure_reason, failure_artifacts, auth_summary=auth_summary)
                    return

                await self._handle_failed(state, result.message, result.artifacts, auth_summary=auth_summary)
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
                await self._handle_failed(state, f'Непредвиденная ошибка: {exc}', artifacts={}, auth_summary=auth_summary)
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

    async def _handle_completed(
        self,
        state: SessionState,
        result: AgentOutput,
        *,
        auth_summary: dict[str, Any] | None,
    ) -> None:
        artifacts = dict(result.artifacts)
        if auth_summary:
            artifacts['auth'] = auth_summary
        if result.order_id:
            await self._emit_event(
                state,
                event_type='payment_ready',
                payload={
                    'order_id': result.order_id,
                    'message': 'Получен orderId, шаг оплаты готов.',
                },
                idempotency_suffix='payment-ready',
            )
            logger.info('payment_ready session_id=%s order_id=%s', state.session_id, result.order_id)

        await self._emit_event(
            state,
            event_type='scenario_finished',
            payload={
                'status': 'completed',
                'message': result.message,
                'order_id': result.order_id,
                'artifacts': artifacts,
            },
            idempotency_suffix='scenario-finished',
        )
        await self._store.record_artifacts(state.session_id, [artifacts])
        await self._store.set_status(state.session_id, SessionStatus.COMPLETED)
        logger.info('session_completed session_id=%s', state.session_id)
        await self._schedule_post_session_analysis(
            state,
            outcome='completed',
            message=result.message,
            order_id=result.order_id,
            artifacts=artifacts,
        )

    async def _handle_failed(
        self,
        state: SessionState,
        reason: str,
        artifacts: dict[str, Any],
        *,
        auth_summary: dict[str, Any] | None,
    ) -> None:
        merged_artifacts = dict(artifacts)
        if auth_summary:
            merged_artifacts['auth'] = auth_summary
        await self._emit_event(
            state,
            event_type='scenario_finished',
            payload={
                'status': 'failed',
                'message': reason,
                'artifacts': merged_artifacts,
            },
            idempotency_suffix='scenario-failed',
        )
        await self._store.record_artifacts(state.session_id, [merged_artifacts])
        await self._store.set_status(state.session_id, SessionStatus.FAILED, error=reason)
        logger.error('session_failed session_id=%s reason=%s', state.session_id, _tail_text(reason, limit=700))
        await self._schedule_post_session_analysis(
            state,
            outcome='failed',
            message=reason,
            order_id=None,
            artifacts=merged_artifacts,
        )

    async def _schedule_post_session_analysis(
        self,
        state: SessionState,
        *,
        outcome: str,
        message: str,
        order_id: str | None,
        artifacts: dict[str, Any],
    ) -> None:
        if self._knowledge_analyzer is None:
            return
        try:
            refreshed = await self._store.get(state.session_id)
        except SessionNotFoundError:
            return
        snapshot = PostSessionAnalysisSnapshot(
            session_id=refreshed.session_id,
            task=refreshed.task,
            start_url=refreshed.start_url,
            metadata=dict(refreshed.metadata),
            outcome=outcome,
            message=message,
            order_id=order_id,
            artifacts=dict(artifacts),
            events=[event.model_dump(mode='json') for event in refreshed.events],
        )
        task = asyncio.create_task(
            self._run_post_session_analysis(snapshot),
            name=f'knowledge-analysis-{refreshed.session_id}',
        )
        self._post_session_analysis_tasks.add(task)
        task.add_done_callback(self._post_session_analysis_tasks.discard)

    async def _run_post_session_analysis(self, snapshot: PostSessionAnalysisSnapshot) -> None:
        if self._knowledge_analyzer is None:
            return
        try:
            async with self._post_session_analysis_semaphore:
                await self._knowledge_analyzer.analyze(snapshot)
        except Exception as exc:  # noqa: BLE001 - анализ знаний не меняет итог покупки
            logger.exception(
                'knowledge_analysis_failed session_id=%s error=%s',
                snapshot.session_id,
                _tail_text(str(exc), limit=700),
            )

    async def _run_sberid_auth_flow(self, state: SessionState) -> dict[str, Any]:
        domain = domain_from_url(state.start_url)
        summary: dict[str, Any] = {
            'provider': None,
            'domain': domain,
            'mode': 'guest',
            'path': 'guest',
            'reason_code': None,
            'attempts': 0,
            'context_prepared': False,
            'allowlist': sorted(self._sberid_allowlist),
            'script_registry': self._auth_script_runner.registry_snapshot(),
        }
        auth = state.auth
        if auth is None:
            summary['reason_code'] = 'auth_not_provided'
            return summary

        provider = (auth.provider or '').strip().lower()
        if not provider:
            provider = 'sberid'
        summary['provider'] = provider
        if provider != 'sberid':
            summary['reason_code'] = 'auth_provider_not_supported'
            return summary

        if not is_domain_in_allowlist(domain, self._sberid_allowlist):
            summary['reason_code'] = 'auth_domain_not_in_allowlist'
            return summary

        summary['mode'] = 'sberid'
        current_auth = auth
        max_attempts = self._sberid_auth_retry_budget + 1
        last_script_artifacts: dict[str, Any] = {}

        for attempt in range(1, max_attempts + 1):
            summary['attempts'] = attempt
            storage_state = current_auth.storage_state
            if not _is_valid_storage_state(storage_state):
                summary['path'] = 'script'
                summary['reason_code'] = AUTH_FAILED_PAYLOAD
                if attempt < max_attempts:
                    user_reply = await self._ask_user_for_reply(
                        state,
                        'Нужен корректный auth-пакет SberId (JSON с storageState). Отправьте новый пакет.',
                        reason_code=AUTH_FAILED_PAYLOAD,
                        extra_context={
                            'attempt': attempt,
                            'max_attempts': max_attempts,
                        },
                    )
                    parsed_auth = self._parse_auth_from_user_reply(user_reply)
                    if parsed_auth is not None:
                        current_auth = parsed_auth
                        await self._store.set_auth(state.session_id, current_auth)
                    else:
                        current_auth = TaskAuthPayload(provider='sberid', storage_state=None)
                    continue

                await self._emit_handoff_and_wait(
                    state,
                    reason_code=AUTH_FAILED_PAYLOAD,
                    message='Автоматический auth-пакет невалиден после повторной попытки.',
                )
                summary['path'] = 'handoff'
                summary['handoff'] = True
                return summary

            script_result = await self._auth_script_runner.run(
                session_id=state.session_id,
                domain=domain,
                start_url=state.start_url,
                storage_state=storage_state,
                attempt=attempt,
            )
            summary['script_status'] = script_result.status
            summary['reason_code'] = script_result.reason_code
            summary['script_message'] = script_result.message
            last_script_artifacts = script_result.artifacts
            if script_result.reason_code == AUTH_OK and script_result.status == 'completed':
                summary['path'] = 'script'
                summary['artifacts'] = last_script_artifacts
                summary['context_prepared'] = bool(last_script_artifacts.get('context_prepared_for_reuse'))
                return summary

            if (
                attempt < max_attempts
                and script_result.reason_code
                in {
                    AUTH_REFRESH_REQUESTED,
                    AUTH_FAILED_REDIRECT_LOOP,
                    AUTH_FAILED_INVALID_SESSION,
                }
            ):
                user_reply = await self._ask_user_for_reply(
                    state,
                    (
                        'SberId-авторизация не подтвердилась. '
                        'Отправьте новый auth-пакет (JSON с storageState) для повтора.'
                    ),
                    reason_code=AUTH_REFRESH_REQUESTED,
                    extra_context=_build_auth_retry_context(
                        attempt=attempt,
                        max_attempts=max_attempts,
                        script_result=script_result,
                    ),
                )
                parsed_auth = self._parse_auth_from_user_reply(user_reply)
                if parsed_auth is not None:
                    current_auth = parsed_auth
                    await self._store.set_auth(state.session_id, current_auth)
                else:
                    current_auth = TaskAuthPayload(provider='sberid', storage_state=None)
                continue

            break

        summary['path'] = 'heuristic'
        summary['artifacts'] = last_script_artifacts
        await self._store.add_agent_memory(
            state.session_id,
            'system',
            (
                '[SBERID_AUTH_HEURISTIC_REQUIRED] '
                'Скриптовая SberId-авторизация не завершилась успешно. '
                'Попробуй эвристический вход; если блокер сохраняется, запроси handoff.'
            ),
        )
        return summary

    async def _run_purchase_script_flow(self, state: SessionState) -> AgentOutput | None:
        if self._purchase_script_runner is None:
            return None

        domain = domain_from_url(state.start_url)
        if not is_domain_in_allowlist(domain, self._purchase_script_allowlist):
            return None

        try:
            script_result = await self._purchase_script_runner.run(
                session_id=state.session_id,
                domain=domain,
                start_url=state.start_url,
                task=state.task,
            )
        except Exception as exc:  # noqa: BLE001 - быстрый путь не должен ломать generic fallback
            await self._store.add_agent_memory(
                state.session_id,
                'system',
                (
                    '[PURCHASE_SCRIPT_FALLBACK] '
                    f'Быстрый purchase-скрипт для {domain} аварийно завершился: {_tail_text(str(exc), limit=500)}. '
                    'Продолжай через generic browser-flow.'
                ),
            )
            logger.warning(
                'purchase_script_exception_fallback session_id=%s domain=%s error=%s',
                state.session_id,
                domain,
                _tail_text(str(exc), limit=700),
            )
            return None

        if script_result.status == 'completed' and script_result.order_id:
            logger.info(
                'purchase_script_completed session_id=%s domain=%s order_id=%s',
                state.session_id,
                domain,
                script_result.order_id,
            )
            return AgentOutput(
                status='completed',
                message=script_result.message,
                order_id=script_result.order_id,
                artifacts={'purchase_script': script_result.artifacts},
            )

        await self._store.add_agent_memory(
            state.session_id,
            'system',
            (
                '[PURCHASE_SCRIPT_FALLBACK] '
                f'Быстрый purchase-скрипт для {domain} не завершился успешно: '
                f'{script_result.reason_code}; {script_result.message}. '
                'Продолжай через generic browser-flow.'
            ),
        )
        logger.info(
            'purchase_script_fallback session_id=%s domain=%s reason=%s message=%s',
            state.session_id,
            domain,
            script_result.reason_code,
            _tail_text(script_result.message, limit=500),
        )
        return None

    async def _ask_user_for_reply(
        self,
        state: SessionState,
        message: str,
        *,
        reason_code: str | None,
        extra_context: dict[str, Any] | None,
    ) -> str:
        reply_id = str(uuid4())
        await self._store.set_waiting_question(state.session_id, message, reply_id)
        refreshed_state = await self._store.get(state.session_id)
        payload: dict[str, Any] = {
            'message': message,
            'reply_id': reply_id,
        }
        if reason_code:
            payload['reason_code'] = reason_code
        if extra_context:
            payload['context'] = extra_context
        await self._emit_event(
            refreshed_state,
            event_type='ask_user',
            payload=payload,
            idempotency_suffix=reply_id,
        )
        logger.info('session_waiting_user session_id=%s reply_id=%s reason=%s', state.session_id, reply_id, reason_code or 'none')

        await refreshed_state.wake_event.wait()
        reply_text = await self._store.pop_reply(state.session_id)
        await self._store.add_agent_memory(state.session_id, 'user', reply_text)
        return reply_text

    async def _emit_handoff_and_wait(
        self,
        state: SessionState,
        *,
        reason_code: str,
        message: str,
    ) -> None:
        await self._emit_event(
            state,
            event_type='handoff_requested',
            payload={
                'message': message,
                'reason_code': reason_code,
                'novnc_url': state.novnc_url,
            },
            idempotency_suffix=f'handoff-requested-{reason_code}',
        )
        operator_reply = await self._ask_user_for_reply(
            state,
            (
                'Переключитесь в noVNC, выполните ручной шаг авторизации и '
                'подтвердите ответом в этом диалоге.'
            ),
            reason_code=reason_code,
            extra_context={'handoff': True, 'novnc_url': state.novnc_url},
        )
        refreshed_state = await self._store.get(state.session_id)
        await self._emit_event(
            refreshed_state,
            event_type='handoff_resumed',
            payload={
                'message': 'Ручной этап handoff завершен, buyer продолжает сценарий.',
                'reason_code': reason_code,
                'operator_reply': operator_reply,
            },
            idempotency_suffix=f'handoff-resumed-{reason_code}',
        )

    @staticmethod
    def _parse_auth_from_user_reply(raw: str) -> TaskAuthPayload | None:
        text = (raw or '').strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        candidate = payload.get('auth') if isinstance(payload.get('auth'), dict) else payload
        if not isinstance(candidate, dict):
            return None
        if 'storageState' not in candidate and 'storage_state' not in candidate:
            if isinstance(candidate.get('cookies'), list) and isinstance(candidate.get('origins'), list):
                candidate = {'provider': 'sberid', 'storageState': candidate}
        try:
            auth = TaskAuthPayload.model_validate(candidate)
        except Exception:
            return None
        return auth

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
        try:
            await self._callback_client.deliver(state.callback_url, envelope)
        except CallbackDeliveryError as exc:
            await self._store.mark_event_delivery(envelope.event_id, 'failed', str(exc))
            raise
        await self._store.mark_event_delivery(envelope.event_id, 'delivered')
        return envelope

    async def _emit_stream_event_best_effort(self, state: SessionState, payload: dict[str, Any]) -> None:
        safe_payload = dict(payload)
        envelope = self._callback_client.build_envelope(
            session_id=state.session_id,
            event_type='agent_stream_event',
            payload=safe_payload,
            idempotency_suffix=(
                f"stream-{safe_payload.get('step', 'unknown')}-"
                f"{safe_payload.get('source', 'unknown')}-"
                f"{safe_payload.get('stream', 'unknown')}-"
                f"{safe_payload.get('sequence', uuid4())}"
            ),
        )
        try:
            await self._store.append_event(state.session_id, envelope)
            await self._callback_client.deliver(state.callback_url, envelope)
        except Exception as exc:  # noqa: BLE001 - stream не должен валить покупку
            try:
                await self._store.mark_event_delivery(envelope.event_id, 'failed', str(exc))
            except Exception:  # noqa: BLE001 - best-effort диагностика
                pass
            logger.warning(
                'agent_stream_delivery_failed session_id=%s step=%s source=%s stream=%s error=%s',
                state.session_id,
                safe_payload.get('step'),
                safe_payload.get('source'),
                safe_payload.get('stream'),
                _tail_text(str(exc), limit=500),
            )
            return
        await self._store.mark_event_delivery(envelope.event_id, 'delivered')


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
        chunks.extend(_collect_artifact_string_samples(artifacts))
    haystack = ' '.join(chunks).lower()
    return any(marker in haystack for marker in TRANSIENT_CDP_MARKERS)


def _collect_artifact_string_samples(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 3:
        return []
    if isinstance(value, str):
        if len(value) <= 2000:
            return [value]
        return [_head_text(value, limit=1000), _tail_text(value, limit=1000)]
    if isinstance(value, dict):
        chunks: list[str] = []
        for item in list(value.values())[:20]:
            chunks.extend(_collect_artifact_string_samples(item, depth=depth + 1))
        return chunks
    if isinstance(value, list):
        chunks = []
        for item in value[:20]:
            chunks.extend(_collect_artifact_string_samples(item, depth=depth + 1))
        return chunks
    return []


def _is_valid_storage_state(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    cookies = payload.get('cookies')
    origins = payload.get('origins')
    return isinstance(cookies, list) and isinstance(origins, list)


def _build_auth_retry_context(*, attempt: int, max_attempts: int, script_result: Any) -> dict[str, Any]:
    artifacts = script_result.artifacts if isinstance(getattr(script_result, 'artifacts', None), dict) else {}
    return {
        'attempt': attempt,
        'max_attempts': max_attempts,
        'script_reason_code': getattr(script_result, 'reason_code', None),
        'script_message': _tail_text(getattr(script_result, 'message', ''), limit=220),
        'stderr_tail': _tail_text(str(artifacts.get('stderr_tail', '')), limit=220),
    }


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
        'trace_date',
        'trace_time',
        'prompt_path',
        'prompt_sha256',
        'trace_file',
        'browser_actions_log_path',
        'browser_actions_total',
        'duration_ms',
        'command_duration_ms',
        'inter_command_idle_ms',
        'browser_busy_union_ms',
        'post_browser_idle_ms',
        'command_errors',
        'codex_tokens_used',
        'codex_model',
        'model_strategy',
        'model_fallback_reason',
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

    top_idle_gaps = raw_trace.get('top_idle_gaps')
    if isinstance(top_idle_gaps, list):
        trace['top_idle_gaps'] = top_idle_gaps[:5]

    codex_attempts = raw_trace.get('codex_attempts')
    if isinstance(codex_attempts, list):
        trace['codex_attempts'] = codex_attempts[-3:]

    return trace
