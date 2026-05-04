from __future__ import annotations

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
    codex_model: str | None = 'gpt-5.5'
    codex_timeout_sec: int = 1800
    codex_workdir: str = '/workspace'
    codex_skip_git_repo_check: bool = True
    codex_sandbox_mode: Literal['read-only', 'workspace-write', 'danger-full-access'] = 'danger-full-access'
    codex_reasoning_effort: Literal['none', 'low', 'medium', 'high', 'xhigh'] | None = 'low'
    codex_reasoning_summary: Literal['auto', 'concise', 'detailed', 'none'] | None = 'none'
    codex_web_search: Literal['disabled', 'cached', 'live'] | None = 'disabled'
    codex_image_generation: Literal['disabled', 'enabled'] = 'disabled'
    buyer_trace_dir: str = '/workspace/.tmp/buyer-observability'
    buyer_user_info_path: str = '/run/buyer/user-buyer-info.md'
    buyer_user_info_max_chars: int = Field(default=12000, ge=1)
    buyer_prompt_preview_chars: int = Field(default=2000, ge=0)
    buyer_stream_tail_chars: int = Field(default=4000, ge=200)
    buyer_browser_actions_tail: int = Field(default=40, ge=1)

    callback_timeout_sec: float = 10.0
    callback_retries: int = 3
    callback_backoff_sec: float = 0.8
    trusted_callback_urls: str = 'http://eval_service:8090/callbacks/buyer'

    sberid_allowlist: str = 'litres.ru,brandshop.ru'
    sber_auth_source: Literal['inline_only', 'external_cookies_api'] = 'inline_only'
    sber_cookies_api_url: str = ''
    sber_cookies_api_timeout_sec: float = Field(default=5.0, ge=0.1)
    sber_cookies_api_retries: int = Field(default=1, ge=0)
    auth_scripts_dir: str = '/app/scripts'
    auth_script_timeout_sec: int = Field(default=90, ge=5)

    state_backend: Literal['memory', 'postgres'] = 'memory'
    database_url: str = 'postgresql://buyer:buyer@postgres:5432/buyer'
    postgres_pool_min_size: int = Field(default=1, ge=1)
    postgres_pool_max_size: int = Field(default=5, ge=1)

    max_active_sessions: int = 1

    status_ttl_sec: int = Field(default=86400, ge=60)


@lru_cache
def get_settings() -> Settings:
    return Settings()
