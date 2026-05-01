# Руководство по CDP tool

- Используй `python /app/tools/cdp_tool.py --endpoint "$BROWSER_CDP_ENDPOINT" <command>`.
- На первом шаге открой `start_url` через `goto --url <start_url>`, если текущая browser state еще не является нужной страницей.
- Если в memory есть системный маркер `[CDP_RECOVERY_RESTART_FROM_START_URL]`, первым действием заново открой `start_url`.
- Предпочитай `snapshot`, `links`, `exists`, `attr`, `url`, `title` перед `html`.
- Для неизвестных селекторов используй короткий timeout.
- После каждого `click`, `fill` или `press` проверяй результат через `url`, `title`, `snapshot`, `exists` или `attr`.
- Не делай вывод о недоступности CDP через `curl`, `/json/version` или DNS-проверки; проверяй браузер только через `cdp_tool.py`.
- Используй `html --path <file>` только как fallback после структурных команд, затем проверяй сохраненный файл локальным поиском.
- Не печатай полный HTML в stdout без явного escape hatch `html --full`.
- Для ограниченного текста используй `text --selector body --max-chars 2000`, не `--limit`.
