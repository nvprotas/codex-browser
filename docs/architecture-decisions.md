# Architecture Decisions: buyer v1

## Статус

- Документ фиксирует обязательные архитектурные решения для `buyer` v1.
- Дата фиксации: 2026-04-23.
- Все новые требования по `buyer` должны явно соответствовать решениям ниже.
- Приоритизация реализации после MVP зафиксирована в `docs/buyer-roadmap.md`.

## Протоколы и данные

- Взаимодействие `buyer` ↔ `middle`: HTTP callbacks с event envelope.
- Обязательные поля envelope: `event_id`, `session_id`, `event_type`, `occurred_at`, `idempotency_key`, `payload`.
- Семантика доставки: `at-least-once` с дедупликацией на стороне `middle`.
- Профиль доставки callback: `timeout=10s`, `3 retries`, exponential backoff + jitter.
- При исчерпании retry-бюджета событие помечается как failed и переводится в ошибку сессии.
- Отдельные auth-события не добавляются; auth-статусы передаются через `ask_user` и `scenario_finished`.
- Канонические reason-коды auth: `auth_ok`, `auth_failed_payload`, `auth_failed_redirect_loop`, `auth_failed_invalid_session`, `auth_refresh_requested`.

## State и runtime

- Persistence и очередь выполнения: `Postgres`.
- Redis не является обязательной зависимостью v1. Distributed locks и отдельный worker-pool выносятся в будущий этап масштабирования.
- `Postgres`: долговременное состояние задач/сессий/артефактов, очередь задач, runtime markers, статусы ожидания пользователя и дедлайны ожидания.
- Runtime-модель v1: один `buyer` process забирает задачи из Postgres по очереди через атомарный claim и обновляет статус сессии.
- Если сессия переходит в `waiting_user`, agent runner освобождается и `buyer` может выполнять следующую задачу.
- `waiting_user` удерживает browser slot только до дедлайна ожидания ответа. После timeout сессия завершается без resume, browser slot очищается и возвращается в пул.
- Браузерный рантайм в MVP разворачивается как пул `browser-sidecar` слотов:
  - внутри sidecar: `Chromium + Xvfb + x11vnc + noVNC`,
  - `buyer` назначает сессии свободный browser slot и подключается к нему по CDP endpoint,
  - noVNC публикуется отдельно от API-контейнера для каждого доступного browser slot,
  - размер пула задается конфигурацией; динамическое создание контейнеров допускается только с явными `min/max` лимитами.
- Лимиты рантайма по умолчанию:
  - `max_active_jobs_per_worker=1`
  - `min_browser_slots=1`
  - `max_browser_slots=2`
  - `waiting_user_timeout_sec=300`
  - `max_handoff_sessions=1`
  - доменные лимиты для снижения флейков и банов.
- `POST /v1/tasks` создает сессию в статусе `queued`; worker переводит ее в `running` после claim и аренды browser slot.
- Поздний reply после `waiting_user_timeout_sec` получает `accepted=false` и `reason_code=waiting_user_timeout`.
- После рестарта `buyer` не продолжает активный runner и не восстанавливает утраченную browser page. Сессии, потерявшие runtime browser slot, должны завершаться понятной ошибкой или требовать нового запуска.
- Политика восстановления CDP (hotfix 2026-04-23):
  - единое окно восстановления: `CDP_RECOVERY_WINDOW_SEC=20`,
  - интервал retry: `CDP_RECOVERY_INTERVAL_MS=500`,
  - preflight перед шагом выполняется через команду `url` (не `title`) с retry в пределах окна,
  - если во время шага ловится transient CDP-сбой, `buyer` повторно запускает тот же шаг и передает агенту системный маркер `[CDP_RECOVERY_RESTART_FROM_START_URL]`,
  - при таком маркере шаг должен начинаться заново с `goto start_url`,
  - после исчерпания окна восстановления шаг завершается `failed` без изменения внешнего callback-контракта.

## SberId авторизация и охват сайтов

