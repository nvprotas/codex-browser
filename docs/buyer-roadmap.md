# Buyer Roadmap

## Статус документа

- Дата создания: 2026-04-25.
- Назначение: приоритизированный roadmap развития `buyer` после MVP.
- Источник решений: `docs/architecture-decisions.md`.
- Roadmap не заменяет v1-спецификацию. Если новая задача меняет архитектурные решения, сначала обновляется `docs/architecture-decisions.md`, затем этот документ и остальные пользовательские документы.

## Контекст

`buyer` — не самостоятельный пользовательский продукт. Это агент покупки, которым будут пользоваться другие персональные ассистенты, в первую очередь `openclaw`. Поэтому ценность roadmap оценивается не только по автономности браузерного сценария, но и по качеству агентного интерфейса:

- предсказуемый task lifecycle для вызывающего агента;
- долговременное состояние, которое не теряется при рестарте;
- понятные machine-readable события, статусы и ошибки;
- безопасная граница оплаты: `buyer` доводит сценарий до SberPay и возвращает `orderId`, но не подтверждает платеж;
- возможность операторского и пользовательского handoff без потери браузерной сессии;
- накопление знаний о магазинах только через review/activation, без скрытого автоприменения.

## Шкала приоритизации

- `Value`: 1-5, где 5 — максимальный вклад в пригодность `buyer` как надежного агента для `openclaw`.
- `Effort`: 1-5, где 5 — крупная архитектурная работа.
- `V/E`: грубый показатель очередности. Фундаментальные задачи могут стоять выше, даже если их ratio ниже.

## Связь С Linear

Задачи roadmap ведутся в Linear-проекте `Ratatouille`, команда `Monaco`. При изменении roadmap нужно синхронно обновлять соответствующие Linear issue: заголовок, описание, статус, зависимости, оценку и приоритет.

