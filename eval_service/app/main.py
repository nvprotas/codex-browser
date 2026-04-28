from __future__ import annotations

from fastapi import FastAPI

from .callbacks import router as callbacks_router
from .settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app = FastAPI(title=app_settings.app_name, version='0.1.0')
    app.state.settings = app_settings
    app.include_router(callbacks_router)

    @app.get('/healthz')
    async def healthz() -> dict[str, str]:
        return {'status': 'ok', 'service': 'eval_service'}

    return app


app = create_app()
