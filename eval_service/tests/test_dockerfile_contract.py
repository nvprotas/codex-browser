from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_eval_service_dockerfile_preserves_package_layout() -> None:
    dockerfile = (ROOT / 'eval_service' / 'Dockerfile').read_text(encoding='utf-8')

    assert 'WORKDIR /workspace' in dockerfile
    assert 'WORKDIR /app' not in dockerfile
    assert 'COPY eval_service ./eval_service' in dockerfile
    assert 'eval_service.app.main:app' in dockerfile


def test_eval_service_dockerfile_installs_codex_cli() -> None:
    dockerfile = (ROOT / 'eval_service' / 'Dockerfile').read_text(encoding='utf-8')

    assert 'nodejs' in dockerfile
    assert 'npm' in dockerfile
    assert 'npm install -g @openai/codex' in dockerfile
    assert 'npm install -g @openai/codex || true' not in dockerfile


def test_eval_service_dockerfile_uses_entrypoint_for_codex_oauth() -> None:
    dockerfile = (ROOT / 'eval_service' / 'Dockerfile').read_text(encoding='utf-8')
    entrypoint = (ROOT / 'eval_service' / 'docker' / 'entrypoint.sh').read_text(encoding='utf-8')

    assert 'RUN chmod +x /workspace/eval_service/docker/entrypoint.sh' in dockerfile
    assert 'ENTRYPOINT ["/workspace/eval_service/docker/entrypoint.sh"]' in dockerfile
    assert 'CMD ["uvicorn", "eval_service.app.main:app", "--host", "0.0.0.0", "--port", "8090"]' in dockerfile
    assert '/run/codex/host-auth' in entrypoint
    assert '/root/.codex/auth.json' in entrypoint
