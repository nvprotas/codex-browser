from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_eval_service_dockerfile_preserves_package_layout() -> None:
    dockerfile = (ROOT / 'eval_service' / 'Dockerfile').read_text(encoding='utf-8')

    assert 'COPY eval_service ./eval_service' in dockerfile
    assert 'eval_service.app.main:app' in dockerfile


def test_eval_service_dockerfile_installs_codex_cli() -> None:
    dockerfile = (ROOT / 'eval_service' / 'Dockerfile').read_text(encoding='utf-8')

    assert 'nodejs' in dockerfile
    assert 'npm' in dockerfile
    assert 'npm install -g @openai/codex' in dockerfile
    assert 'npm install -g @openai/codex || true' not in dockerfile
