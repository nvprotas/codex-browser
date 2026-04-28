from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import AliasChoices, Field

from eval_service.app.auth_profiles import AuthProfileLoader
from eval_service.app.buyer_client import BuyerClient
from eval_service.app.callback_urls import build_buyer_callback_url
from eval_service.app.case_registry import CaseRegistry
from eval_service.app.models import CaseRunState, EvalCase, EvalRunCase, EvalRunManifest, EvalRunStatus, StrictBaseModel
from eval_service.app.run_store import RunStore


DEFAULT_CASE_TIMEOUT_SECONDS = 600.0
DEFAULT_PAYMENT_READY_GRACE_SECONDS = 5.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


router = APIRouter()


class RunCreateRequest(StrictBaseModel):
    case_ids: list[str] = Field(default_factory=list, validation_alias=AliasChoices('case_ids', 'selected_case_ids'))


class RunOrchestrator:
    def __init__(
        self,
        *,
        case_registry: CaseRegistry,
        run_store: RunStore,
        buyer_client: BuyerClient,
        auth_profile_loader: AuthProfileLoader,
        run_id_generator: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        timeout_seconds: float = DEFAULT_CASE_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        payment_ready_grace_seconds: float = DEFAULT_PAYMENT_READY_GRACE_SECONDS,
    ) -> None:
        self.case_registry = case_registry
        self.run_store = run_store
        self.buyer_client = buyer_client
        self.auth_profile_loader = auth_profile_loader
        self.run_id_generator = run_id_generator or generate_eval_run_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.payment_ready_grace_seconds = payment_ready_grace_seconds

    async def create_run(
        self,
        *,
        selected_case_ids: Sequence[str] | None,
        callback_url: str,
    ) -> EvalRunManifest:
        cases = _select_cases(self.case_registry.load_cases(), selected_case_ids)
        eval_run_id = self.run_id_generator()
        self.run_store.create_run(
            eval_run_id,
            cases=[EvalRunCase(eval_case_id=case.eval_case_id, case_version=case.case_version) for case in cases],
            status=EvalRunStatus.PENDING,
        )
        self._write_run_status(eval_run_id, EvalRunStatus.RUNNING)

        await self._run_cases_until_waiting(eval_run_id=eval_run_id, cases=cases, callback_url=callback_url)

        return self._refresh_run_status(eval_run_id)

    async def resume_after_operator_reply(
        self,
        *,
        eval_run_id: str,
        eval_case_id: str,
        callback_url: str,
    ) -> EvalRunManifest:
        self._write_run_status(eval_run_id, EvalRunStatus.RUNNING)
        state = await self._wait_for_case(eval_run_id, eval_case_id)
        if state == CaseRunState.WAITING_USER:
            return self._refresh_run_status(eval_run_id)

        remaining_cases = self._remaining_manifest_cases(eval_run_id, after_eval_case_id=eval_case_id)
        await self._run_cases_until_waiting(
            eval_run_id=eval_run_id,
            cases=remaining_cases,
            callback_url=callback_url,
        )
        return self._refresh_run_status(eval_run_id)

    async def _run_cases_until_waiting(
        self,
        *,
        eval_run_id: str,
        cases: Sequence[EvalCase],
        callback_url: str,
    ) -> None:
        for case in cases:
            run_case = _find_case(self.run_store.read_manifest(eval_run_id).cases, case.eval_case_id)
            if run_case.state == CaseRunState.WAITING_USER:
                break
            if _is_terminal_case_state(run_case.state):
                continue
            if run_case.state != CaseRunState.PENDING:
                state = await self._wait_for_case(eval_run_id, case.eval_case_id)
                if state == CaseRunState.WAITING_USER:
                    break
                continue

            auth_result = self.auth_profile_loader.load(case.auth_profile)
            if auth_result.skip_reason is not None:
                self.run_store.update_case(
                    eval_run_id,
                    case.eval_case_id,
                    state=CaseRunState.SKIPPED_AUTH_MISSING,
                    finished_at=self.clock(),
                    error=json.dumps(auth_result.skip_reason.model_dump(mode='json'), ensure_ascii=False, sort_keys=True),
                )
                continue

            state = await self._run_case(
                eval_run_id=eval_run_id,
                case=case,
                callback_url=callback_url,
                storage_state=auth_result.storage_state,
            )
            if state == CaseRunState.WAITING_USER:
                break

    async def _run_case(
        self,
        *,
        eval_run_id: str,
        case: EvalCase,
        callback_url: str,
        storage_state: dict[str, Any] | None,
    ) -> CaseRunState:
        self.run_store.update_case(
            eval_run_id,
            case.eval_case_id,
            state=CaseRunState.STARTING,
            started_at=self.clock(),
            error=None,
        )
        try:
            response = await self.buyer_client.create_task(
                task=case.task,
                start_url=case.start_url,
                metadata={**case.buyer_metadata(), 'eval_run_id': eval_run_id},
                callback_url=callback_url,
                storage_state=storage_state,
            )
            session_id = _required_response_string(response, 'session_id')
        except Exception as exc:
            return self._mark_case_runtime_failure(eval_run_id, case.eval_case_id, exc)

        self._record_created_task(eval_run_id, case.eval_case_id, session_id)
        return await self._wait_for_case(eval_run_id, case.eval_case_id)

    async def _wait_for_case(self, eval_run_id: str, eval_case_id: str) -> CaseRunState:
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            case = _find_case(self.run_store.read_manifest(eval_run_id).cases, eval_case_id)
            if case.state == CaseRunState.PAYMENT_READY:
                await self.sleep(self.payment_ready_grace_seconds)
                latest_case = _find_case(self.run_store.read_manifest(eval_run_id).cases, eval_case_id)
                if latest_case.state == CaseRunState.PAYMENT_READY:
                    manifest = self.run_store.update_case(
                        eval_run_id,
                        eval_case_id,
                        state=CaseRunState.FINISHED,
                        finished_at=self.clock(),
                        waiting_reply_id=None,
                    )
                    return _find_case(manifest.cases, eval_case_id).state
                if _is_wait_or_terminal(latest_case.state):
                    return latest_case.state
                continue
            if _is_wait_or_terminal(case.state):
                return case.state
            if self.monotonic() >= deadline:
                manifest = self.run_store.update_case(
                    eval_run_id,
                    eval_case_id,
                    state=CaseRunState.TIMEOUT,
                    finished_at=self.clock(),
                    error=f'timeout after {self.timeout_seconds}s',
                )
                return _find_case(manifest.cases, eval_case_id).state
            await self.sleep(min(self.poll_interval_seconds, max(deadline - self.monotonic(), 0.0)))

    def _record_created_task(self, eval_run_id: str, eval_case_id: str, session_id: str) -> None:
        case = _find_case(self.run_store.read_manifest(eval_run_id).cases, eval_case_id)
        if case.state in {CaseRunState.PENDING, CaseRunState.STARTING, CaseRunState.RUNNING}:
            self.run_store.update_case(
                eval_run_id,
                eval_case_id,
                state=CaseRunState.RUNNING,
                session_id=session_id,
            )
            return
        self.run_store.update_case(eval_run_id, eval_case_id, session_id=session_id)

    def _mark_case_runtime_failure(self, eval_run_id: str, eval_case_id: str, exc: Exception) -> CaseRunState:
        manifest = self.run_store.update_case(
            eval_run_id,
            eval_case_id,
            state=CaseRunState.TIMEOUT,
            finished_at=self.clock(),
            waiting_reply_id=None,
            error=f'buyer runtime failure: {type(exc).__name__}: {exc}',
        )
        return _find_case(manifest.cases, eval_case_id).state

    def _refresh_run_status(self, eval_run_id: str) -> EvalRunManifest:
        manifest = self.run_store.read_manifest(eval_run_id)
        if all(_is_terminal_case_state(case.state) for case in manifest.cases):
            return self._write_run_status(eval_run_id, EvalRunStatus.FINISHED)
        return self._write_run_status(eval_run_id, EvalRunStatus.RUNNING)

    def _write_run_status(self, eval_run_id: str, status: EvalRunStatus) -> EvalRunManifest:
        manifest = self.run_store.read_manifest(eval_run_id)
        updated = manifest.model_copy(update={'status': status, 'updated_at': self.clock()})
        self.run_store.write_manifest(updated)
        return updated

    def _remaining_manifest_cases(self, eval_run_id: str, *, after_eval_case_id: str) -> list[EvalCase]:
        manifest = self.run_store.read_manifest(eval_run_id)
        remaining_case_ids: list[str] = []
        current_case_seen = False
        for run_case in manifest.cases:
            if run_case.eval_case_id == after_eval_case_id:
                current_case_seen = True
                continue
            if current_case_seen:
                remaining_case_ids.append(run_case.eval_case_id)

        if not current_case_seen:
            raise KeyError(after_eval_case_id)

        cases_by_id = {case.eval_case_id: case for case in self.case_registry.load_cases()}
        remaining_cases: list[EvalCase] = []
        for eval_case_id in remaining_case_ids:
            case = cases_by_id.get(eval_case_id)
            if case is None:
                raise ValueError(f'eval case из manifest не найден в registry: {eval_case_id}')
            remaining_cases.append(case)
        return remaining_cases


