# Persistent State Postgres Design

## Контекст

`buyer` сейчас хранит `SessionState` в памяти процесса. После рестарта теряются статусы, события, ожидаемые ответы и история выполнения, хотя внешний контракт `/v1/tasks`, `/v1/sessions`, `/v1/replies` уже предполагает устойчивый `session_id`.

Roadmap фиксирует первую задачу Phase 1: Postgres для задач, сессий, событий, ответов и артефактов. Redis и автоматическое продолжение выполнения после рестарта остаются отдельной задачей.

## Границы Реализации

- Postgres становится backend по умолчанию в `docker-compose.yml`.
- `STATE_BACKEND=postgres|memory` оставляет in-memory backend для unit-тестов и локального escape hatch.
- Внешний API не меняется: `/v1/tasks`, `/v1/sessions`, `/v1/sessions/{session_id}`, `/v1/replies`.
- `SessionStore` остается фасадом для `BuyerService`, но долговременные данные пишет через repository-слой.
- `task_ref` и `wake_event` остаются runtime-only полями процесса.
- После рестарта `buyer` восстанавливает сохраненные сессии, статусы, события и agent memory, но не автопродолжает runner. Повторный старт активных сессий будет проектироваться вместе с Redis locks/runtime markers.

## Auth И Browser State

`storageState` Playwright не сохраняется в Postgres. Он может жить только в памяти текущей runtime-сессии и передаваться в auth-скрипты. В БД допускаются только безопасные runtime-метаданные auth flow: provider, domain, mode, path, reason_code, attempts, context_prepared и служебные timestamps.

Отдельная future-задача должна исследовать, можно ли безопасно сохранять браузерное состояние между рестартами. Эта задача не входит в текущую реализацию, потому что она меняет границу безопасности auth/session data и связана с изоляцией браузерных контекстов.

## Схема Данных

- `buyer_sessions`: `session_id`, `task`, `start_url`, `callback_url`, `novnc_url`, `status`, `metadata`, `last_error`, `created_at`, `updated_at`.
- `buyer_events`: `event_id`, `session_id`, `event_type`, `occurred_at`, `idempotency_key`, `payload`, `delivery_status`, `delivery_error`, `created_at`.
- `buyer_replies`: `reply_id`, `session_id`, `question`, `message`, `status`, `reason_code`, `context`, `created_at`, `answered_at`.
- `buyer_artifacts`: `artifact_id`, `session_id`, `artifact_type`, `uri`, `metadata`, `created_at`.
- `buyer_auth_context`: `session_id`, `provider`, `domain`, `mode`, `path`, `reason_code`, `attempts`, `context_prepared`, `metadata`, `updated_at`.
- `buyer_agent_memory`: ordered role/text messages used by the runner loop.

## Миграции И Startup

Легковесный migration runner выполняется внутри `buyer` через `asyncpg`. Он создает таблицу `buyer_schema_migrations` и последовательно применяет SQL-миграции. Для MVP не добавляется Alembic, чтобы не заводить отдельный CLI и конфигурационный контур.

`buyer` проверяет Postgres при старте FastAPI и закрывает connection pool при shutdown. Методы repository также лениво инициализируют pool, чтобы unit-тесты могли работать с repository напрямую.

## Поведение Store

- `create_session` проверяет лимит активных сессий по Postgres и создает запись со статусом `created`.
- `set_status`, `set_waiting_question`, `apply_reply`, `pop_reply`, `append_event`, `add_agent_memory` выполняют атомарные DB-операции и возвращают свежий `SessionState`.
- `get` и `list_sessions` возвращают состояние вместе с событиями и agent memory.
- `status_ttl_sec` сохраняет прежнюю семантику: terminal-сессии удаляются при list/create через repository cleanup.
- Pending replies восстанавливаются через последнюю открытую запись `buyer_replies`, но `wake_event` остается только внутри текущего процесса.

## Тестирование

- Unit-тесты текущего in-memory store остаются зелеными.
- Добавляются тесты repository/store на сохранение статуса, событий, replies и agent memory между двумя экземплярами store на одном repository.
- Добавляется тест, что `storageState` не попадает в Postgres-представление сессии после пересоздания store.
- Тесты FastAPI/main покрывают выбор backend-а настройками без изменения внешнего API.

## Открытые Вопросы

- Как безопасно сохранять и восстанавливать browser context/storage между рестартами без утечки cookies/tokens.
- Как перезапускать активные сессии после рестарта worker-а без двойного runner-а. Это относится к Redis locks/runtime markers.
