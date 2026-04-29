# Stop Purchase Session Design

> **Статус:** утвержденный дизайн.

## Цель

Добавить возможность оператору остановить уже запущенную покупочную сессию, чтобы освободить runtime-слот и прекратить дальнейшие действия `buyer` в браузере.

## Текущее поведение

`buyer` запускает сессию через `POST /v1/tasks`, хранит runtime `task_ref` и завершает сценарий только через `completed` или `failed`. Публичного endpoint-а для ручной остановки нет. `micro-ui` умеет запускать сессию и отправлять reply, но не умеет останавливать активный сценарий.

## Целевое поведение

Остановка доступна двумя каналами:

- HTTP API `buyer`: `POST /v1/sessions/{session_id}/stop`.
- Операторский UI: кнопка остановки в `micro-ui`, которая вызывает proxy endpoint `micro-ui` и дальше передает запрос в `buyer`.

Остановка трактуется как штатная неуспешная финализация, а не как удаление сессии. Для активной сессии (`created`, `running`, `waiting_user`) сервис:

- доставляет callback `scenario_finished`;
- передает в payload `status: failed`, `reason_code: session_stopped_by_operator`, сообщение и артефакт с причиной остановки;
- переводит сессию в `failed` и записывает `last_error`;
- отменяет runtime `task_ref`;
- будит `waiting_user`, если сессия ждала reply;
- освобождает active-session slot.

Для уже терминальной сессии (`completed`, `failed`) запрос идемпотентен: состояние не меняется, ответ содержит `accepted: false` и текущий статус.

## Контракт API

Request body опционален:

```json
{
  "reason": "Оператор остановил сценарий"
}
```

Response:

```json
{
  "session_id": "<uuid>",
  "accepted": true,
  "status": "failed"
}
```

Ошибки:

- `404`: сессия не найдена.
- `422`: невалидный request body.

## Callback

Новый тип события не вводится. Получатели уже умеют обрабатывать `scenario_finished`, поэтому остановка приходит как финальное событие:

```json
{
  "status": "failed",
  "message": "Сессия остановлена оператором.",
  "reason_code": "session_stopped_by_operator",
  "artifacts": {
    "stop_reason": "Оператор остановил сценарий"
  }
}
```

## Micro-UI

`micro-ui` добавляет backend proxy endpoint `POST /api/sessions/{session_id}/stop`. Frontend показывает кнопку остановки только для активных статусов `created`, `running`, `waiting_user`. После успешного запроса UI перечитывает сессии и события обычным механизмом обновления.

## Тестирование

- Сервисный тест: остановка `running` сессии отправляет `scenario_finished`, переводит статус в `failed` и освобождает слот.
- Сервисный тест: остановка `waiting_user` будит ожидающий runner и не зависает.
- API-тест `buyer`: endpoint возвращает `404` для неизвестной сессии и корректный response для активной.
- Тест `micro-ui`: proxy вызывает buyer stop endpoint и пробрасывает ответ.

## Не входит

- Новый статус `stopped`/`cancelled`.
- Удаление persistent-сессии.
- Гарантированная очистка корзины или браузерного профиля на стороне магазина.
- Отдельный callback event `session_stopped`.

## Самопроверка

- Scope ограничен остановкой текущей runtime-сессии.
- Контракт не расширяет enum статусов и не ломает существующий lifecycle `completed|failed`.
- Callback остается совместимым с текущим `scenario_finished`.
- UI и API покрывают один и тот же backend-контракт.
