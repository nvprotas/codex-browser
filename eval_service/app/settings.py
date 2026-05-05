from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'eval-service'
    buyer_api_base_url: str = 'http://buyer:8000'
    buyer_trace_dir: Path = Path('/workspace/.tmp/buyer-observability')
    eval_runs_dir: Path = Path('/workspace/eval/runs')
    eval_cases_dir: Path = Path('/workspace/eval/cases')
    eval_auth_profiles_dir: Path = Path('/run/eval/auth-profiles')
    eval_auth_source: Literal['auth_profiles', 'buyer_runtime'] = 'auth_profiles'
    eval_judge_model: str = 'gpt-5.5'
    eval_baseline_window: int = Field(default=5, ge=1)
    eval_callback_base_url: str | None = None
    eval_callback_secret: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