@router.post('/runs', response_model=EvalRunManifest)
async def create_eval_run(request: Request, payload: RunCreateRequest | None = None) -> EvalRunManifest:
    orchestrator = get_run_orchestrator(request)
    try:
        return await orchestrator.create_run(
            selected_case_ids=payload.case_ids if payload is not None else None,
            callback_url=build_buyer_callback_url(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


def generate_eval_run_id() -> str:
    timestamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    return f'eval-{timestamp}-{uuid.uuid4().hex[:8]}'


def get_run_orchestrator(request: Request) -> RunOrchestrator:
    settings = request.app.state.settings
    return RunOrchestrator(
        case_registry=getattr(request.app.state, 'case_registry', CaseRegistry(settings.eval_cases_dir)),
        run_store=_get_run_store(request),
        buyer_client=_get_buyer_client(request),
        auth_profile_loader=getattr(
            request.app.state,
            'auth_profile_loader',
            AuthProfileLoader(settings.eval_auth_profiles_dir),
        ),
        run_id_generator=getattr(request.app.state, 'eval_run_id_generator', None),
        clock=getattr(request.app.state, 'orchestrator_clock', None),
        monotonic=getattr(request.app.state, 'orchestrator_monotonic', None),
        sleep=getattr(request.app.state, 'orchestrator_sleep', None),
        timeout_seconds=float(getattr(request.app.state, 'orchestrator_timeout_seconds', DEFAULT_CASE_TIMEOUT_SECONDS)),
        poll_interval_seconds=float(
            getattr(request.app.state, 'orchestrator_poll_interval_seconds', DEFAULT_POLL_INTERVAL_SECONDS)
        ),
        payment_ready_grace_seconds=float(
            getattr(request.app.state, 'orchestrator_payment_ready_grace_seconds', DEFAULT_PAYMENT_READY_GRACE_SECONDS)
        ),
    )


def _get_run_store(request: Request) -> RunStore:
    store = getattr(request.app.state, 'run_store', None)
    if store is None:
        store = RunStore(request.app.state.settings.eval_runs_dir)
        request.app.state.run_store = store
    return store


def _get_buyer_client(request: Request) -> BuyerClient:
    client = getattr(request.app.state, 'buyer_client', None)
    if client is None:
        client = BuyerClient(request.app.state.settings.buyer_api_base_url)
        request.app.state.buyer_client = client
    return client


def _select_cases(cases: Sequence[EvalCase], selected_case_ids: Sequence[str] | None) -> list[EvalCase]:
    if not selected_case_ids:
        return list(cases)

    by_id = {case.eval_case_id: case for case in cases}
    selected: list[EvalCase] = []
    seen: set[str] = set()
    for eval_case_id in selected_case_ids:
        if eval_case_id in seen:
            continue
        seen.add(eval_case_id)
        case = by_id.get(eval_case_id)
        if case is None:
            raise ValueError(f'eval case не найден: {eval_case_id}')
        selected.append(case)
    return selected


def _find_case(cases: Sequence[EvalRunCase], eval_case_id: str) -> EvalRunCase:
    for case in cases:
        if case.eval_case_id == eval_case_id:
            return case
    raise KeyError(eval_case_id)


def _is_wait_or_terminal(state: CaseRunState) -> bool:
    return state == CaseRunState.WAITING_USER or _is_terminal_case_state(state)


def _is_terminal_case_state(state: CaseRunState) -> bool:
    return state in {
        CaseRunState.SKIPPED_AUTH_MISSING,
        CaseRunState.FINISHED,
        CaseRunState.FAILED,
        CaseRunState.TIMEOUT,
        CaseRunState.JUDGED,
        CaseRunState.JUDGE_FAILED,
    }


def _response_field(response: object, field_name: str) -> object:
    if isinstance(response, dict):
        return response[field_name]
    return getattr(response, field_name)


def _required_response_string(response: object, field_name: str) -> str:
    try:
        value = _response_field(response, field_name)
    except (AttributeError, KeyError) as exc:
        raise ValueError(f'buyer response не содержит {field_name}') from exc
    if not isinstance(value, str) or not value:
        raise ValueError(f'buyer response.{field_name} должен быть непустой строкой')
    return value
