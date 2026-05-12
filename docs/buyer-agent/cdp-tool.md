# Руководство по CDP tool

- CDP доступен через shell command execution, а не как отдельный нативный tool-вызов Codex.
- Используй `python /app/tools/cdp_tool.py --endpoint "$BROWSER_CDP_ENDPOINT" <command>`.
- На первом шаге открой `start_url` через `goto --url <start_url>`, если текущая browser state еще не является нужной страницей.
- Если в memory есть системный маркер `[CDP_RECOVERY_RESTART_FROM_START_URL]`, первым действием заново открой `start_url`.
- Предпочитай `snapshot`, `links`, `exists`, `attr`, `url`, `title` перед `html`.
- Для `snapshot` используй `--limit <N>`, не `--max-chars`; `--max-chars` поддерживается у `text`, но не у `snapshot`.
- Для ожидания переходов используй `wait-url --contains <text>` или `wait-url --regex <pattern>`.
- Для ожидания DOM milestone используй `wait-selector --selector <selector>`.
- Если клик должен сразу привести к ожидаемому URL или DOM состоянию, используй `click --selector <selector> --wait-url-contains <text>` / `--wait-url-regex <pattern>` / `--wait-selector <selector>` вместо отдельного лишнего observe-step.
- Для неизвестных селекторов используй короткий timeout.
- После state-changing действий проверяй milestone/evidence через `url`, `title`, `snapshot`, `exists` или `attr`, если сам результат команды не доказывает нужное состояние.
- Не делай вывод о недоступности CDP через `curl`, `/json/version` или DNS-проверки; проверяй браузер только через `cdp_tool.py`.
- Используй `html --path <file>` только как fallback после структурных команд, затем проверяй сохраненный файл локальным поиском.
- Не печатай полный HTML в stdout без явного escape hatch `html --full`.
- Для ограниченного текста используй `text --selector body --max-chars 2000`, не `--limit`.

## OTP / SMS codes

For one-time SMS code fields, use `otp-fill` instead of plain `fill` and do not press `Enter` afterwards.

Example:

```bash
python /app/tools/cdp_tool.py --endpoint http://browser:9223 otp-fill \
  --selector 'input[autocomplete="one-time-code"]' \
  --code "$SMS_CODE" \
  --digits 4 \
  --settle-ms 4000
```

`otp-fill` normalizes the supplied value to digits, focuses the field, clears the current value, types the code character by character, and waits for evidence that the OTP prompt closed or showed an invalid-code message. It does not click resend, submit, or press `Enter`.

Use the JSON result:

- `accepted=true`: continue the checkout flow.
- `invalid_code=true`: ask the user for a new SMS code.
- `still_open=true` without `invalid_code`: stop and report that the site did not confirm the code; do not retry blindly.

