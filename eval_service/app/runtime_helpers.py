from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import Request

from eval_service.app.buyer_client import BuyerClient
from eval_service.app.models import CaseRunState, EvalRunCase
from eval_service.app.run_store import RunStore


def get_run_store(request: Request) -> RunStore:
    return get_run_store_from_app(request.app)


def get_run_store_from_app(app: Any) -> RunStore:
    store = getattr(app.state, 'run_store', None)
    if store is None:
        store = RunStore(app.state.settings.eval_runs_dir)
        app.state.run_store = store
    return store


def get_buyer_client(request: Request) -> BuyerClient:
    client = getattr(request.app.state, 'buyer_client', None)
    if client is None:
        client = BuyerClient(request.app.state.settings.buyer_api_base_url)
        request.app.state.buyer_client = client
    return client


def find_case(cases: Sequence[EvalRunCase], eval_case_id: str) -> EvalRunCase:
    for case in cases:
        if case.eval_case_id == eval_case_id:
            return case
    raise KeyError(eval_case_id)


def is_terminal_case_state(state: CaseRunState) -> bool:
    return state in {
        CaseRunState.SKIPPED_AUTH_MISSING,
        CaseRunState.UNVERIFIED,
        CaseRunState.FINISHED,
        CaseRunState.FAILED,
        CaseRunState.TIMEOUT,
        CaseRunState.JUDGED,
        CaseRunState.JUDGE_FAILED,
    }


def response_field(response: object, field_name: str) -> object:
    if isinstance(response, dict):
        return response[field_name]
    return getattr(response, field_name)
