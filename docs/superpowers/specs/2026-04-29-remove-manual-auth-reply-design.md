# Remove Manual Auth Reply Design

> **Статус:** утвержденный дизайн для `MON-29`.
> **Linear:** [MON-29](https://linear.app/monaco-dev/issue/MON-29/buyer-phase-1-udalit-ruchnuyu-peredachu-auth-paketov-cherez).

## Цель

Удалить UX, при котором `buyer` просит пользователя или оператора прислать cookies, Playwright `storageState`, localStorage, tokens или JSON auth-пакет через `ask_user` и `/v1/replies`.

## Текущее поведение

Сейчас при невалидном inline `auth.storageState` или неуспешном SberId auth-скрипте `buyer` отправляет `ask_user` с просьбой отправить новый JSON auth-пакет. Это оставляет чувствительные auth-данные в пользовательском диалоге и смешивает два разных канала: пользовательские решения по покупке и машинную передачу auth-секретов.

## Целевое поведение

`ask_user` и `/v1/replies` остаются только для пользовательских решений по покупке: параметры товара, подтверждение замены, адресные уточнения, операторские инструкции и продолжение после handoff.

Auth-пакеты больше нельзя запрашивать или принимать через пользовательский reply. Если inline `auth.storageState` отсутствует или невалиден, `buyer` фиксирует machine-readable reason-code в `auth_summary` и продолжает guest-flow. Если магазин дальше блокирует покупку логином, применяется обычный handoff без передачи cookies через чат.

## Reason-коды

- `auth_not_provided`: auth-пакет не передан.
- `auth_inline_invalid_payload`: inline `storageState` передан, но не прошел валидацию.
- Существующие script reason-коды (`auth_refresh_requested`, `auth_failed_redirect_loop`, `auth_failed_invalid_session`) больше не запускают запрос нового auth-пакета через `ask_user`.

## Не входит

- Получение cookies из внешнего cookies API.
- Настройки `SBER_AUTH_SOURCE` и `SBER_COOKIES_API_*`.
- Вызов `GET /api/v1/cookies`.

Эти пункты вынесены в отдельную follow-up задачу [MON-30](https://linear.app/monaco-dev/issue/MON-30/buyer-phase-1-external-sber-cookies-api-kak-auth-source).

## Самопроверка

- Scope не включает внешний cookies API.
- Запрещенный UX сформулирован явно.
- Долговременное хранение cookies/storageState не вводится.
