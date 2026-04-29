from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eval_service.app.models import CaseRunState


DEFAULT_AUTH_PROFILES_DIR = Path('/run/eval/auth-profiles')


class AuthProfileSkipReason(BaseModel):
    model_config = ConfigDict(extra='forbid')

    state: CaseRunState = CaseRunState.SKIPPED_AUTH_MISSING
    reason: Literal['auth_profile_missing', 'auth_profile_invalid']
    auth_profile: str = Field(min_length=1)
    message: str = Field(min_length=1)


class AuthProfileLoadResult(BaseModel):
    model_config = ConfigDict(extra='forbid')

    storage_state: dict[str, Any] | None = None
    skip_reason: AuthProfileSkipReason | None = None


class AuthProfileLoader:
    def __init__(self, base_dir: str | Path = DEFAULT_AUTH_PROFILES_DIR) -> None:
        self.base_dir = Path(base_dir)

    def load(self, auth_profile: str | None) -> AuthProfileLoadResult:
        if not auth_profile:
            return AuthProfileLoadResult()

        profile_path = self._profile_path(auth_profile)
        if profile_path is None or not profile_path.is_file():
            return AuthProfileLoadResult(
                skip_reason=AuthProfileSkipReason(
                    reason='auth_profile_missing',
                    auth_profile=auth_profile,
                    message=f'Auth-профиль {auth_profile!r} не найден.',
                )
            )

        try:
            raw_profile = profile_path.read_text(encoding='utf-8')
            storage_state = json.loads(raw_profile)
        except (JSONDecodeError, OSError, UnicodeDecodeError):
            return self._invalid_result(auth_profile)

        if not isinstance(storage_state, dict):
            return self._invalid_result(auth_profile)

        return AuthProfileLoadResult(storage_state=storage_state)

    def _profile_path(self, auth_profile: str) -> Path | None:
        profile_name = f'{auth_profile}.json'
        if Path(profile_name).name != profile_name:
            return None
        return self.base_dir / profile_name

    @staticmethod
    def _invalid_result(auth_profile: str) -> AuthProfileLoadResult:
        return AuthProfileLoadResult(
            skip_reason=AuthProfileSkipReason(
                reason='auth_profile_invalid',
                auth_profile=auth_profile,
                message=f'Auth-профиль {auth_profile!r} не является валидным storageState JSON.',
            )
        )


def load_auth_profile(
    auth_profile: str | None,
    *,
    base_dir: str | Path = DEFAULT_AUTH_PROFILES_DIR,
) -> AuthProfileLoadResult:
    return AuthProfileLoader(base_dir).load(auth_profile)
