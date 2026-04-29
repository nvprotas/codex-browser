# External Sber Cookies API Design

> **Статус:** утвержденный дизайн для `MON-30`.
> **Linear:** [MON-30](https://linear.app/monaco-dev/issue/MON-30/buyer-phase-1-external-sber-cookies-api-kak-auth-source).
> **Зависимость:** [MON-29](https://linear.app/monaco-dev/issue/MON-29/buyer-phase-1-udalit-ruchnuyu-peredachu-auth-paketov-cherez).

## Цель

Добавить машинный источник SberId auth-пакета: `buyer` должен уметь получить cookies из внешнего Sber cookies API, преобразовать их в Playwright `storageState` и использовать в существующем SberId scripts-first flow.

## Внешний контракт

Внешний сервис описан OpenAPI-файлом `buyer-sber-cookies-api-openapi (1).yaml`:

- `GET /api/v1/cookies`: вернуть текущий набор cookies;
- `POST /api/v1/cookies`: сохранить cookies.

Для `buyer` в `MON-30` нужен только read-path `GET /api/v1/cookies`. Write-path остается ответственностью внешнего auth-контура.

## Источники auth

Приоритет источников:

1. Inline `auth.storageState` в `POST /v1/tasks`.
2. External cookies API, если включен через конфигурацию.
3. Guest-flow, если ни один машинный источник не дал валидный пакет.

Если inline `auth.storageState` передан, внешний cookies API для этой сессии не вызывается.

## Конфигурация

Добавить настройки:

- `SBER_AUTH_SOURCE`: `inline_only` или `external_cookies_api`.
- `SBER_COOKIES_API_URL`: базовый URL внешнего сервиса без завершающего slash.
- `SBER_COOKIES_API_TIMEOUT_SEC`: timeout одного HTTP-запроса.
- `SBER_COOKIES_API_RETRIES`: количество повторов после первой попытки.

Default: `SBER_AUTH_SOURCE=inline_only`, чтобы текущие окружения не начали ходить во внешний сервис без явного включения.

## Преобразование cookies

Успешный response:

```json
{
  "cookies": [
    {
      "name": "id_user2",
      "value": "...",
      "domain": "id.sber.ru",
      "path": "/",
      "expires": -1,
      "httpOnly": true,
      "secure": true,
      "sameSite": "Lax"
    }
  ],
  "updatedAt": "2026-04-29T10:00:00Z",
  "count": 1
}
```

Преобразуется в:

```json
{
  "cookies": [
    {
      "name": "id_user2",
      "value": "...",
      "domain": "id.sber.ru",
      "path": "/",
      "expires": -1,
      "httpOnly": true,
      "secure": true,
      "sameSite": "Lax"
    }
  ],
  "origins": []
}
```

Минимальная валидация cookie: `name`, `value`, `domain`, `path` непустые строки, `secure` boolean. `expires`, `httpOnly`, `sameSite` принимаются опционально; `sameSite` допускает только `Strict`, `Lax`, `None`.

## Reason-коды

- `auth_external_loaded`: cookies успешно получены и преобразованы в storage state.
- `auth_external_unavailable`: сервис не настроен, вернул сетевую ошибку, HTTP error или невалидный JSON.
- `auth_external_timeout`: сервис не ответил в timeout/retry budget.
- `auth_external_invalid_payload`: JSON не содержит валидного массива cookies или cookie shape невалиден.
- `auth_external_empty_payload`: сервис вернул пустой массив cookies.

## Безопасность

- Cookies/storageState живут только в runtime state текущей сессии.
- Postgres получает только sanitized auth metadata: source, reason_code, cookie_count, domains, updatedAt, attempts.
- Логи external client не должны писать cookie values.
- `MON-30` не возвращает ручную передачу cookies через пользовательский чат; это запрещено `MON-29`.

## Самопроверка

- Scope ограничен read-path external cookies API.
- Inline override и default `inline_only` указаны явно.
- Write-path `POST /api/v1/cookies` не входит.
- Долговременное хранение cookies/storageState не вводится.
