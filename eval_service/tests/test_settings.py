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
    assert settings.eval_callback_base_url is None
    assert settings.eval_callback_secret is None


def test_settings_read_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv('BUYER_API_BASE_URL', 'http://buyer.local:9000')
    monkeypatch.setenv('EVAL_BASELINE_WINDOW', '9')
    monkeypatch.setenv('EVAL_CALLBACK_BASE_URL', 'http://eval_service:8090')
    monkeypatch.setenv('EVAL_CALLBACK_SECRET', 'callback-secret')

    settings = Settings(_env_file=None)

    assert settings.buyer_api_base_url == 'http://buyer.local:9000'
    assert settings.eval_baseline_window == 9
    assert settings.eval_callback_base_url == 'http://eval_service:8090'
    assert settings.eval_callback_secret == 'callback-secret'


def test_docker_compose_binds_eval_service_to_loopback_and_sets_callback_contract() -> None:
    compose = Path('docker-compose.yml').read_text(encoding='utf-8')

    assert '- "127.0.0.1:5432:5432"' in compose
    assert '- "127.0.0.1:6901:6901"' in compose
    assert '- "127.0.0.1:8000:8000"' in compose
    assert '- "127.0.0.1:8080:8080"' in compose
    assert '- "127.0.0.1:8090:8090"' in compose
    assert '- "9223:9223"' not in compose
    assert '- "6901:6901"' not in compose
    assert '- "8000:8000"' not in compose
    assert '- "8080:8080"' not in compose
    assert '- "8090:8090"' not in compose
    assert 'TRUSTED_CALLBACK_URLS: ${TRUSTED_CALLBACK_URLS:-http://eval_service:8090/callbacks/buyer}' in compose
    assert 'EVAL_CALLBACK_BASE_URL: ${EVAL_CALLBACK_BASE_URL:-http://eval_service:8090}' in compose
    assert 'EVAL_CALLBACK_SECRET: ${EVAL_CALLBACK_SECRET:?set EVAL_CALLBACK_SECRET}' in compose