| Roadmap | Linear | Название |
| --- | --- | --- |
| 1 | [MON-12](https://linear.app/monaco-dev/issue/MON-12/buyer-phase-1-persistent-state-na-postgres-dlya-zadach-sessij-sobytij) | Persistent state: Postgres для задач, сессий, событий и артефактов |
| 2 | [MON-13](https://linear.app/monaco-dev/issue/MON-13/buyer-phase-1-redis-locks-i-runtime-markers) | Postgres task queue и browser-slot runtime |
| 2.1 | [MON-27](https://linear.app/monaco-dev/issue/MON-27/buyer-phase-1-issledovanie-persistence-brauzernogo-sostoyaniya-mezhdu) | Исследование persistence браузерного состояния между рестартами |
| 3 | [MON-14](https://linear.app/monaco-dev/issue/MON-14/buyer-phase-1-handoff-fsm) | Handoff FSM |
| 4 | [MON-15](https://linear.app/monaco-dev/issue/MON-15/buyer-phase-1-api-upravleniya-lifecycle-sessii) | API управления lifecycle: pause, resume, abort, operator command |
| 5 | [MON-16](https://linear.app/monaco-dev/issue/MON-16/buyer-phase-1-artifact-i-trace-manifest) | Artifact и trace manifest |
| 6 | [MON-17](https://linear.app/monaco-dev/issue/MON-17/buyer-phase-2-reviewactivation-flow-dlya-knowledge-analysis) | Review/activation flow для knowledge-analysis |
| 7 | [MON-18](https://linear.app/monaco-dev/issue/MON-18/buyer-phase-2-lifecycle-dlya-scriptplaybook-candidates) | Lifecycle для script/playbook candidates |
| 8 | [MON-20](https://linear.app/monaco-dev/issue/MON-20/buyer-phase-2-minimalnyj-verifier-dlya-payment-ready-i-purchase-script) | Минимальный verifier для payment_ready и purchase-script result |
| 9 | [MON-21](https://linear.app/monaco-dev/issue/MON-21/buyer-phase-2-formalnaya-model-task-step-attempt) | Формальная модель Task -> Step -> Attempt |
| 10 | [MON-19](https://linear.app/monaco-dev/issue/MON-19/buyer-phase-2-buyer-worker-separation) | Buyer worker separation |
| 11 | [MON-22](https://linear.app/monaco-dev/issue/MON-22/buyer-phase-3-policy-layer-dlya-opasnyh-dejstvij) | Policy layer для опасных действий |
| 12 | [MON-23](https://linear.app/monaco-dev/issue/MON-23/buyer-phase-3-site-profiles) | Site profiles |
| 13 | [MON-24](https://linear.app/monaco-dev/issue/MON-24/buyer-phase-3-strategy-ranking-i-ab-metrics) | Strategy ranking и A/B metrics |
| 14 | [MON-25](https://linear.app/monaco-dev/issue/MON-25/buyer-phase-3-whitelist-dsl-vmesto-svobodnogo-generic-browser-flow) | Whitelist DSL вместо свободного generic browser flow |
| 15 | [MON-26](https://linear.app/monaco-dev/issue/MON-26/buyer-phase-3-vision-challenge) | Vision challenge |

## Roadmap По Фазам

### Phase 1 — Надежный Runtime И Контракт Для Агентов

Цель фазы: убрать ключевые ограничения MVP: in-memory state, один активный сценарий, неполный lifecycle handoff и слабая операционная наблюдаемость.

#### 1. Persistent state: Postgres для задач, сессий, событий и артефактов

**Value:** 5  
**Effort:** 4  
**V/E:** 1.25  
**Статус:** planned  
**Зависимости:** нет.

**Что сделать:**

- Добавить Postgres в `docker-compose.yml` и конфигурацию `buyer`.
- Ввести миграции для таблиц:
  - `buyer_sessions`: session id, task, start_url, callback_url, status, timestamps, last_error, metadata;
  - `buyer_events`: callback envelope, idempotency key, delivery status;
  - `buyer_artifacts`: ссылки на trace-файлы, browser-actions, script outputs, screenshots и knowledge-analysis artifacts;
  - `buyer_replies`: ожидаемые и полученные ответы пользователя;
  - `buyer_auth_context`: только runtime-метаданные auth flow без постоянного хранения `storageState`.
- Перевести `SessionStore` с in-memory словаря на repository-слой поверх Postgres.
- Сохранить текущий внешний API `/v1/tasks`, `/v1/sessions`, `/v1/replies` без ломающих изменений.
- Добавить тесты на восстановление статуса после пересоздания service/store.

**Value для `openclaw`:**

`openclaw` сможет надежно отслеживать задачу после рестарта контейнера, сетевого сбоя или переподключения. Это превращает `buyer` из локального процесса в сервис с долговременным контрактом: задача имеет устойчивый id, статус и историю событий.

#### 2. Postgres task queue и browser-slot runtime

**Value:** 4
**Effort:** 3
**V/E:** 1.33
**Статус:** implemented
**Зависимости:** задача 1.

**Что сделать:**

- Перевести запуск задач на Postgres-очередь: `POST /v1/tasks` создает сессию в `queued`, worker забирает следующую доступную задачу и переводит ее в `running`.
- Добавить атомарный claim задачи в Postgres без Redis: один `buyer` process является целевым режимом MVP, но claim должен защищать от случайного двойного запуска.
- Ввести runtime manager browser slots:
  - сессия арендует свободный browser slot на время `running`;
  - при `waiting_user` browser slot удерживается до дедлайна, а agent runner освобождается;
  - после `completed`, `failed` или timeout browser slot очищается и возвращается в пул.
- Добавить конфигурируемые лимиты:
  - `max_active_jobs_per_worker`;
  - `min_browser_slots`;
  - `max_browser_slots`;
  - `waiting_user_timeout_sec`;
  - `max_handoff_sessions`;
  - доменные лимиты для одного магазина.
- Реализовать timeout ожидания пользователя: если ответ не пришел за `waiting_user_timeout_sec`, сессия завершается без resume, browser slot освобождается, поздний reply получает machine-readable отказ.
- Поддержать динамическое количество browser slots в пределах `min/max`. Для MVP предпочтителен статический compose-пул с динамической арендой слотов; Docker-managed autoscale допускается отдельным follow-up после оценки безопасности Docker socket.

**Реализация 2026-04-28:**

- Очередь реализована в Postgres через статус `queued` и атомарный claim `FOR UPDATE SKIP LOCKED`.
- Runtime slots задаются статическим compose-пулом `browser-1`/`browser-2` и `BROWSER_SLOTS_JSON`.
- `waiting_user` освобождает agent job capacity, удерживает browser slot до `WAITING_USER_TIMEOUT_SEC` и возвращает поздним reply `reason_code=waiting_user_timeout`.
- Runtime markers активных сессий после restart завершаются понятной ошибкой без восстановления browser page или auth state.

**Value для `openclaw`:**

Вызывающий агент сможет ставить несколько задач в очередь и не блокироваться на `waiting_user`: `buyer` будет выполнять следующую задачу, пока предыдущая ожидает ответ с ограниченным TTL. Инфраструктура остается проще, потому что Redis не нужен для персонального single-worker MVP.

#### 2.1. Исследование persistence браузерного состояния между рестартами

**Value:** 4
**Effort:** 3
**V/E:** 1.33
**Статус:** planned
**Зависимости:** задачи 1-2.

**Что сделать:**

- Проанализировать, можно ли безопасно сохранять Playwright browser context/storage между рестартами `buyer` и `browser`.
- Разделить техническое состояние браузера и чувствительные auth-данные: cookies, tokens, `storageState`, localStorage.
- Оценить варианты шифрования, TTL, привязки к сессии и ручного сброса состояния.
- Зафиксировать, какие части browser state можно восстанавливать автоматически, а какие требуют повторного auth/handoff.
- Подготовить threat model и критерии включения в production.

**Value для `openclaw`:**

Если сохранение браузерного состояния окажется безопасным, `openclaw` сможет продолжать часть нестабильных сценариев после рестарта без повторного прохождения логина и выбора контекста магазина. До отдельного решения текущая Postgres-задача не сохраняет browser cookies/tokens/storageState.

#### 3. Handoff FSM

**Value:** 5  
**Effort:** 3  
**V/E:** 1.67  
**Статус:** planned  
**Зависимости:** задачи 1-2 желательны, но минимальная версия возможна раньше.

**Что сделать:**

- Реализовать явную FSM handoff:
  - `requested`;
  - `granted`;
  - `operator_active`;
  - `resume_requested`;
  - `resumed`;
  - `aborted`.
- Хранить состояние handoff отдельно от общего статуса сессии.
- Добавить события:
  - `handoff_requested`;
  - `handoff_granted`;
  - `handoff_operator_active`;
  - `handoff_resume_requested`;
  - `handoff_resumed`;
  - `handoff_aborted`.
- Сохранить совместимость с текущими событиями `handoff_requested` и `handoff_resumed`.
- Логировать действия человека во время handoff: координаты, селектор под кликом, URL, timestamp.
- После resume продолжать работу в той же браузерной сессии.

**Value для `openclaw`:**

`openclaw` получит понятную модель передачи управления человеку. Это важно для CAPTCHA, нестабильных checkout-потоков и ситуаций, где ассистент должен объяснить пользователю, что именно требуется сделать.

#### 4. API управления lifecycle: pause, resume, abort, operator command

**Value:** 4  
**Effort:** 2  
**V/E:** 2.00  
**Статус:** planned  
**Зависимости:** задача 3 для полной версии.

**Что сделать:**

- Добавить endpoints:
  - `POST /v1/sessions/{session_id}/pause`;
  - `POST /v1/sessions/{session_id}/resume`;
  - `POST /v1/sessions/{session_id}/abort`;
  - `POST /v1/sessions/{session_id}/operator-command`.
- Для каждого действия возвращать machine-readable результат: `accepted`, `session_status`, `reason_code`.
- Поддержать operator command как входной канал для ручных указаний: “не выбирай замену”, “продолжи после ручного логина”, “останови сценарий”.
- Пробрасывать lifecycle-события в callback envelope.
- Добавить конфликтные проверки: нельзя resume завершенную сессию, нельзя abort уже завершенную сессию, нельзя pause в `waiting_user` без reason.

**Value для `openclaw`:**

Персональный ассистент сможет управлять `buyer` как долгоживущим инструментом, а не как одноразовым HTTP-запуском. Это снижает риск зависших сценариев и дает вызывающему агенту контроль над пользовательским опытом.

#### 5. Artifact и trace manifest

**Value:** 4  
**Effort:** 2  
**V/E:** 2.00  
**Статус:** planned  
**Зависимости:** задача 1 желательна.

**Что сделать:**

- Для каждой сессии формировать `manifest.json` в trace-каталоге.
- В manifest включать:
  - prompt-файлы;
  - step trace JSON;
  - browser-actions JSONL;
  - script outputs;
  - screenshots;
  - knowledge-analysis artifacts;
  - размеры файлов и timestamps.
- Добавить `GET /v1/sessions/{session_id}/artifacts`.
- В micro-ui показывать список артефактов с типом, временем и кратким summary.
- Реализовать retention 30 дней для session artifacts и handoff logs.

**Value для `openclaw`:**

Если задача завершилась ошибкой, `openclaw` сможет получить структурированную диагностику и решить: повторить, уточнить у пользователя, передать человеку или создать follow-up задачу.

### Phase 2 — Качество Результата И Обучение Через Review

Цель фазы: повысить надежность прохождения магазинов без скрытого автоприменения знаний.

#### 6. Review/activation flow для knowledge-analysis

**Value:** 4  
**Effort:** 3  
**V/E:** 1.33  
**Статус:** planned  
**Зависимости:** задача 5.

**Что сделать:**

- Ввести локальное хранилище knowledge drafts:
  - `navigation_hints`;
  - `pitfalls`;
  - `site_overview_plain`;
  - `negative_knowledge`;
  - `playbook_candidate`.
- Добавить статусы:
  - `draft`;
  - `reviewed`;
  - `active`;
  - `rejected`;
  - `archived`.
- Добавить API:
  - список draft-кандидатов;
  - просмотр evidence refs;
  - approve/reject;
  - activation для конкретного домена.
- При следующем запуске использовать только `active` знания.
- Запретить wildcard-активацию для domain-specific знаний.
- Сохранять audit trail: кто активировал, когда, по какой evidence.

**Value для `openclaw`:**

`buyer` начнет улучшаться от сессии к сессии, но без неожиданного поведения. `openclaw` сможет доверять, что новые знания не применяются автоматически после одного флейкового прогона.

#### 7. Lifecycle для script/playbook candidates

**Value:** 4  
**Effort:** 3  
**V/E:** 1.33  
**Статус:** planned  
**Зависимости:** задача 6.

**Что сделать:**

- Формализовать lifecycle сценариев:
  - `draft`;
  - `review`;
  - `publish`;
  - `archived`.
- Хранить candidate metadata:
  - домен;
  - источник сессии;
  - evidence refs;
  - ожидаемый входной контракт;
  - ожидаемый выходной контракт;
  - ограничения безопасности.
- Добавить dry-run проверку candidate script/playbook против сохраненных trace/evidence.
- Запретить автопубликацию в v1.
- При публикации добавлять сценарий в registry allowlist только явно.

**Value для `openclaw`:**

Для часто используемых магазинов `buyer` сможет переходить от generic flow к быстрым и стабильным сценариям. При этом `openclaw` не столкнется с внезапным использованием непроверенного скрипта.

#### 8. Минимальный verifier для payment_ready и purchase-script result

**Value:** 5  
**Effort:** 3  
**V/E:** 1.67  
**Статус:** needs architecture decision  
**Зависимости:** задача 5.

**Что сделать:**

- Добавить architecture decision: входит ли formal verifier в v1 или Phase 2.
- Начать не с общего verifier всех шагов, а с узкой проверки финальных результатов:
  - `orderId` найден;
  - текущая страница относится к checkout/payment flow;
  - финальная кнопка оплаты не нажималась;
  - SberPay выбран или доступен;
  - артефакты не содержат одноразовые платежные секреты.
- Для purchase scripts требовать structured verification block в output.
- При неуспешной верификации не отправлять `payment_ready`, а продолжать generic flow или просить handoff.
- Покрыть тестами false-positive сценарии: найден похожий id не на платежной странице, страница корзины без checkout, ошибка оплаты, order token в URL.

**Value для `openclaw`:**

Главная ценность — доверие к финальному результату. `openclaw` не должен показывать пользователю платежный шаг, если `buyer` только “думает”, что дошел до оплаты.

#### 9. Формальная модель Task -> Step -> Attempt

**Value:** 4  
**Effort:** 4  
**V/E:** 1.00  
**Статус:** planned после persistence  
**Зависимости:** задачи 1 и 5.

**Что сделать:**

- Разделить сессию на шаги:
  - auth preparation;
  - search/open product;
  - add to cart;
  - checkout;
  - SberPay/payment readiness;
  - handoff;
  - post-session analysis.
- Для каждого шага хранить attempts с:
  - input context;
  - runner type: `script`, `codex`, `handoff`;
  - result;
  - artifacts;
  - verification status;
  - retry reason.
- Сохранить внешний callback-контракт, но сделать payload богаче за счет step metadata.
- Добавить `GET /v1/sessions/{session_id}/steps`.

**Value для `openclaw`:**

Вызывающий агент сможет понимать, где именно находится покупка: авторизация, корзина, checkout, ожидание пользователя или финальный платежный шаг. Это улучшает диалог с пользователем и retry-логику.

#### 10. Buyer worker separation

**Value:** 4  
**Effort:** 4  
**V/E:** 1.00  
**Статус:** planned после persistence и Postgres task queue  
**Зависимости:** задачи 1-2.

**Что сделать:**

- Вынести выполнение сессий из FastAPI process в отдельный `buyer-worker`.
- API должен только создавать задачу, возвращать статус и принимать команды.
- Worker должен poll-ить или атомарно забирать задачи через Postgres queue/claim.
- Поддержать graceful shutdown: текущий step завершается или помечается recoverable.
- Добавить heartbeat worker-а и diagnostics в session status.
- Подготовить возможность нескольких worker-ов без двойного выполнения одной сессии; если появится реальный worker-pool, отдельно оценить Redis или Postgres advisory locks.

**Value для `openclaw`:**

`openclaw` получит стабильный API, который не блокируется долгими браузерными сценариями. Это основа для production-like эксплуатации и параллельных покупок.

### Phase 3 — Guardrails, Domain Awareness И Масштабирование

Цель фазы: расширять автономность только после появления надежного состояния, handoff, verification и review loops.

#### 11. Policy layer для опасных действий

**Value:** 3  
**Effort:** 3  
**V/E:** 1.00  
**Статус:** planned после verifier  
**Зависимости:** задачи 8-9.

**Что сделать:**

- Формализовать запрещенные и требующие подтверждения действия:
  - финальное подтверждение оплаты;
  - изменение адреса доставки;
  - выбор замены выше бюджета;
  - подписки, автопродление, платные опции;
  - сохранение платежных данных.
- Добавить reason codes для policy decisions.
- Возвращать policy blocks в `ask_user` или `handoff_requested`.
- Логировать policy decision в artifacts/status.

**Value для `openclaw`:**

Персональный ассистент сможет объяснять пользователю, почему покупка остановлена или требует подтверждения. Это снижает риск нежелательных действий и повышает доверие к агенту.

#### 12. Site profiles

**Value:** 3  
**Effort:** 4  
**V/E:** 0.75  
**Статус:** phase 3  
**Зависимости:** задачи 6-9.

**Что сделать:**

- Ввести domain-specific profile:
  - known selectors;
  - navigation hints;
  - checkout entry points;
  - SberPay markers;
  - known pitfalls.
- Загружать active profile в prompt/script context.
- Не использовать wildcard profile для записи новых знаний.
- Добавить ручное редактирование profile через internal UI/API.

**Value для `openclaw`:**

`buyer` сможет вести себя стабильно на повторяющихся магазинах и давать вызывающему агенту более точные причины ошибок: “не найден SberPay”, “магазин требует логин”, “нужен ручной выбор адреса”.

#### 13. Strategy ranking и A/B metrics

**Value:** 3  
**Effort:** 5  
**V/E:** 0.60  
**Статус:** phase 3  
**Зависимости:** задачи 9 и 12.

**Что сделать:**

- Хранить несколько стратегий для одного step/domain.
- Собирать метрики:
  - success rate;
  - latency;
  - retry count;
  - handoff rate;
  - verifier fail reasons.
- Выбирать стратегию по score, а не по ручному порядку.
- Добавить A/B bucket только для безопасных non-payment шагов.

**Value для `openclaw`:**

`buyer` со временем будет выбирать более надежный путь на конкретном магазине. Для ассистента это означает меньше повторных вопросов пользователю и меньше handoff.

#### 14. Whitelist DSL вместо свободного generic browser flow

**Value:** 3  
**Effort:** 5  
**V/E:** 0.60  
**Статус:** requires separate architecture decision  
**Зависимости:** задачи 8-12.

**Что сделать:**

- Описать ограниченный набор browser actions:
  - `goto`;
  - `click`;
  - `fill`;
  - `press`;
  - `wait_for`;
  - `select_option`;
  - `scroll`;
  - `extract_text`;
  - `screenshot`.
- Валидировать действия до исполнения.
- Запретить произвольный код в generic path.
- Сохранять action batches и execution trace.
- Постепенно перевести scripts/playbooks на DSL там, где это дешевле и безопаснее TypeScript.

**Value для `openclaw`:**

DSL делает поведение `buyer` более объяснимым и воспроизводимым. Но это крупный архитектурный поворот, поэтому он не должен блокировать v1 runtime-hardening.

#### 15. Vision challenge

**Value:** 2  
**Effort:** 4  
**V/E:** 0.50  
**Статус:** phase 3+  
**Зависимости:** задачи 5, 8 и 9.

**Что сделать:**

- Сохранять viewport screenshot после значимых шагов.
- Запускать multimodal check только на ограниченных местах:
  - финальная проверка checkout/payment page;
  - verifier disagreement;
  - handoff diagnostics.
- Не использовать vision как единственный критерий успеха.
- Сохранять vision output как diagnostic artifact.

**Value для `openclaw`:**

Vision даст второе мнение в сложных UI-ситуациях, но не заменит структурную проверку DOM/URL/action trace. Для ассистента это полезно как объяснение ошибок и дополнительная диагностика.

## Задачи, Которые Не Нужно Делать Сейчас

- Полный перенос архитектуры внешнего `agent-buyer-main`.
- Автопубликация новых скриптов или знаний без review.
- Полный policy engine до появления verifier и step/attempt модели.
- Whitelist DSL как обязательная замена `codex exec` в ближайшей фазе.
- Vision-first подход к верификации.
- A/B ranking до накопления нормализованных attempts.

## Рекомендуемый Порядок Реализации

1. Persistent state: Postgres.
2. Postgres task queue и browser-slot runtime.
3. Handoff FSM.
4. Lifecycle API: pause/resume/abort/operator command.
5. Artifact/trace manifest.
6. Knowledge review/activation.
7. Script/playbook candidate lifecycle.
8. Минимальный verifier для `payment_ready`.
9. Task -> Step -> Attempt.
10. Buyer-worker separation.
11. Policy layer.
12. Site profiles.
13. Strategy ranking.
14. Whitelist DSL.
15. Vision challenge.

Такой порядок сохраняет текущую направленность проекта: `buyer` остается агентом покупки для `openclaw`, а не превращается в отдельную e-commerce платформу раньше времени.