- Охват v1: смешанный режим.
  - SberId-поддерживаемые сайты (по allowlist) используют SberId-логин.
  - Неподдерживаемые сайты используют guest-flow; при блокирующем логине включается handoff.
- Канонический владелец auth-данных: `middle`; `openclaw` является прокси до `buyer`.
- Передача auth-пакета: inline `storageState` в task payload.
- Жизненный цикл auth-пакета: только в памяти текущей сессии (`session-bound`), без постоянного хранения и reuse между сессиями.
- Сохранение browser context/storage между рестартами является отдельным открытым вопросом: до отдельного threat model и решения нельзя писать cookies, tokens, `storageState` или localStorage в долговременное хранилище.
- Ошибка формата `storageState`: `auth_failed_payload` + `ask_user` на новый пакет.
- Вход через SberId: `scripts first` → эвристический fallback → handoff.
- Опубликованные магазинные auth-скрипты SberId: `litres.ru`, `brandshop.ru`.
- Критерий auth success: редирект обратно на магазин + маркер авторизованного состояния.
- Redirect loop guard на `id.sber.ru`: максимум 2 цикла.
- Retry budget auth: 1 повтор с новым auth-пакетом.
- Для ускорения известных сценариев допускается `purchase scripts-first` после подготовки auth-контекста.
  - Охват первого шага: только `litres.ru`.
  - Успех purchase-скрипта: найден `orderId` на странице оплаты без выполнения платежа.
  - Ошибка или неуверенный результат purchase-скрипта не завершает сессию и ведет в generic `codex exec` fallback.
- В v1 не выполняются:
  - пост-логин проверка, что это ожидаемый аккаунт;
  - фильтрация доменов входящего `storageState`.

## Handoff и lifecycle скриптов

- Handoff работает по явной FSM:
  - `requested -> granted -> operator_active -> resume_requested -> resumed|aborted`.
- После handoff `buyer` продолжает сценарий в той же браузерной сессии.
- CAPTCHA решается через handoff человеком.
- Lifecycle скриптов: `draft -> review -> publish`.
- Автопубликация новых скриптов в v1 не допускается.

## Post-session анализ знаний

- Анализ знаний не находится на критическом пути покупки.
- `buyer` сначала доставляет внешний `scenario_finished` callback и только после успешной доставки запускает отдельный асинхронный `codex exec` для анализа завершенной сессии.
- Ошибка post-session анализа не меняет итоговый статус сессии покупки и не порождает внешний callback.
- Результаты анализа сохраняются как внутренние артефакты в trace-каталоге сессии:
  - `knowledge-analysis-prompt.txt`,
  - `knowledge-analysis.json`,
  - `knowledge-analysis-trace.json`.
- Все кандидаты знаний создаются в статусе `draft` и не используются следующими прогонами без review/активации.
- Для failed-сессий допускаются только pitfalls/negative knowledge; `playbook_candidate` должен быть `null`.
- Анализ знаний не должен сохранять auth-пакеты, cookies, `storageState`, токены и одноразовые платежные данные.
- Внешний callback-контракт v1 не расширяется событием `knowledge_analysis_finished`.

## Наблюдаемость, релиз и операционные ограничения

- Observability v1: только `logs + metrics`.
- Trace шага содержит агрегаты browser actions: суммарное время команд, idle между командами, количество/объем HTML и breakdown по командам.
- Политика логирования v1: разрешено логирование полного `storageState` и cookie values (осознанно принятый риск).
- Release gate v1: только `unit/integration`.
- Rollout v1: `big-bang deploy`.
- SLO в v1: не фиксируются, собираются только фактические метрики.
- Retention артефактов сессий и handoff-логов: 30 дней.

## Временные компромиссы (до Phase 2)

- `network trust only` для межсервисной безопасности.
- Отсутствие e2e release gate.
- Отсутствие жестких SLO-целей.

Эти решения считаются временными до отдельного этапа Phase 2 hardening.
