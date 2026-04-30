from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ._utils import head_text as _head_text, tail_text as _tail_text
from .auth_scripts import (
    AUTH_OK,
    SberIdScriptRunner,
    domain_from_url,
    is_domain_in_allowlist,
)
from .callback import CallbackClient, CallbackDeliveryError
from .external_auth import ExternalSberCookiesClient
from .knowledge_analyzer import PostSessionAnalysisSnapshot, PostSessionKnowledgeAnalyzer
from .models import AgentOutput, EventEnvelope, SessionStatus, TaskAuthPayload
from .persistence import _sanitize_persistent_metadata
from .payment_verifier import payment_evidence_from_purchase_script, verify_completed_payment
from .purchase_scripts import PurchaseScriptRunner
from .runner import AgentRunner
from .state import (
    ReplyValidationError,
    SessionConflictError,
    SessionNotFoundError,
    SessionState,
    SessionStore,
)
from .user_profile import append_profile_updates

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
        buyer_user_info_path: str = '/run/buyer/user-buyer-info.md',
        external_auth_client: ExternalSberCookiesClient | None = None,
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
        self._buyer_user_info_path = buyer_user_info_path
        self._callback_tokens: dict[str, str] = {}
        self._external_auth_client = external_auth_client

    async def create_session(
        self,
        task: str,
        start_url: str,
        callback_url: str | None,
        metadata: dict[str, Any],
        auth: TaskAuthPayload | None,
        callback_token: str | None = None,
    ) -> SessionState:
        state = await self._store.create_session(
            task=task,
            start_url=start_url,
            callback_url=callback_url or self._default_callback_url,
            novnc_url=self._novnc_url,
            metadata=metadata,
            auth=auth,
        )
        if callback_token:
            self._callback_tokens[state.session_id] = callback_token
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
            _log_buyer_progress(
                session_id=session_id,
                stage='session',
                message=f'Сессия запущена, стартовая страница: {_compact_url_for_container_log(state.start_url)}',
            )

            await self._store.add_agent_memory(session_id, 'system', f'Start URL: {state.start_url}')
            await self._store.add_agent_memory(session_id, 'user', state.task)
            auth_summary = await self._run_sberid_auth_flow(state)
            if auth_summary:
                auth_summary = _sanitize_auth_summary_for_runtime(auth_summary)
                await self._store.set_auth_context(session_id, auth_summary)
                _log_buyer_progress(
                    session_id=session_id,
                    stage='auth',
                    status=str(auth_summary.get('reason_code') or auth_summary.get('path') or 'unknown'),
                    message=_describe_auth_summary(auth_summary),
                )
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
                _log_buyer_progress(
                    session_id=session_id,
                    stage='agent_step',
                    step_index=step_index,
                    message='Запускаю generic шаг агента через codex.',
                )
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
                self._persist_profile_updates(session_id=session_id, step_index=step_index, updates=result.profile_updates)
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
                    payment_verification = verify_completed_payment(state.start_url, result)
                    if not payment_verification.accepted:
                        await self._handle_failed(
                            state,
                            payment_verification.failure_reason or 'Completed result rejected: payment verification failed.',
                            _artifacts_with_payment_evidence(result),
                            auth_summary=auth_summary,
                        )
                        return
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
                        _log_buyer_progress(
                            session_id=session_id,
                            stage='cdp_recovery',
                            step_index=step_index,
                            status='retrying',
                            message=(
                                f'Временный CDP-сбой, пробую восстановиться: попытка {recovery_attempts}, '
                                f'последняя ошибка: {last_recovery_error_tail}'
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
                **_eval_ids_from_state(state),
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
                    **_eval_ids_from_state(state),
                )
                await self._store.append_event(session_id, fallback_event)
        finally:
            self._callback_tokens.pop(session_id, None)

    async def _handle_completed(
        self,
        state: SessionState,
        result: AgentOutput,
        *,
        auth_summary: dict[str, Any] | None,
    ) -> None:
        artifacts = dict(result.artifacts)
        if result.payment_evidence is not None:
            artifacts['payment_evidence'] = result.payment_evidence.model_dump(mode='json')
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
            _log_buyer_progress(
                session_id=state.session_id,
                stage='payment',
                status='ready',
                order_id=result.order_id,
                message='Платежный шаг готов, orderId получен и отправлен в callback.',
            )

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
        _log_buyer_progress(
            session_id=state.session_id,
            stage='session',
            status='completed',
            order_id=result.order_id,
            message='Сценарий покупки завершен успешно.',
        )
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
        _log_buyer_progress(
            session_id=state.session_id,
            stage='session',
            status='failed',
            message=f'Сценарий завершился ошибкой: {_tail_text(reason, limit=300)}',
        )
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
        auth = await self._resolve_session_auth(state, summary)
        if auth is None:
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
                summary['mode'] = 'guest'
                summary['path'] = 'guest'
                summary['reason_code'] = 'auth_inline_invalid_payload'
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

    async def _resolve_session_auth(self, state: SessionState, summary: dict[str, Any]) -> TaskAuthPayload | None:
        if state.auth is not None:
            summary['source'] = 'inline'
            return state.auth

        if self._external_auth_client is None:
            summary['source'] = 'none'
            summary['reason_code'] = 'auth_not_provided'
            return None

        result = await self._external_auth_client.fetch_storage_state()
        summary['source'] = 'external_cookies_api'
        summary['external_auth'] = result.metadata
        summary['reason_code'] = result.reason_code
        if result.storage_state is None:
            return None

        auth = TaskAuthPayload(provider='sberid', storageState=result.storage_state)
        await self._store.set_auth(state.session_id, auth)
        state.auth = auth
        return auth

    async def _run_purchase_script_flow(self, state: SessionState) -> AgentOutput | None:
        if self._purchase_script_runner is None:
            return None

        domain = domain_from_url(state.start_url)
        if not is_domain_in_allowlist(domain, self._purchase_script_allowlist):
            return None

        _log_buyer_progress(
            session_id=state.session_id,
            stage='purchase_script',
            message=f'Пробую быстрый purchase-скрипт для домена {domain}.',
        )
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
            _log_buyer_progress(
                session_id=state.session_id,
                stage='purchase_script',
                status='fallback',
                message=f'Быстрый purchase-скрипт упал, перехожу к generic flow: {_tail_text(str(exc), limit=250)}',
            )
            return None

        if script_result.status == 'completed' and script_result.order_id:
            payment_evidence = payment_evidence_from_purchase_script(script_result)
            candidate = AgentOutput(
                status='completed',
                message=script_result.message,
                order_id=script_result.order_id,
                payment_evidence=payment_evidence,
                artifacts={'purchase_script': script_result.artifacts},
            )
            payment_verification = verify_completed_payment(state.start_url, candidate)
            if not payment_verification.accepted:
                await self._store.add_agent_memory(
                    state.session_id,
                    'system',
                    (
                        '[PURCHASE_SCRIPT_FALLBACK] '
                        f'Быстрый purchase-скрипт для {domain} вернул completed/order_id, '
                        f'но не прошел verifier SberPay: {payment_verification.failure_reason}. '
                        'Продолжай через generic browser-flow.'
                    ),
                )
                logger.warning(
                    'purchase_script_invalid_payment_evidence_fallback session_id=%s domain=%s order_id=%s artifacts=%s',
                    state.session_id,
                    domain,
                    script_result.order_id,
                    _tail_text(json.dumps(script_result.artifacts, ensure_ascii=False, default=str), limit=700),
                )
                _log_buyer_progress(
                    session_id=state.session_id,
                    stage='purchase_script',
                    status='fallback',
                    order_id=script_result.order_id,
                    message=(
                        'Быстрый purchase-скрипт дошел до orderId, но verifier отклонил evidence; '
                        'перехожу к generic flow.'
                    ),
                )
                return None

            logger.info(
                'purchase_script_completed session_id=%s domain=%s order_id=%s',
                state.session_id,
                domain,
                script_result.order_id,
            )
            _log_buyer_progress(
                session_id=state.session_id,
                stage='purchase_script',
                status='completed',
                order_id=script_result.order_id,
                message='Быстрый purchase-скрипт довел сценарий до платежного шага.',
            )
            return candidate

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
        _log_buyer_progress(
            session_id=state.session_id,
            stage='purchase_script',
            status='fallback',
            message=(
                f'Быстрый purchase-скрипт не завершил сценарий ({script_result.reason_code}); '
                'перехожу к generic flow.'
            ),
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
        _log_buyer_progress(
            session_id=state.session_id,
            stage='waiting_user',
            status=reason_code or 'needs_reply',
            message=f'Ожидаю ответ оператора: {_head_text(message, limit=220)}',
        )

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

    def _persist_profile_updates(self, *, session_id: str, step_index: int, updates: list[str]) -> None:
        appended = append_profile_updates(self._buyer_user_info_path, updates)
        if appended <= 0:
            return
        logger.info(
            'buyer_user_info_updated session_id=%s step=%s appended=%s path=%s',
            session_id,
            step_index,
            appended,
            self._buyer_user_info_path,
        )

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
            **_eval_ids_from_state(state),
        )
        await self._store.append_event(state.session_id, envelope)
        try:
            headers = self._callback_headers_for_session(state.session_id)
            if headers:
                await self._callback_client.deliver(state.callback_url, envelope, headers=headers)
            else:
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
            **_eval_ids_from_state(state),
        )
        try:
            await self._store.append_event(state.session_id, envelope)
            headers = self._callback_headers_for_session(state.session_id)
            if headers:
                await self._callback_client.deliver(state.callback_url, envelope, headers=headers)
            else:
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

    def _callback_headers_for_session(self, session_id: str) -> dict[str, str] | None:
        token = self._callback_tokens.get(session_id)
        if not token:
            return None
        return {'X-Eval-Callback-Token': token}


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


