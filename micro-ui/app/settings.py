from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'micro-ui-mvp'
    buyer_base_url: str = 'http://buyer:8000'
    eval_service_base_url: str = 'http://eval_service:8090'
    ui_poll_interval_sec: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
