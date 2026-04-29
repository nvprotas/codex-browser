from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from eval_service.app.models import (
    BuyerCallbackEnvelope,
    CaseRunState,
    EvalRunCase,
    EvalRunManifest,
    EvalRunStatus,
    validate_path_segment_id,
)


_UNSET = object()


class RunStore:
    def __init__(self, runs_dir: Path | str, *, clock: Callable[[], datetime] | None = None) -> None:
        self.runs_dir = Path(runs_dir)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_locks: dict[str, Lock] = {}
        self._run_locks_guard = Lock()

    def run_dir(self, eval_run_id: str) -> Path:
        validate_path_segment_id(eval_run_id, 'eval_run_id')
        return self.runs_dir / eval_run_id

    def manifest_path(self, eval_run_id: str) -> Path:
        return self.run_dir(eval_run_id) / 'manifest.json'

    def summary_path(self, eval_run_id: str) -> Path:
        return self.run_dir(eval_run_id) / 'summary.json'

    def create_run(
        self,
        eval_run_id: str,
        *,
        cases: Sequence[EvalRunCase],
        status: EvalRunStatus = EvalRunStatus.PENDING,
    ) -> EvalRunManifest:
        now = self._now()
        run_dir = self.run_dir(eval_run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        manifest = EvalRunManifest(
            eval_run_id=eval_run_id,
            status=status,
            created_at=now,
            updated_at=now,
            cases=list(cases),
        )
        self.write_manifest(manifest)
        return manifest

    def read_manifest(self, eval_run_id: str) -> EvalRunManifest:
        return EvalRunManifest.model_validate_json(self.manifest_path(eval_run_id).read_text(encoding='utf-8'))

    def write_manifest(self, manifest: EvalRunManifest) -> Path:
        path = self.manifest_path(manifest.eval_run_id)
        _write_json_atomic(path, manifest.model_dump(mode='json'))
        return path

    def update_run_status(self, eval_run_id: str, status: EvalRunStatus) -> EvalRunManifest:
        with self._run_lock(eval_run_id):
            manifest = self.read_manifest(eval_run_id)
            updated = _replace_manifest_fields(manifest, status=status, updated_at=self._now())
            self.write_manifest(updated)
            return updated

    def find_case_by_session_id(self, session_id: str) -> tuple[str, str] | None:
        matches: list[tuple[str, str]] = []
        if not self.runs_dir.exists():
            return None

        for manifest_path in sorted(self.runs_dir.glob('*/manifest.json')):
            manifest = EvalRunManifest.model_validate_json(manifest_path.read_text(encoding='utf-8'))
            for case in manifest.cases:
                if case.session_id == session_id:
                    matches.append((manifest.eval_run_id, case.eval_case_id))

        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f'session_id неоднозначен в eval manifests: {session_id}')
        return matches[0]

    def update_case(
        self,
        eval_run_id: str,
        eval_case_id: str,
        *,
        state: CaseRunState | object = _UNSET,
        session_id: str | None | object = _UNSET,
        started_at: datetime | None | object = _UNSET,
        finished_at: datetime | None | object = _UNSET,
        waiting_reply_id: str | None | object = _UNSET,
        error: str | None | object = _UNSET,
        artifact_paths: Mapping[str, str] | None = None,
    ) -> EvalRunManifest:
        return self._update_case(
            eval_run_id,
            eval_case_id,
            state=state,
            session_id=session_id,
            started_at=started_at,
            finished_at=finished_at,
            waiting_reply_id=waiting_reply_id,
            error=error,
            artifact_paths=artifact_paths,
        )

    def append_callback_event(
        self,
        eval_run_id: str,
        eval_case_id: str,
        event: BuyerCallbackEnvelope,
        *,
        state: CaseRunState | object = _UNSET,
        session_id: str | None | object = _UNSET,
        started_at: datetime | None | object = _UNSET,
        finished_at: datetime | None | object = _UNSET,
        waiting_reply_id: str | None | object = _UNSET,
        error: str | None | object = _UNSET,
        artifact_paths: Mapping[str, str] | None = None,
    ) -> EvalRunManifest:
        return self._update_case(
            eval_run_id,
            eval_case_id,
            state=state,
            session_id=event.session_id if session_id is _UNSET else session_id,
            started_at=started_at,
            finished_at=finished_at,
            waiting_reply_id=waiting_reply_id,
            error=error,
            artifact_paths=artifact_paths,
            callback_event=event,
        )

    def write_summary(self, eval_run_id: str, summary: Mapping[str, Any]) -> Path:
        with self._run_lock(eval_run_id):
            path = self.summary_path(eval_run_id)
            _write_json_atomic(path, summary)
            manifest = self.read_manifest(eval_run_id)
            updated = _replace_manifest_fields(manifest, updated_at=self._now(), summary_path='summary.json')
            self.write_manifest(updated)
            return path

    def _update_case(
        self,
        eval_run_id: str,
        eval_case_id: str,
        *,
        state: CaseRunState | object = _UNSET,
        session_id: str | None | object = _UNSET,
        started_at: datetime | None | object = _UNSET,
        finished_at: datetime | None | object = _UNSET,
        waiting_reply_id: str | None | object = _UNSET,
        error: str | None | object = _UNSET,
        artifact_paths: Mapping[str, str] | None = None,
        callback_event: BuyerCallbackEnvelope | None = None,
    ) -> EvalRunManifest:
        validate_path_segment_id(eval_case_id, 'eval_case_id')
        with self._run_lock(eval_run_id):
            manifest = self.read_manifest(eval_run_id)
            cases = list(manifest.cases)
            case_index = _find_case_index(cases, eval_case_id)
            case = cases[case_index]
            if callback_event is not None and _has_callback_event(case, callback_event):
                return manifest

            case_data = case.model_dump()

            _set_if_present(case_data, 'state', state)
            _set_if_present(case_data, 'session_id', session_id)
            _set_if_present(case_data, 'started_at', started_at)
            _set_if_present(case_data, 'finished_at', finished_at)
            _set_if_present(case_data, 'waiting_reply_id', waiting_reply_id)
            _set_if_present(case_data, 'error', error)
            if artifact_paths:
                case_data['artifact_paths'] = {**case.artifact_paths, **artifact_paths}
            if callback_event is not None:
                case_data['callback_events'] = [*case.callback_events, callback_event]

            cases[case_index] = EvalRunCase.model_validate(case_data)
            updated = _replace_manifest_fields(manifest, updated_at=self._now(), cases=cases)
            self.write_manifest(updated)
            return updated

    def _now(self) -> datetime:
        return self._clock()

    def _run_lock(self, eval_run_id: str) -> Lock:
        validate_path_segment_id(eval_run_id, 'eval_run_id')
        with self._run_locks_guard:
            lock = self._run_locks.get(eval_run_id)
            if lock is None:
                lock = Lock()
                self._run_locks[eval_run_id] = lock
            return lock


def _replace_manifest_fields(manifest: EvalRunManifest, **updates: Any) -> EvalRunManifest:
    data = manifest.model_dump()
    data.update(updates)
    return EvalRunManifest.model_validate(data)


def _find_case_index(cases: Sequence[EvalRunCase], eval_case_id: str) -> int:
    for index, case in enumerate(cases):
        if case.eval_case_id == eval_case_id:
            return index
    raise KeyError(f'eval case is absent from run manifest: {eval_case_id}')


def _has_callback_event(case: EvalRunCase, event: BuyerCallbackEnvelope) -> bool:
    return any(
        existing.event_id == event.event_id or existing.idempotency_key == event.idempotency_key
        for existing in case.callback_events
    )


def _set_if_present(data: dict[str, Any], key: str, value: Any) -> None:
    if value is not _UNSET:
        data[key] = value


def _write_json_atomic(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path = path.with_name(f'.{path.name}.tmp')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp_path.write_text(f'{text}\n', encoding='utf-8')
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
