# Architecture Decisions: buyer v1

## Статус

- Документ фиксирует обязательные архитектурные решения для `buyer` v1.
- Дата фиксации: 2026-04-23.
- Все новые требования по `buyer` должны явно соответствовать решениям ниже.
- Приоритизация реализации после MVP зафиксирована в `docs/buyer-roadmap.md`.

## Протоколы и данные

- Взаимодействие `buyer` ↔ `middle`: HTTP callbacks с event envelope.
- Каноническое описание HTTP endpoints: `docs/openapi.yaml`.
- Каноническое описание callback envelope и текущих payload-схем: `docs/callbacks.openapi.yaml`.
- Обязательные поля envelope: `event_id`, `session_id`, `event_type`, `occurred_at`, `idempotency_key`, `payload`.
- Семантика доставки: `at-least-once` с дедупликацией на стороне `middle`.
- Профиль доставки callback: `timeout=10s`, `3 retries`, exponential backoff + jitter.
- При исчерпании retry-бюджета событие помечается как failed и переводится в ошибку сессии.
- Отдельные auth-события не добавляются; auth-статусы передаются через `scenario_finished` и через `ask_user` только для пользовательских действий без передачи auth-секретов.
- Канонические reason-коды auth: `auth_ok`, `auth_failed_payload`, `auth_failed_redirect_loop`, `auth_failed_invalid_session`, `auth_refresh_requested`, `auth_inline_invalid_payload`, `auth_external_unavailable`, `auth_external_timeout`, `auth_external_invalid_payload`, `auth_external_empty_payload`, `auth_external_loaded`.

## State и runtime

- Persistence и очередь выполнения: `Postgres`.
- Redis не является обязательной зависимостью v1. Distributed locks и отдельный worker-pool выносятся в будущий этап масштабирования.
- `Postgres`: долговременное состояние задач/сессий/артефактов, очередь задач, статусы ожидания пользователя и дедлайны ожидания.
- Runtime-модель v1: один `buyer` process забирает задачи из Postgres по очереди и обновляет статус сессии.
- Если сессия переходит в `waiting_user`, agent runner освобождается и `buyer` может выполнять следующую задачу.
- `waiting_user` удерживает browser slot только до дедлайна ожидания ответа. После timeout сессия завершается без resume, browser slot очищается и возвращается в пул.
- Браузерный рантайм в MVP разворачивается как пул `browser-sidecar` слотов:
  - внутри sidecar: `Chromium + Xvfb + x11vnc + noVNC`,
  - `buyer` назначает сессии свободный browser slot и подключается к нему по CDP endpoint,
  - noVNC публикуется отдельно от API-контейнера для каждого доступного browser slot,
  - размер пула задается конфигурацией; динамическое создание контейнеров допускается только с явными `min/max` лимитами.
- Лимиты рантайма по умолчанию:
  - `max_active_jobs_per_worker=1`
  - `max_browser_slots=2`
  - `waiting_user_timeout_sec=300`
  - `max_handoff_sessions=1`
  - доменные лимиты для снижения флейков и банов.
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
- Канонический владелец auth-данных: внешний auth-контур/`middle`; `openclaw` является прокси до `buyer`.
- Источники auth-пакета: inline `storageState` в task payload или внешний cookies API, включенный через конфигурацию `buyer`.
- Конфигурация external source: `SBER_AUTH_SOURCE=inline_only|external_cookies_api`, полный endpoint `SBER_COOKIES_API_URL`, `SBER_COOKIES_API_TIMEOUT_SEC`, `SBER_COOKIES_API_RETRIES`; default `inline_only`.
- Приоритет источников: inline `storageState` выше внешнего cookies API; если inline-пакет передан, внешний сервис для этой сессии не вызывается.
- Жизненный цикл auth-пакета: только в памяти текущей сессии (`session-bound`), без постоянного хранения и reuse между сессиями.
- Сохранение browser context/storage между рестартами является отдельным открытым вопросом: до отдельного threat model и решения нельзя писать cookies, tokens, `storageState` или localStorage в долговременное хранилище.
- Ошибка формата inline `storageState`: `auth_inline_invalid_payload` без запроса нового пакета у пользователя.
- Внешний cookies API читается через `GET` по полному URL из `SBER_COOKIES_API_URL`, возвращает cookies, которые `buyer` валидирует и преобразует в Playwright `storageState` с пустым `origins`; write-path остается вне ответственности `buyer`.
- Пользовательский канал `ask_user` и `/v1/replies` нельзя использовать для запроса или передачи cookies, tokens, localStorage, `storageState` или JSON auth-пакетов.
- Если auth-пакет не удалось получить из машинных источников, `buyer` фиксирует reason-code в auth summary и продолжает guest-flow; при блокирующем логине применяется handoff.
- Вход через SberId: `scripts first` → эвристический fallback → handoff.
- Опубликованные магазинные auth-скрипты SberId: `litres.ru`, `brandshop.ru`.
- Критерий auth success: редирект обратно на магазин + маркер авторизованного состояния.
- Redirect loop guard на `id.sber.ru`: максимум 2 цикла.
- Retry budget auth-скрипта по умолчанию: 1 повтор; новый auth-пакет не запрашивается через пользовательский reply.
- Для ускорения известных сценариев допускается `purchase scripts-first` после подготовки auth-контекста.
  - Охват первого шага: только `litres.ru`.
  - Успех purchase-скрипта: найден `orderId` на странице SberPay без выполнения платежа.
  - SberPay не взаимозаменяем с СБП, Системой быстрых платежей, SBP или FPS; такие способы оплаты не считаются успешным платежным шагом.
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

## Eval и LLM-as-a-Judge

- Статус решения: принято, дата фиксации — 2026-04-28.
- Eval-контур является learning loop для улучшения `buyer`, а не release gate для других релизов.
- Eval не меняет поведение `buyer`: `buyer` получает обычный task payload и нейтральную metadata без специальных `is_eval` веток.
- Eval выполняется отдельным контейнером `eval_service`, который оркестрирует batch-run, принимает callbacks от `buyer`, обрабатывает `ask_user`, запускает judge и отдает API для eval-таба в `micro-ui`.
- Источник ожиданий для judge — версионируемые case-файлы `eval/cases/*.yaml`, а не догадки из trace.
- MVP запускает cases вручную из `micro-ui` и выполняет их последовательно. Автоматический judge после каждой обычной сессии не включается.
- `payment_ready` считается терминальным успешным состоянием eval-case; после него `eval_service` ждет короткий grace period 5 секунд для дозаписи trace/events.
- Timeout eval-case по умолчанию — 600 секунд; timeout/partial trace также оценивается judge.
- Перед LLM Judge всегда строится redacted `judge-input.json`. Raw trace не передается в LLM.
- Redaction удаляет auth-пакеты, cookies, `storageState`, токены, `orderId`, одноразовые payment URLs и другие платежные секреты.
- Judge backend MVP — `codex exec --output-schema` с моделью из `EVAL_JUDGE_MODEL`.
- Judge возвращает strict `evaluation.json` с checks `ok/not_ok/skipped`, числовыми `duration_ms` и `buyer_tokens_used`, evidence refs и draft-рекомендациями.
- Draft-рекомендации judge могут относиться к prompt, playbook, site profile, script candidate или eval case, но не применяются автоматически.
- Baseline времени и токенов считается детерминированно как медиана последних N успешных evaluations по `eval_case_id`; trend-judge по истории выносится в future work.

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
