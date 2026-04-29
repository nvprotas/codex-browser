from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class ExternalSberCookiesResult:
    reason_code: str
    storage_state: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    message: str | None = None


class ExternalSberCookiesClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_sec: float,
        retries: int,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip('/')
        self._timeout_sec = timeout_sec
        self._retries = max(retries, 0)
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_sec)

    async def fetch_storage_state(self) -> ExternalSberCookiesResult:
        if not self._base_url:
            return ExternalSberCookiesResult(
                reason_code='auth_external_unavailable',
                metadata={'attempts': 0},
                message='External Sber cookies API URL is not configured.',
            )

        attempts = self._retries + 1
        last_error: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http_client.get(
                    f'{self._base_url}/api/v1/cookies',
                    timeout=self._timeout_sec,
                )
                response.raise_for_status()
                result = cookies_payload_to_storage_state(response.json())
                if result.storage_state is None and attempt < attempts:
                    last_error = result.message
                    continue
                metadata = dict(result.metadata)
                metadata['attempts'] = attempt
                return ExternalSberCookiesResult(
                    reason_code=result.reason_code,
                    storage_state=result.storage_state,
                    metadata=metadata,
                    message=result.message,
                )
            except httpx.TimeoutException as exc:
                last_error = str(exc)
                if attempt >= attempts:
                    return ExternalSberCookiesResult(
                        reason_code='auth_external_timeout',
                        metadata={'attempts': attempt},
                        message=last_error,
                    )
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
                if attempt >= attempts:
                    return ExternalSberCookiesResult(
                        reason_code='auth_external_unavailable',
                        metadata={'attempts': attempt},
                        message=last_error,
                    )

        return ExternalSberCookiesResult(
            reason_code='auth_external_unavailable',
            metadata={'attempts': attempts},
            message=last_error,
        )

    async def aclose(self) -> None:
        await self._http_client.aclose()


def cookies_payload_to_storage_state(payload: Any) -> ExternalSberCookiesResult:
    if not isinstance(payload, dict):
        return ExternalSberCookiesResult(
            reason_code='auth_external_invalid_payload',
            metadata={},
            message='External cookies payload must be an object.',
        )

    cookies = payload.get('cookies')
    if not isinstance(cookies, list):
        return ExternalSberCookiesResult(
            reason_code='auth_external_invalid_payload',
            metadata={},
            message='External cookies payload must contain cookies array.',
        )
    if not cookies:
        return ExternalSberCookiesResult(
            reason_code='auth_external_empty_payload',
            metadata=_metadata_from_payload(payload, cookies=[]),
            message='External cookies payload contains no cookies.',
        )

    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        if not _is_valid_cookie(cookie):
            return ExternalSberCookiesResult(
                reason_code='auth_external_invalid_payload',
                metadata=_metadata_from_payload(payload, cookies=[]),
                message='External cookie shape is invalid.',
            )
        normalized.append(dict(cookie))

    storage_state = {'cookies': normalized, 'origins': []}
    return ExternalSberCookiesResult(
        reason_code='auth_external_loaded',
        storage_state=storage_state,
        metadata=_metadata_from_payload(payload, cookies=normalized),
    )


def _metadata_from_payload(payload: dict[str, Any], *, cookies: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        'cookie_count': len(cookies),
        'domains': sorted({cookie['domain'] for cookie in cookies if isinstance(cookie.get('domain'), str)}),
    }
    updated_at = payload.get('updatedAt')
    if isinstance(updated_at, str) and updated_at:
        metadata['updated_at'] = updated_at
    return metadata


def _is_valid_cookie(cookie: Any) -> bool:
    if not isinstance(cookie, dict):
        return False
    for key in ('name', 'value', 'domain', 'path'):
        value = cookie.get(key)
        if not isinstance(value, str) or not value:
            return False
    secure = cookie.get('secure')
    if not isinstance(secure, bool):
        return False
    same_site = cookie.get('sameSite')
    if same_site is not None and same_site not in {'Strict', 'Lax', 'None'}:
        return False
    http_only = cookie.get('httpOnly')
    if http_only is not None and not isinstance(http_only, bool):
        return False
    expires = cookie.get('expires')
    if expires is not None and not isinstance(expires, int | float):
        return False
    return True
