from __future__ import annotations

from pathlib import Path

from eval_service.app.settings import Settings


def test_settings_defaults_define_eval_runtime_paths() -> None:
    settings = Settings(_env_file=None)

    assert settings.buyer_api_base_url == 'http://buyer:8000'
    assert settings.buyer_trace_dir == Path('/workspace/.tmp/buyer-observability')
    assert settings.eval_runs_dir == Path('/workspace/eval/runs')
    assert settings.eval_cases_dir == Path('/workspace/eval/cases')
    assert settings.eval_auth_profiles_dir == Path('/run/eval/auth-profiles')
    assert settings.eval_judge_model == 'gpt-5.5'
    assert settings.eval_baseline_window == 5


def test_settings_read_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv('BUYER_API_BASE_URL', 'http://buyer.local:9000')
    monkeypatch.setenv('EVAL_BASELINE_WINDOW', '9')

    settings = Settings(_env_file=None)

    assert settings.buyer_api_base_url == 'http://buyer.local:9000'
    assert settings.eval_baseline_window == 9