def _eval_ids_from_state(state: SessionState) -> dict[str, str | None]:
    return {
        'eval_run_id': _string_metadata_value(state.metadata, 'eval_run_id'),
        'eval_case_id': _string_metadata_value(state.metadata, 'eval_case_id'),
    }


def _string_metadata_value(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


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


def _sanitize_auth_summary_for_runtime(summary: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_persistent_metadata(summary)
    return sanitized if isinstance(sanitized, dict) else {}


def _artifacts_with_payment_evidence(result: AgentOutput) -> dict[str, Any]:
    artifacts = dict(result.artifacts)
    if result.payment_evidence is not None:
        artifacts['payment_evidence'] = result.payment_evidence.model_dump(mode='json')
    return artifacts


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
    _log_buyer_progress(
        session_id=session_id,
        stage='agent_step',
        step_index=step_index,
        status=result.status,
        order_id=result.order_id,
        message=f'Шаг агента завершен: {_head_text(result.message, limit=260)}',
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
            summary = _summarize_browser_action_for_container_log(item)
            if summary is None:
                continue
            logger.info(
                'browser_action session_id=%s step=%s event=%s command=%s ok=%s duration_ms=%s target=%s page=%s summary=%s',
                session_id,
                step_index,
                summary.get('event'),
                summary.get('command'),
                summary.get('ok'),
                summary.get('duration_ms'),
                summary.get('target') or 'none',
                summary.get('page') or 'unknown',
                summary.get('summary') or '',
            )


def _log_buyer_progress(
    *,
    session_id: str,
    stage: str,
    message: str,
    step_index: int | None = None,
    status: str | None = None,
    order_id: str | None = None,
) -> None:
    logger.info(
        'buyer_progress session_id=%s stage=%s step=%s status=%s order_id=%s message=%s',
        session_id,
        stage,
        step_index if step_index is not None else 'none',
        status or 'none',
        order_id or 'none',
        _head_text(message, limit=500),
    )


def _summarize_browser_action_for_container_log(record: dict[str, Any]) -> dict[str, Any] | None:
    raw_event = record.get('event')
    command = record.get('command')
    if not isinstance(raw_event, str) or not isinstance(command, str) or not command:
        return None

    event = _compact_browser_event(raw_event)
    if event is None:
        return None

    details = record.get('details') if isinstance(record.get('details'), dict) else {}
    result = record.get('result') if isinstance(record.get('result'), dict) else {}
    failed = raw_event == 'browser_command_failed' or record.get('ok') is False
    ok = None if event == 'started' else not failed
    page = _compact_url_for_container_log(_first_string(result.get('url'), details.get('url')))
    target = _browser_action_target(command=command, details=details, result=result)

    return {
        'event': event,
        'command': command,
        'ok': ok,
        'duration_ms': record.get('duration_ms') if isinstance(record.get('duration_ms'), int) else None,
        'target': target,
        'page': page,
        'summary': _browser_action_summary(
            event=event,
            command=command,
            failed=failed,
            details=details,
            result=result,
            error=record.get('error'),
        ),
    }


def _compact_browser_event(raw_event: str) -> str | None:
    if raw_event == 'browser_command_started':
        return 'started'
    if raw_event == 'browser_command_finished':
        return 'finished'
    if raw_event == 'browser_command_failed':
        return 'failed'
    return None


def _browser_action_target(*, command: str, details: dict[str, Any], result: dict[str, Any]) -> str:
    parts: list[str] = []
    selector = _first_string(details.get('selector'), result.get('selector'))
    if selector:
        parts.append(f'selector={_head_text(selector, limit=120)}')

    name = _first_string(details.get('name'), result.get('name'))
    if name:
        parts.append(f'name={_head_text(name, limit=80)}')

    key = _first_string(details.get('key'), result.get('key'))
    if key:
        parts.append(f'key={_head_text(key, limit=80)}')

    if command == 'wait':
        seconds = details.get('seconds') or result.get('seconds')
        if isinstance(seconds, int | float):
            parts.append(f'seconds={seconds:g}')

    target_url = _compact_url_for_container_log(_first_string(details.get('url'), result.get('url')))
    if target_url:
        parts.append(f'url={target_url}')

    return ' '.join(parts) or 'none'


def _browser_action_summary(
    *,
    event: str,
    command: str,
    failed: bool,
    details: dict[str, Any],
    result: dict[str, Any],
    error: Any,
) -> str:
    if event == 'started':
        return f'начата команда браузера: {command}'

    if failed:
        error_text = _tail_text(str(error), limit=220) if error is not None else 'без текста ошибки'
        return f'команда браузера завершилась ошибкой: {command}; {error_text}'

    if command == 'snapshot':
        items = result.get('items') if isinstance(result.get('items'), list) else []
        text_preview = _visible_snapshot_text_preview(items)
        if text_preview:
            return f'снимок страницы: элементов={len(items)}, текст="{text_preview}"'
        return f'снимок страницы: элементов={len(items)}'

    if command == 'attr':
        exists = result.get('exists')
        value = _first_string(result.get('value'))
        value_summary = _compact_url_for_container_log(value) or (_head_text(value, limit=120) if value else 'empty')
        return f'прочитан атрибут: exists={exists}, value={value_summary}'

    if command == 'exists':
        return f'проверено наличие элемента: exists={result.get("exists")}'

    if command == 'text':
        text = _first_string(result.get('text'))
        size = result.get('text_size')
        if text:
            return f'прочитан текст: size={size}, preview="{_head_text(text, limit=180)}"'
        return f'прочитан текст: size={size}'

    if command == 'html':
        size = result.get('html_size') or result.get('size')
        path = _first_string(details.get('path'), result.get('path'))
        return f'получен HTML: size={size}, path={path or "stdout"}'

    if command == 'links':
        links = result.get('links') if isinstance(result.get('links'), list) else []
        return f'найдены ссылки: count={len(links)}'

    return f'команда браузера выполнена: {command}'


def _visible_snapshot_text_preview(items: list[Any]) -> str:
    chunks: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get('visible') is False:
            continue
        text = item.get('text')
        if not isinstance(text, str):
            continue
        compact = _head_text(text, limit=90)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        chunks.append(compact)
        if len(' '.join(chunks)) >= 180:
            break
    return _head_text(' | '.join(chunks), limit=220)


def _compact_url_for_container_log(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _head_text(value, limit=160)
    if not parsed.scheme or not parsed.netloc:
        return _head_text(value, limit=160)
    path = parsed.path or '/'
    return _head_text(f'{parsed.scheme}://{parsed.netloc}{path}', limit=220)


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _describe_auth_summary(summary: dict[str, Any]) -> str:
    source = summary.get('source') or 'unknown'
    mode = summary.get('mode') or 'guest'
    path = summary.get('path') or 'none'
    reason = summary.get('reason_code') or 'none'
    attempts = summary.get('attempts') or 0
    return f'Контекст авторизации подготовлен: source={source}, mode={mode}, path={path}, reason={reason}, attempts={attempts}.'


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
