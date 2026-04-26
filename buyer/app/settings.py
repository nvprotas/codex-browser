from __future__ import annotations

import socket
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'buyer-mvp'
    middle_callback_url: str = 'http://micro-ui:8080/callbacks'
    novnc_public_url: str = 'http://localhost:6901/vnc.html?autoconnect=1&resize=scale'
    browser_cdp_endpoint: str = 'http://browser:9223'
    cdp_recovery_window_sec: float = Field(default=20.0, ge=0.0)
    cdp_recovery_interval_ms: int = Field(default=500, ge=1)

    codex_bin: str = 'codex'
    codex_model: str | None = None
    buyer_model_strategy: Literal['single', 'fast_then_strong'] = 'single'
    buyer_fast_codex_model: str = 'gpt-5.4-mini'
    buyer_strong_codex_model: str | None = None
    codex_timeout_sec: int = 1800
    codex_workdir: str = '/workspace'
    codex_skip_git_repo_check: bool = True
    codex_sandbox_mode: Literal['read-only', 'workspace-write', 'danger-full-access'] = 'danger-full-access'
    buyer_trace_dir: str = '/workspace/.tmp/buyer-observability'
    buyer_prompt_preview_chars: int = Field(default=2000, ge=0)
    buyer_stream_tail_chars: int = Field(default=4000, ge=200)
    buyer_browser_actions_tail: int = Field(default=40, ge=1)

    callback_timeout_sec: float = 10.0
    callback_retries: int = 3
    callback_backoff_sec: float = 0.8

    sberid_allowlist: str = 'litres.ru,brandshop.ru,kuper.ru,samokat.ru,okko.tv'
    sberid_auth_retry_budget: int = Field(default=1, ge=0)
    auth_scripts_dir: str = '/app/scripts'
    auth_script_timeout_sec: int = Field(default=90, ge=5)
    purchase_script_allowlist: str = 'litres.ru'
    purchase_script_timeout_sec: int = Field(default=120, ge=5)

    state_backend: Literal['memory', 'postgres'] = 'memory'
    database_url: str = 'postgresql://buyer:buyer@postgres:5432/buyer'
    postgres_pool_min_size: int = Field(default=1, ge=1)
    postgres_pool_max_size: int = Field(default=5, ge=1)

    runtime_backend: Literal['memory', 'redis'] = 'memory'
    redis_url: str = 'redis://redis:6379/0'
    redis_key_prefix: str = 'buyer:runtime'
    buyer_worker_id: str = Field(default_factory=socket.gethostname)
    max_active_jobs_per_worker: int = Field(default=4, ge=1)
    max_handoff_sessions: int = Field(default=1, ge=1)
    domain_active_limit_default: int | None = Field(default=1, ge=1)
    domain_active_limits: str = ''
    runtime_lock_ttl_sec: int = Field(default=3600, ge=1)
    runtime_marker_ttl_sec: int = Field(default=300, ge=1)

    status_ttl_sec: int = Field(default=86400, ge=60)


@lru_cache
def get_settings() -> Settings:
    return Settings()
