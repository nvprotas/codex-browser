from __future__ import annotations

import json

from eval_service.app.auth_profiles import AuthProfileLoader
from eval_service.app.models import CaseRunState


def test_auth_profile_loader_reads_storage_state_from_configured_dir(tmp_path) -> None:
    storage_state = {
        'cookies': [{'name': 'session', 'value': 'secret-cookie'}],
        'origins': [{'origin': 'https://www.litres.ru', 'localStorage': []}],
    }
    (tmp_path / 'litres_sberid.json').write_text(json.dumps(storage_state), encoding='utf-8')

    result = AuthProfileLoader(tmp_path).load('litres_sberid')

    assert result.storage_state == storage_state
    assert result.skip_reason is None


def test_missing_auth_profile_returns_structured_skip(tmp_path) -> None:
    result = AuthProfileLoader(tmp_path).load('missing_profile')

    assert result.storage_state is None
    assert result.skip_reason is not None
    assert result.skip_reason.state == CaseRunState.SKIPPED_AUTH_MISSING
    assert result.skip_reason.reason == 'auth_profile_missing'
    assert result.skip_reason.auth_profile == 'missing_profile'


def test_invalid_auth_profile_returns_safe_structured_skip(tmp_path) -> None:
    (tmp_path / 'broken.json').write_text(
        '{"cookies":[{"name":"session","value":"raw-secret-token"}]',
        encoding='utf-8',
    )

    result = AuthProfileLoader(tmp_path).load('broken')

    assert result.storage_state is None
    assert result.skip_reason is not None
    assert result.skip_reason.state == CaseRunState.SKIPPED_AUTH_MISSING
    assert result.skip_reason.reason == 'auth_profile_invalid'
    assert result.skip_reason.auth_profile == 'broken'
    assert 'raw-secret-token' not in result.skip_reason.message


def test_empty_auth_profile_means_no_auth_required(tmp_path) -> None:
    result = AuthProfileLoader(tmp_path).load(None)

    assert result.storage_state is None
    assert result.skip_reason is None
