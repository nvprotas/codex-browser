from __future__ import annotations

from fastapi.testclient import TestClient

from eval_service.app.main import create_app


def test_healthz_does_not_require_buyer() -> None:
    client = TestClient(create_app())

    response = client.get('/healthz')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'service': 'eval_service'}
