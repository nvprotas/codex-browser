from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


REQUIRED_EVALUATION_CHECKS = frozenset(
    {
        'outcome_ok',
        'safety_ok',
        'payment_boundary_ok',
        'evidence_ok',
        'recommendations_ok',
    }
)


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class EvalRunStatus(StrEnum):
    PENDING = 'pending'
    RUNNING = 'running'
    FINISHED = 'finished'
    FAILED = 'failed'
    CANCELED = 'canceled'


class CaseRunState(StrEnum):
    PENDING = 'pending'
    SKIPPED_AUTH_MISSING = 'skipped_auth_missing'
    STARTING = 'starting'
    RUNNING = 'running'
    WAITING_USER = 'waiting_user'
    PAYMENT_READY = 'payment_ready'
    FINISHED = 'finished'
    TIMEOUT = 'timeout'
    JUDGE_PENDING = 'judge_pending'
    JUDGED = 'judged'
    JUDGE_FAILED = 'judge_failed'


class CallbackEventType(StrEnum):
    ASK_USER = 'ask_user'
    PAYMENT_READY = 'payment_ready'
    SCENARIO_FINISHED = 'scenario_finished'
    STATUS_UPDATE = 'status_update'
    ERROR = 'error'
    SESSION_STARTED = 'session_started'
    AGENT_STEP_STARTED = 'agent_step_started'
    AGENT_STEP_FINISHED = 'agent_step_finished'
    AGENT_STREAM_EVENT = 'agent_stream_event'
    HANDOFF_REQUESTED = 'handoff_requested'
    HANDOFF_RESUMED = 'handoff_resumed'


class EvaluationStatus(StrEnum):
    JUDGED = 'judged'
    JUDGE_SKIPPED = 'judge_skipped'
    JUDGE_FAILED = 'judge_failed'


class CheckStatus(StrEnum):
    OK = 'ok'
    NOT_OK = 'not_ok'
    SKIPPED = 'skipped'


class EvaluationRecommendationCategory(StrEnum):
    PROMPT = 'prompt'
    PLAYBOOK = 'playbook'
    SITE_PROFILE = 'site_profile'
    SCRIPT_CANDIDATE = 'script_candidate'
    EVAL_CASE = 'eval_case'


class RecommendationPriority(StrEnum):
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'


class ExpectedOutcome(StrictBaseModel):
    target: str = Field(min_length=1)
    stop_condition: str = Field(min_length=1)
    acceptable_variants: list[str] = Field(default_factory=list)


class EvalCase(StrictBaseModel):
    eval_case_id: str = Field(min_length=1)
    case_version: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    host: str = Field(min_length=1)
    task: str = Field(min_length=1)
    start_url: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    auth_profile: str | None = None
    expected_outcome: ExpectedOutcome
    forbidden_actions: list[str] = Field(default_factory=list)
    rubric: dict[str, Any] = Field(default_factory=dict)

    def buyer_metadata(self) -> dict[str, Any]:
        return {
            **self.metadata,
            'eval_case_id': self.eval_case_id,
            'case_version': self.case_version,
            'host': self.host,
            'case_title': self.title,
            'variant_id': self.variant_id,
        }


class BuyerCallbackEnvelope(StrictBaseModel):
    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_type: CallbackEventType
    occurred_at: datetime
    idempotency_key: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    eval_run_id: str | None = None
    eval_case_id: str | None = None


class EvalRunCase(StrictBaseModel):
    eval_case_id: str = Field(min_length=1)
    case_version: str = Field(min_length=1)
    state: CaseRunState = CaseRunState.PENDING
    session_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    waiting_reply_id: str | None = None
    error: str | None = None
    callback_events: list[BuyerCallbackEnvelope] = Field(default_factory=list)
    artifact_paths: dict[str, str] = Field(default_factory=dict)


class EvalRunManifest(StrictBaseModel):
    eval_run_id: str = Field(min_length=1)
    status: EvalRunStatus = EvalRunStatus.PENDING
    created_at: datetime
    updated_at: datetime
    cases: list[EvalRunCase] = Field(default_factory=list)
    summary_path: str | None = None


class EvidenceRef(StrictBaseModel):
    event_id: str | None = None
    trace_file: str | None = None
    browser_actions_file: str | None = None
    step_index: int | None = Field(default=None, ge=0)
    record_index: int | str | None = None
    screenshot_path: str | None = None


class EvaluationMetrics(StrictBaseModel):
    duration_ms: int | None = Field(default=None, ge=0)
    buyer_tokens_used: int | None = Field(default=None, ge=0)
    judge_tokens_used: int | None = Field(default=None, ge=0)


class EvaluationCheck(StrictBaseModel):
    status: CheckStatus
    reason: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class EvaluationRecommendation(StrictBaseModel):
    category: EvaluationRecommendationCategory
    priority: RecommendationPriority
    rationale: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    draft_text: str = Field(min_length=1)


class JudgeMetadata(StrictBaseModel):
    backend: str = Field(min_length=1)
    model: str = Field(min_length=1)


class EvaluationResult(StrictBaseModel):
    eval_run_id: str = Field(min_length=1)
    eval_case_id: str = Field(min_length=1)
    case_version: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    status: EvaluationStatus
    metrics: EvaluationMetrics
    checks: dict[str, EvaluationCheck]
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    recommendations: list[EvaluationRecommendation] = Field(default_factory=list)
    judge_metadata: JudgeMetadata

    @field_validator('checks')
    @classmethod
    def validate_required_checks(cls, checks: dict[str, EvaluationCheck]) -> dict[str, EvaluationCheck]:
        check_names = set(checks)
        if check_names != REQUIRED_EVALUATION_CHECKS:
            missing = sorted(REQUIRED_EVALUATION_CHECKS - check_names)
            unexpected = sorted(check_names - REQUIRED_EVALUATION_CHECKS)
            details = []
            if missing:
                details.append(f'нет обязательных checks: {", ".join(missing)}')
            if unexpected:
                details.append(f'лишние checks: {", ".join(unexpected)}')
            raise ValueError('; '.join(details))
        return checks
