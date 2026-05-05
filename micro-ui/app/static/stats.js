(() => {
  const STATS_SERVICE_PATH = '/api/eval/stats/sessions';
  const CDP_INFO = {
    url: { type: 'read', desc: 'Проверяет, на каком адресе сейчас открыт браузер.', when: 'После переходов, кликов и оплаты, чтобы понять, куда попал агент.', risk: 'Очень дешевая проверка, страницу не меняет.' },
    title: { type: 'read', desc: 'Читает заголовок вкладки браузера.', when: 'Быстрая проверка, что открыта ожидаемая страница или магазин.', risk: 'Очень дешевая проверка, страницу не меняет.' },
    goto: { type: 'navigation', desc: 'Переводит браузер на указанный URL.', when: 'Старт сценария, recovery после тупика или явный переход на найденную страницу.', risk: 'Меняет страницу и может сбросить текущий контекст.' },
    click: { type: 'write', desc: 'Нажимает найденную кнопку, ссылку, чекбокс или другой элемент страницы.', when: 'Выбор товара, добавление в корзину, переход к checkout, выбор доставки или оплаты.', risk: 'Меняет состояние страницы; после клика нужен контрольный признак результата.' },
    fill: { type: 'write', desc: 'Вводит текст в поле формы.', when: 'Поиск, имя, телефон, email, адрес или другой checkout input.', risk: 'Может вводить пользовательские данные и менять форму.' },
    press: { type: 'write', desc: 'Нажимает клавишу в текущем фокусе браузера.', when: 'Enter для поиска/submit, Escape для закрытия окна, Tab для перехода между полями.', risk: 'Результат зависит от того, какой элемент был в фокусе.' },
    wait: { type: 'wait', desc: 'Просто ждет N миллисекунд без проверки состояния страницы.', when: 'Последний fallback при анимациях, нестабильной загрузке или внешних виджетах.', risk: 'Тратит время и не доказывает, что нужное состояние наступило.' },
    'wait-url': { type: 'wait', desc: 'Ждет, пока адрес страницы совпадет с ожидаемой строкой или регулярным выражением.', when: 'После клика, submit или редиректа, если успех виден по URL.', risk: 'Может ждать до timeout, если сайт остался на другой странице.' },
    'wait-selector': { type: 'wait', desc: 'Ждет появления конкретного элемента на странице.', when: 'После действия, когда результат должен быть виден как кнопка, форма, сообщение или блок checkout.', risk: 'Зависит от точности селектора; неверный селектор приведет к timeout.' },
    snapshot: { type: 'read', desc: 'Делает DOM-снимок страницы: показывает агенту видимые кнопки, ссылки, поля ввода, заголовки и тексты, по которым он выбирает следующий шаг.', when: 'Основной способ понять, что сейчас видно на странице, без выгрузки всего HTML.', risk: 'Дороже простых read-проверок, но обычно полезнее и чище полного HTML.' },
    links: { type: 'read', desc: 'Собирает ссылки из выбранной части страницы.', when: 'Поиск кандидатов для перехода: категории, карточки товара, checkout, help/payment links.', risk: 'Страницу не меняет, но список может быть шумным.' },
    text: { type: 'read', desc: 'Читает видимый текст выбранного блока страницы.', when: 'Проверка цены, названия товара, ошибки формы, условий доставки или платежного шага.', risk: 'Страницу не меняет; слишком широкий блок даст много шума.' },
    exists: { type: 'read', desc: 'Проверяет, есть ли на странице конкретный элемент.', when: 'Быстро подтвердить milestone: корзина открыта, кнопка SberPay видна, ошибка появилась.', risk: 'Дешевая проверка, но не объясняет содержимое элемента.' },
    attr: { type: 'read', desc: 'Читает атрибут элемента: href, value, disabled, aria-label и похожие признаки.', when: 'Проверка ссылки, состояния кнопки, заполненного значения или скрытой подсказки.', risk: 'Страницу не меняет; полезность зависит от выбранного атрибута.' },
    screenshot: { type: 'evidence', desc: 'Сохраняет картинку текущего экрана браузера.', when: 'Нужен визуальный артефакт для debug, judge или проверки платежного экрана.', risk: 'Дороже read-команд и пишет файл с артефактом.' },
    html: { type: 'heavy', desc: 'Выгружает HTML страницы или выбранного блока.', when: 'Fallback, когда snapshot/text/links/attr не дают нужной информации.', risk: 'Самая шумная команда: много данных, медленнее читать и сложнее анализировать.' },
  };
  const GANTT_CATEGORIES = [
    { category: 'runtime', label: 'runtime/idle' },
    { category: 'read', label: 'read' },
    { category: 'write', label: 'write' },
    { category: 'navigation', label: 'navigation' },
    { category: 'wait', label: 'wait' },
    { category: 'evidence', label: 'evidence' },
    { category: 'heavy', label: 'heavy' },
    { category: 'error', label: 'error' },
  ];

  const panel = document.getElementById('stats-tab-panel');
  if (!panel) {
    return;
  }

  const state = {
    sessions: [],
    warnings: [],
    selectedSessionId: null,
    sortKey: 'start_ts',
    sortDir: 'desc',
    filterSource: 'all',
    filterStatus: 'all',
    search: '',
    loading: false,
    error: null,
    loadedOnce: false,
    ganttTooltipEl: null,
    cmdTooltipEl: null,
  };

  function q(selector) {
    return panel.querySelector(selector);
  }

  function qa(selector) {
    return panel.querySelectorAll(selector);
  }

  function node(tag, className, textValue) {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (textValue !== undefined) {
      element.textContent = textValue;
    }
    return element;
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function asObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function number(value) {
    const parsed = Number(value || 0);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  }

  function fmtMs(value) {
    const ms = number(value);
    if (!ms) {
      return '—';
    }
    if (ms >= 60000) {
      return `${(ms / 60000).toFixed(1)}m`;
    }
    return `${Math.round(ms / 1000)}s`;
  }

  function fmtNum(value) {
    const parsed = number(value);
    if (!parsed) {
      return '—';
    }
    if (parsed >= 1000000) {
      return `${(parsed / 1000000).toFixed(1)}M`;
    }
    if (parsed >= 1000) {
      return `${(parsed / 1000).toFixed(1)}K`;
    }
    return String(Math.round(parsed));
  }

  function fmtTs(value) {
    if (!value) {
      return '—';
    }
    const date = new Date(Number(value));
    if (Number.isNaN(date.getTime())) {
      return '—';
    }
    return date.toLocaleString('ru-RU', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function formatTimelineTs(value, isAbsolute) {
    if (!isAbsolute) {
      return `+${fmtMs(value)}`;
    }
    const date = new Date(Number(value));
    if (Number.isNaN(date.getTime())) {
      return '—';
    }
    return date.toLocaleString('ru-RU', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  function shortId(value, max = 16) {
    const text = String(value || '');
    if (!text) {
      return '—';
    }
    if (text.length <= max) {
      return text;
    }
    const side = Math.floor((max - 1) / 2);
    return `${text.slice(0, side)}…${text.slice(-side)}`;
  }

  function median(values) {
    const sorted = values.map(number).filter(Boolean).sort((left, right) => left - right);
    if (!sorted.length) {
      return 0;
    }
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 === 0 ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle];
  }

  async function fetchJson(url) {
    const response = await fetch(url);
    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = { raw: text };
    }
    if (!response.ok) {
      const detail = body?.detail || body?.raw || `HTTP ${response.status}`;
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return body;
  }

  function normalizeBreakdown(value) {
    const result = {};
    for (const [command, rawStats] of Object.entries(asObject(value))) {
      if (!command) {
        continue;
      }
      if (rawStats && typeof rawStats === 'object') {
        result[command] = {
          count: number(rawStats.count),
          duration_ms: number(rawStats.duration_ms),
          errors: number(rawStats.errors),
        };
      } else {
        result[command] = { count: number(rawStats), duration_ms: 0, errors: 0 };
      }
    }
    return result;
  }

  function normalizeTimeline(value) {
    return asArray(value)
      .map((item) => {
        const command = String(item?.command || '');
        if (!command) {
          return null;
        }
        const offsetMs = number(item.offset_ms);
        const durationMs = number(item.duration_ms);
        const endOffsetMs = number(item.end_offset_ms) || offsetMs + durationMs;
        const startTs = number(item.start_ts);
        const endTs = number(item.end_ts) || (startTs ? startTs + durationMs : 0);
        return {
          command,
          event: String(item.event || ''),
          ok: item.ok !== false,
          duration_ms: durationMs,
          start_ts: startTs,
          end_ts: endTs,
          offset_ms: offsetMs,
          end_offset_ms: Math.max(endOffsetMs, offsetMs + durationMs),
          sequence: number(item.sequence),
          attempt_id: String(item.attempt_id || ''),
        };
      })
      .filter(Boolean);
  }

  function normalizeSessions(sessions) {
    return asArray(sessions).map((session) => {
      let stepOffset = 0;
      const steps = asArray(session.steps).map((step) => {
        const breakdown = normalizeBreakdown(step.command_breakdown);
        const totalFromBreakdown = Object.values(breakdown).reduce((sum, item) => sum + number(item.count), 0);
        const durationMs = number(step.duration_ms);
        const normalized = {
          ...step,
          step: number(step.step),
          duration_ms: durationMs,
          codex_returncode: Number(step.codex_returncode || 0),
          codex_tokens_used: number(step.codex_tokens_used),
          command_duration_ms: number(step.command_duration_ms),
          inter_command_idle_ms: number(step.inter_command_idle_ms),
          browser_busy_union_ms: number(step.browser_busy_union_ms),
          command_errors: number(step.command_errors),
          html_commands: number(step.html_commands),
          html_bytes: number(step.html_bytes),
          post_browser_idle_ms: number(step.post_browser_idle_ms),
          total_cmds: number(step.total_cmds) || totalFromBreakdown,
          command_breakdown: breakdown,
          command_timeline: normalizeTimeline(step.command_timeline),
          timeline_total_ms: number(step.timeline_total_ms),
          screenshots: asArray(step.screenshots),
          _stepOffset: stepOffset,
        };
        stepOffset += durationMs;
        return normalized;
      });
      return {
        ...session,
        session_id: String(session.session_id || ''),
        source: session.source === 'eval' ? 'eval' : 'direct',
        status: String(session.status || 'unknown'),
        host: String(session.host || 'unknown'),
        start_ts: number(session.start_ts),
        duration_ms: number(session.duration_ms) || steps.reduce((sum, step) => sum + number(step.duration_ms), 0),
        tokens_total: number(session.tokens_total) || steps.reduce((sum, step) => sum + number(step.codex_tokens_used), 0),
        step_count: number(session.step_count) || steps.length,
        cdp_count: number(session.cdp_count) || steps.reduce((sum, step) => sum + number(step.total_cmds), 0),
        errors: number(session.errors) || steps.reduce((sum, step) => sum + number(step.command_errors), 0),
        screenshot_count: number(session.screenshot_count)
          || steps.reduce((sum, step) => sum + asArray(step.screenshots).length, 0),
        steps,
      };
    });
  }

  function filteredSessions() {
    let list = [...state.sessions];
    if (state.filterSource !== 'all') {
      list = list.filter((session) => session.source === state.filterSource);
    }
    if (state.filterStatus !== 'all') {
      list = list.filter((session) => session.status === state.filterStatus);
    }
    if (state.search) {
      const needle = state.search.toLowerCase();
      list = list.filter((session) => [
        session.session_id,
        session.eval_run_id,
        session.eval_case_id,
        session.host,
        session.trace_dir,
      ].some((value) => String(value || '').toLowerCase().includes(needle)));
    }
    list.sort((left, right) => {
      let leftValue = left[state.sortKey] ?? 0;
      let rightValue = right[state.sortKey] ?? 0;
      if (typeof leftValue === 'string') {
        leftValue = leftValue.toLowerCase();
        rightValue = String(rightValue).toLowerCase();
      }
      if (leftValue < rightValue) {
        return state.sortDir === 'asc' ? -1 : 1;
      }
      if (leftValue > rightValue) {
        return state.sortDir === 'asc' ? 1 : -1;
      }
      return 0;
    });
    return list;
  }

  function cdpType(command) {
    return CDP_INFO[command]?.type || 'read';
  }

  function typeClass(type) {
    return {
      read: 'read',
      write: 'write',
      navigation: 'navigation',
      wait: 'wait',
      evidence: 'evidence',
      heavy: 'heavy',
    }[type] || 'read';
  }

  function statusClass(status) {
    const normalized = String(status || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '_');
    const known = new Set([
      'pending',
      'starting',
      'running',
      'waiting_user',
      'payment_ready',
      'unverified',
      'finished',
      'judged',
      'timeout',
      'judge_failed',
      'skipped_auth_missing',
      'completed',
      'success',
      'failed',
      'error',
      'unknown',
    ]);
    return known.has(normalized) ? normalized : 'unknown';
  }

  function ensureTooltip(key, className) {
    if (!state[key]) {
      state[key] = node('div', className);
      state[key].style.display = 'none';
      document.body.appendChild(state[key]);
    }
    return state[key];
  }

  function positionTooltip(tooltip, event) {
    const width = tooltip.offsetWidth || 240;
    tooltip.style.left = `${Math.min(event.clientX + 14, window.innerWidth - width - 8)}px`;
    tooltip.style.top = `${Math.max(4, event.clientY - 10)}px`;
  }

  function attachGanttTooltip(element, textValue) {
    const tooltip = ensureTooltip('ganttTooltipEl', 'stats-gantt-tooltip');
    element.addEventListener('mouseenter', (event) => {
      tooltip.textContent = textValue;
      tooltip.style.display = 'block';
      positionTooltip(tooltip, event);
    });
    element.addEventListener('mousemove', (event) => positionTooltip(tooltip, event));
    element.addEventListener('mouseleave', () => {
      tooltip.style.display = 'none';
    });
  }

  function attachCommandTooltip(element, command) {
    const info = CDP_INFO[command] || {};
    const tooltip = ensureTooltip('cmdTooltipEl', 'stats-cmd-tooltip');
    element.addEventListener('mouseenter', (event) => {
      tooltip.replaceChildren(
        node('div', 'stats-cmd-tooltip-title', command),
        node('div', 'stats-cmd-tooltip-section', 'Что делает'),
        document.createTextNode(info.desc || '—'),
        node('div', 'stats-cmd-tooltip-section', 'Когда использовать'),
        document.createTextNode(info.when || '—'),
        node('div', 'stats-cmd-tooltip-section', 'Стоимость / риск'),
        document.createTextNode(info.risk || '—'),
      );
      tooltip.style.display = 'block';
      positionTooltip(tooltip, event);
    });
    element.addEventListener('mousemove', (event) => positionTooltip(tooltip, event));
    element.addEventListener('mouseleave', () => {
      tooltip.style.display = 'none';
    });
  }

  function setGanttHighlight(scope, category) {
    const active = Boolean(category);
    scope.classList.toggle('is-highlighted', active);
    for (const element of scope.querySelectorAll('[data-gantt-types]')) {
      const types = String(element.dataset.ganttTypes || '').split(/\s+/).filter(Boolean);
      const matched = active && types.includes(category);
      element.classList.toggle('highlighted', matched);
      element.classList.toggle('dimmed', active && !matched);
    }
    for (const element of scope.querySelectorAll('.stats-gantt-lane[data-gantt-category], .stats-gantt-legend-item[data-gantt-category]')) {
      const matched = active && element.dataset.ganttCategory === category;
      element.classList.toggle('highlighted', matched);
      element.classList.toggle('dimmed', active && !matched);
    }
  }

  function attachGanttHighlight(element, scope, category) {
    element.dataset.ganttCategory = category;
    element.addEventListener('mouseenter', () => setGanttHighlight(scope, category));
    element.addEventListener('mouseleave', () => setGanttHighlight(scope, null));
    element.addEventListener('focus', () => setGanttHighlight(scope, category));
    element.addEventListener('blur', () => setGanttHighlight(scope, null));
  }

  function renderKpis(list) {
    const grid = q('.stats-kpi-grid');
    if (!grid) {
      return;
    }
    const htmlBytes = list.reduce(
      (sum, session) => sum + session.steps.reduce((stepSum, step) => stepSum + number(step.html_bytes), 0),
      0,
    );
    const kpis = [
      ['Sessions', list.length],
      ['Eval', list.filter((session) => session.source === 'eval').length],
      ['Direct', list.filter((session) => session.source === 'direct').length],
      ['Errors', list.reduce((sum, session) => sum + number(session.errors), 0)],
      ['Med duration', fmtMs(median(list.map((session) => session.duration_ms)))],
      ['Tokens total', fmtNum(list.reduce((sum, session) => sum + number(session.tokens_total), 0))],
      ['Browser cmds', fmtNum(list.reduce((sum, session) => sum + number(session.cdp_count), 0))],
      ['HTML bytes', htmlBytes >= 1048576 ? `${(htmlBytes / 1048576).toFixed(1)}M` : `${Math.round(htmlBytes / 1024)}K`],
    ];
    grid.replaceChildren();
    for (const [label, value] of kpis) {
      const item = node('div', 'stats-kpi-item');
      item.append(node('span', 'stats-kpi-label', label), node('span', 'stats-kpi-value', String(value)));
      grid.append(item);
    }
  }

  function renderFilters() {
    const bar = q('.stats-filters');
    if (!bar) {
      return;
    }
    bar.replaceChildren();

    const search = document.createElement('input');
    search.type = 'search';
    search.placeholder = 'session_id / eval_run_id / case_id / host…';
    search.value = state.search;
    search.addEventListener('input', (event) => {
      state.search = event.target.value;
      renderStats();
    });
    bar.append(search);

    for (const [key, label] of [['all', 'Все'], ['eval', 'eval'], ['direct', 'direct']]) {
      const button = node('button', `stats-filter-btn${state.filterSource === key ? ' active' : ''}`, label);
      button.type = 'button';
      button.addEventListener('click', () => {
        state.filterSource = key;
        renderFilters();
        renderStats();
      });
      bar.append(button);
    }

    const statuses = [...new Set(state.sessions.map((session) => session.status).filter(Boolean))].sort();
    for (const status of statuses) {
      const active = state.filterStatus === status;
      const button = node('button', `stats-filter-btn${active ? ' active' : ''}`, status);
      button.type = 'button';
      button.addEventListener('click', () => {
        state.filterStatus = active ? 'all' : status;
        renderFilters();
        renderStats();
      });
      bar.append(button);
    }
  }

  function renderTable(list) {
    const tbody = q('#stats-sessions-tbody');
    const empty = q('#stats-sessions-empty');
    if (!tbody || !empty) {
      return;
    }
    tbody.replaceChildren();
    empty.hidden = list.length > 0;
    if (!list.length) {
      const message = state.loading
        ? 'Загрузка stats…'
        : state.error || 'Нет trace-сессий. Убедитесь что BUYER_TRACE_DIR настроен и содержит step-XXX-trace.json.';
      empty.textContent = message;
    }

    qa('.stats-session-table th[data-sort]').forEach((header) => {
      const icon = header.querySelector('.sort-icon');
      header.classList.remove('sort-asc', 'sort-desc');
      if (!icon) {
        return;
      }
      if (header.dataset.sort === state.sortKey) {
        header.classList.add(state.sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
        icon.textContent = state.sortDir === 'asc' ? '↑' : '↓';
      } else {
        icon.textContent = '↕';
      }
    });

    for (const session of list) {
      const row = document.createElement('tr');
      if (session.errors > 0) {
        row.classList.add('has-error');
      }
      if (session.session_id === state.selectedSessionId) {
        row.classList.add('selected');
      }

      row.append(
        tableCell('stats-mono muted', fmtTs(session.start_ts)),
        tableCell('stats-mono', shortId(session.session_id)),
        chipCell(`stats-source-${session.source}`, session.source),
        tableCell('stats-mono muted', session.host),
        chipCell(`eval-status ${statusClass(session.status)}`, session.status),
        tableCell('stats-mono', fmtMs(session.duration_ms)),
        tableCell('stats-mono', fmtNum(session.tokens_total)),
        tableCell('stats-mono', String(session.step_count)),
        tableCell('stats-mono', fmtNum(session.cdp_count)),
        tableCell(`stats-mono ${session.errors > 0 ? 'error' : 'muted'}`, session.errors > 0 ? String(session.errors) : '—'),
        evalCell(session),
        tableCell('stats-mono muted', `${session.step_count}t · ${session.screenshot_count}ss`),
      );
      row.firstChild.firstChild.title = session.start_ts ? new Date(session.start_ts).toISOString() : '';
      row.children[1].firstChild.title = session.session_id;
      row.addEventListener('click', () => {
        state.selectedSessionId = session.session_id;
        renderStats();
      });
      tbody.append(row);
    }
  }

  function tableCell(className, value) {
    const cell = document.createElement('td');
    cell.append(node('span', className, value));
    return cell;
  }

  function chipCell(className, value) {
    const cell = document.createElement('td');
    cell.append(node('span', className, value));
    return cell;
  }

  function evalCell(session) {
    const cell = document.createElement('td');
    if (session.eval_case_id) {
      const tag = node('span', 'eval-tag', shortId(session.eval_case_id, 22));
      tag.title = session.eval_case_id;
      cell.append(tag);
    } else {
      cell.append(node('span', 'stats-mono muted', '—'));
    }
    return cell;
  }

  function setupTableSort() {
    qa('.stats-session-table th[data-sort]').forEach((header) => {
      header.addEventListener('click', () => {
        const key = header.dataset.sort;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          state.sortKey = key;
          state.sortDir = 'desc';
        }
        renderStats();
      });
    });
  }

  function renderSessionDetail() {
    const container = q('#stats-session-detail-content');
    const wrap = q('#stats-session-detail-wrap');
    const placeholder = q('#stats-detail-placeholder');
    if (!container || !wrap || !placeholder) {
      return;
    }
    const session = state.sessions.find((item) => item.session_id === state.selectedSessionId);
    container.replaceChildren();
    wrap.hidden = !session;
    placeholder.hidden = Boolean(session);
    if (!session) {
      return;
    }

    const header = node('div', 'stats-detail-header');
    const idWrap = node('span', 'stats-mono');
    idWrap.append(node('span', `stats-source-${session.source}`, session.source), ' ');
    const idCode = node('span', 'code', shortId(session.session_id, 34));
    idCode.title = session.session_id;
    idWrap.append(idCode);
    header.append(idWrap, node('span', `eval-status ${statusClass(session.status)}`, session.status));
    container.append(header);

    const meta = node('div', 'stats-detail-meta-grid');
    const items = [
      ['start', fmtTs(session.start_ts)],
      ['duration', fmtMs(session.duration_ms)],
      ['tokens', fmtNum(session.tokens_total)],
      ['steps', String(session.step_count)],
      ['CDP commands', fmtNum(session.cdp_count)],
      ['errors', String(session.errors)],
      ['screenshots', String(session.screenshot_count)],
      ['host', session.host],
      ['trace_dir', session.trace_dir || '—'],
    ];
    if (session.eval_case_id) {
      items.splice(8, 0, ['eval_case_id', session.eval_case_id], ['eval_run_id', shortId(session.eval_run_id, 28)]);
    }
    for (const [label, value] of items) {
      const item = node('div', 'stats-detail-meta-item');
      item.append(node('span', 'stats-detail-meta-label', label), node('span', 'stats-detail-meta-value', value));
      meta.append(item);
    }
    container.append(meta);

    const twoCol = node('div', 'stats-detail-two-col');
    const ganttCol = node('div', 'stats-detail-col-wide');
    ganttCol.append(panelHead('Timeline', 'Browser timeline'), renderGantt(session));
    const commandCol = node('div', 'stats-detail-col-narrow');
    commandCol.append(panelHead('This session', 'Command breakdown'), renderSessionCommandBreakdown(session));
    twoCol.append(ganttCol, commandCol);
    container.append(twoCol, panelHead('Steps', 'Per-step detail'), renderStepsTable(session));
  }

  function panelHead(eyebrow, title) {
    const head = node('div', 'panel-head');
    const inner = node('div');
    inner.append(node('p', 'eyebrow', eyebrow), node('h2', null, title));
    head.append(inner);
    return head;
  }

  function collectTimelineCommands(session) {
    const raw = [];
    for (const step of session.steps) {
      for (const item of step.command_timeline) {
        raw.push({ step, item });
      }
    }
    const useAbsolute = raw.length > 0 && raw.every(({ item }) => item.start_ts > 0 && item.end_ts > 0);
    return raw
      .map(({ step, item }, order) => {
        const offsetMs = number(item.offset_ms);
        const endOffsetMs = number(item.end_offset_ms) || offsetMs + number(item.duration_ms);
        const localStartMs = number(step._stepOffset) + offsetMs;
        const localEndMs = number(step._stepOffset) + endOffsetMs;
        const startMs = useAbsolute ? number(item.start_ts) : localStartMs;
        const rawEndMs = useAbsolute ? number(item.end_ts) : localEndMs;
        const durationMs = Math.max(number(item.duration_ms), rawEndMs - startMs, 1);
        return {
          command: item.command,
          type: cdpType(item.command),
          event: item.event,
          ok: item.ok !== false,
          step: step.step,
          sequence: item.sequence,
          attempt_id: item.attempt_id,
          startMs,
          endMs: Math.max(rawEndMs, startMs + durationMs),
          durationMs,
          absolute: useAbsolute,
          order,
        };
      })
      .sort((left, right) => (
        left.startMs - right.startMs
        || left.endMs - right.endMs
        || left.sequence - right.sequence
        || left.order - right.order
      ));
  }

  function timelineBounds(commands) {
    if (!commands.length) {
      return null;
    }
    const startMs = Math.min(...commands.map((command) => command.startMs));
    const endMs = Math.max(...commands.map((command) => command.endMs));
    return {
      startMs,
      endMs,
      totalMs: Math.max(endMs - startMs, 1),
      absolute: commands.every((command) => command.absolute),
    };
  }

  function buildIdleSegments(commands, totalMs) {
    const intervals = commands
      .map((command) => ({
        start: Math.min(totalMs, Math.max(0, command.offMs)),
        end: Math.min(totalMs, Math.max(0, command.endOffMs)),
      }))
      .filter((interval) => interval.end > interval.start)
      .sort((left, right) => left.start - right.start || left.end - right.end);
    if (intervals.length < 2) {
      return [];
    }

    const merged = [intervals[0]];
    for (const interval of intervals.slice(1)) {
      const current = merged[merged.length - 1];
      if (interval.start <= current.end) {
        current.end = Math.max(current.end, interval.end);
      } else {
        merged.push({ ...interval });
      }
    }

    const gaps = [];
    for (let index = 1; index < merged.length; index += 1) {
      const previous = merged[index - 1];
      const current = merged[index];
      if (current.start > previous.end) {
        gaps.push({ offMs: previous.end, durationMs: current.start - previous.end });
      }
    }
    return gaps;
  }

  function commandTimelineTip(command) {
    return [
      command.command,
      `type: ${command.type}`,
      `step: ${command.step || '—'}`,
      `sequence: ${command.sequence || '—'}`,
      `status: ${command.ok ? 'ok' : 'failed'}`,
      `dur: ${fmtMs(command.durationMs)}`,
      `start: ${formatTimelineTs(command.startMs, command.absolute)}`,
    ].join('\n');
  }

  function commandSegment(command) {
    const types = [command.type];
    if (!command.ok) {
      types.push('error');
    }
    return {
      offMs: command.offMs,
      durationMs: Math.max(command.endOffMs - command.offMs, 1),
      className: `${typeClass(command.type)}${command.ok ? '' : ' error'}`,
      types,
      highlightCategory: command.type,
      tip: commandTimelineTip(command),
    };
  }

  function commandTimelineLane(command) {
    return {
      label: `#${command.order + 1} ${command.command}`,
      category: command.type,
      segments: [commandSegment(command)],
    };
  }

  function renderGantt(session) {
    const wrapper = node('div', 'stats-gantt-wrap');
    const commands = collectTimelineCommands(session);
    const bounds = timelineBounds(commands);
    if (!bounds) {
      wrapper.append(node('div', 'empty', 'Нет данных timeline.'));
      return wrapper;
    }
    wrapper.append(node('p', 'stats-gantt-notice', 'Шкала построена по timestamps из browser-actions JSONL; runtime/idle — промежутки без активных CDP-команд.'));

    const totalMs = bounds.totalMs;
    const pct = (ms) => `${Math.min(100, Math.max(0, (ms / totalMs) * 100)).toFixed(2)}%`;
    const width = (ms) => `${Math.min(100, Math.max(0.3, (ms / totalMs) * 100)).toFixed(2)}%`;
    for (const command of commands) {
      command.offMs = Math.max(command.startMs - bounds.startMs, 0);
      command.endOffMs = Math.max(command.endMs - bounds.startMs, command.offMs + 1);
    }

    const gantt = node('div', 'stats-gantt');
    const axisRow = node('div', 'stats-gantt-axis-row');
    const axis = node('div', 'stats-gantt-axis');
    for (const point of [0, 0.25, 0.5, 0.75, 1]) {
      axis.append(node('span', null, fmtMs(totalMs * point)));
    }
    axisRow.append(node('span', 'stats-gantt-label-col', 'Lane'), axis);
    gantt.append(axisRow);

    const group = node('div', 'stats-gantt-step-group');
    const idleSegments = buildIdleSegments(commands, totalMs).map((segment) => ({
      ...segment,
      className: 'runtime',
      types: ['runtime'],
      tip: [
        'runtime/idle',
        'between active CDP commands',
        `dur: ${fmtMs(segment.durationMs)}`,
        `start: ${formatTimelineTs(bounds.absolute ? bounds.startMs + segment.offMs : segment.offMs, bounds.absolute)}`,
      ].join('\n'),
    }));
    const lanes = [
      ...(idleSegments.length ? [{ label: 'runtime/idle', category: 'runtime', segments: idleSegments }] : []),
      ...commands.map(commandTimelineLane),
    ];

    for (const lane of lanes) {
      const row = node('div', 'stats-gantt-lane');
      const label = node('span', 'stats-gantt-lane-label', lane.label);
      row.tabIndex = 0;
      attachGanttHighlight(row, wrapper, lane.category);
      const track = node('div', 'stats-gantt-track');
      for (const segment of lane.segments) {
        const durationMs = Math.max(Math.min(segment.durationMs, totalMs - segment.offMs), 1);
        const bar = node('div', `stats-gantt-bar ${segment.className}`);
        bar.dataset.ganttTypes = segment.types.join(' ');
        bar.style.left = pct(segment.offMs);
        bar.style.width = width(durationMs);
        attachGanttTooltip(bar, segment.tip);
        attachGanttHighlight(bar, wrapper, segment.highlightCategory || segment.types[0]);
        track.append(bar);
      }
      row.append(label, track);
      group.append(row);
    }
    gantt.append(group);

    const legend = node('div', 'stats-gantt-legend');
    for (const { category, label } of GANTT_CATEGORIES) {
      const item = node('div', 'stats-gantt-legend-item');
      item.tabIndex = 0;
      attachGanttHighlight(item, wrapper, category);
      const swatch = node('div', `stats-gantt-legend-swatch stats-gantt-bar ${category}`);
      swatch.dataset.ganttTypes = category;
      item.append(swatch, node('span', null, label));
      legend.append(item);
    }
    wrapper.append(gantt, legend);
    return wrapper;
  }

  function renderStepsTable(session) {
    const wrap = node('div', 'eval-table-wrap');
    const table = node('table', 'stats-steps-table');
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    for (const column of ['Step', 'rc', 'Duration', 'Tokens', 'Cmd dur', 'Idle', 'Cmds', 'HTML', 'Errors', 'stdout', 'stderr']) {
      headerRow.append(node('th', null, column));
    }
    thead.append(headerRow);
    table.append(thead);

    const tbody = document.createElement('tbody');
    for (const step of session.steps) {
      const row = document.createElement('tr');
      row.append(
        tableCell('stats-mono', String(step.step)),
        tableCell(`stats-mono ${step.codex_returncode !== 0 ? 'error' : 'ok'}`, String(step.codex_returncode)),
        tableCell('stats-mono', fmtMs(step.duration_ms)),
        tableCell('stats-mono', fmtNum(step.codex_tokens_used)),
        tableCell('stats-mono', fmtMs(step.command_duration_ms)),
        tableCell('stats-mono muted', fmtMs(step.inter_command_idle_ms)),
        tableCell('stats-mono', String(step.total_cmds)),
        tableCell('stats-mono muted', step.html_bytes ? `${Math.round(step.html_bytes / 1024)}K` : '—'),
        tableCell(`stats-mono ${step.command_errors > 0 ? 'error' : 'muted'}`, step.command_errors > 0 ? String(step.command_errors) : '—'),
        detailCell('stdout', step.stdout_tail, false),
        detailCell('stderr', step.stderr_tail, true),
      );
      tbody.append(row);
    }
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  function detailCell(label, value, isError) {
    const cell = document.createElement('td');
    if (!value) {
      cell.append(node('span', 'stats-mono muted', '—'));
      return cell;
    }
    const details = document.createElement('details');
    details.append(node('summary', 'eval-callbacks-summary', label));
    const pre = node('pre');
    pre.textContent = value;
    pre.style.cssText = [
      'max-height:100px',
      'overflow:auto',
      'margin:3px 0 0',
      `border:1px solid ${isError ? 'rgba(180,35,24,0.3)' : 'var(--line)'}`,
      'border-radius:var(--radius-sm)',
      `background:${isError ? 'rgba(180,35,24,0.04)' : 'rgba(255,255,255,0.58)'}`,
      'padding:5px 7px',
      'font-size:0.68rem',
      `color:${isError ? 'var(--error)' : 'inherit'}`,
      'white-space:pre-wrap',
      'font-family:var(--font-mono)',
    ].join(';');
    details.append(pre);
    cell.append(details);
    return cell;
  }

  function aggregateCommands(sessions) {
    const aggregate = {};
    for (const session of sessions) {
      for (const step of session.steps) {
        for (const [command, stats] of Object.entries(step.command_breakdown)) {
          aggregate[command] = aggregate[command] || { count: 0, duration_ms: 0, errors: 0 };
          aggregate[command].count += number(stats.count);
          aggregate[command].duration_ms += number(stats.duration_ms);
          aggregate[command].errors += number(stats.errors);
        }
      }
    }
    return aggregate;
  }

  function renderSessionCommandBreakdown(session) {
    return buildCommandBreakdownTable(aggregateCommands([session]));
  }

  function renderCommandBreakdown(list) {
    const container = q('#stats-cmd-breakdown-content');
    if (!container) {
      return;
    }
    container.replaceChildren(buildCommandBreakdownTable(aggregateCommands(list)));
  }

  function buildCommandBreakdownTable(aggregate) {
    const rows = Object.entries(aggregate).sort((left, right) => number(right[1].duration_ms) - number(left[1].duration_ms));
    if (!rows.length) {
      return node('div', 'empty', 'Нет данных о командах.');
    }
    const totalDuration = rows.reduce((sum, [, stats]) => sum + number(stats.duration_ms), 0);
    const wrap = node('div', 'stats-cmd-table-wrap');
    const table = node('table', 'stats-command-table');
    const thead = document.createElement('thead');
    const header = document.createElement('tr');
    for (const column of ['Command', 'Type', 'Count', 'Dur', 'Err', 'Share']) {
      header.append(node('th', null, column));
    }
    thead.append(header);
    table.append(thead);
    const tbody = document.createElement('tbody');
    for (const [command, stats] of rows) {
      const type = cdpType(command);
      const row = document.createElement('tr');
      if (number(stats.errors) > 0) {
        row.classList.add('cmd-has-error');
      }
      const commandCell = document.createElement('td');
      const commandName = node('span', 'stats-command-name', command);
      attachCommandTooltip(commandName, command);
      commandCell.append(commandName);
      const share = totalDuration > 0 ? number(stats.duration_ms) / totalDuration : 0;
      row.append(
        commandCell,
        chipCell(`stats-command-type ${type}`, type),
        tableCell('stats-mono', fmtNum(stats.count)),
        tableCell('stats-mono', fmtMs(stats.duration_ms)),
        tableCell(`stats-mono ${number(stats.errors) > 0 ? 'error' : 'muted'}`, number(stats.errors) > 0 ? String(stats.errors) : '—'),
        shareCell(share),
      );
      tbody.append(row);
    }
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  function shareCell(share) {
    const cell = document.createElement('td');
    const wrap = node('div', 'stats-share-bar');
    const track = node('div', 'stats-share-track');
    const fill = node('div', 'stats-share-fill');
    fill.style.width = `${(share * 100).toFixed(1)}%`;
    track.append(fill);
    const label = node('span', 'stats-mono muted', `${(share * 100).toFixed(0)}%`);
    label.style.fontSize = '0.64rem';
    wrap.append(track, label);
    cell.append(wrap);
    return cell;
  }

  function renderEvalSlices(list) {
    renderEvalTable(q('#stats-eval-by-case'), list, 'eval_case_id', 'Case');
    renderEvalTable(q('#stats-eval-by-host'), list, 'host', 'Host');
  }

  function renderEvalTable(container, sessions, key, label) {
    if (!container) {
      return;
    }
    container.replaceChildren();
    const evalSessions = sessions.filter((session) => session.source === 'eval');
    const groups = {};
    for (const session of evalSessions) {
      const groupKey = session[key] || 'unknown';
      groups[groupKey] = groups[groupKey] || [];
      groups[groupKey].push(session);
    }
    if (!Object.keys(groups).length) {
      container.append(node('div', 'empty', 'Нет eval-сессий.'));
      return;
    }

    const wrap = node('div', 'eval-table-wrap');
    const table = node('table', 'stats-eval-table');
    const thead = document.createElement('thead');
    const header = document.createElement('tr');
    for (const column of [label, 'Sessions', 'Success', 'Med duration', 'Med tokens']) {
      header.append(node('th', null, column));
    }
    thead.append(header);
    table.append(thead);
    const tbody = document.createElement('tbody');
    for (const [groupKey, groupedSessions] of Object.entries(groups)) {
      const ok = groupedSessions.filter((session) => session.status === 'payment_ready' || session.status === 'completed').length;
      const row = document.createElement('tr');
      const keyCell = document.createElement('td');
      keyCell.append(node('span', key === 'eval_case_id' ? 'eval-tag' : 'stats-mono', groupKey));
      row.append(
        keyCell,
        tableCell('stats-mono', String(groupedSessions.length)),
        chipCell(`eval-status ${ok === groupedSessions.length ? 'payment_ready' : ok > 0 ? 'unverified' : 'timeout'}`, `${ok}/${groupedSessions.length}`),
        tableCell('stats-mono', fmtMs(median(groupedSessions.map((session) => session.duration_ms)))),
        tableCell('stats-mono', fmtNum(median(groupedSessions.map((session) => session.tokens_total)))),
      );
      tbody.append(row);
    }
    table.append(tbody);
    wrap.append(table);
    container.append(wrap);
  }

  function renderStats() {
    const list = filteredSessions();
    if (state.selectedSessionId && !state.sessions.some((session) => session.session_id === state.selectedSessionId)) {
      state.selectedSessionId = null;
    }
    renderKpis(list);
    renderCommandBreakdown(list);
    renderEvalSlices(list);
    renderTable(list);
    renderSessionDetail();
  }

  async function loadStats() {
    state.loading = true;
    state.error = null;
    renderStats();
    try {
      const payload = await fetchJson(STATS_SERVICE_PATH);
      state.sessions = normalizeSessions(payload?.sessions || []);
      state.warnings = asArray(payload?.warnings);
      if (!state.selectedSessionId && state.sessions.length) {
        state.selectedSessionId = state.sessions[0].session_id;
      }
      state.loadedOnce = true;
    } catch (error) {
      state.sessions = [];
      state.selectedSessionId = null;
      state.error = `Stats service недоступен: ${error.message}`;
    } finally {
      state.loading = false;
      renderFilters();
      renderStats();
    }
  }

  function setupStatsTabRefresh() {
    const tab = document.getElementById('tab-stats');
    if (!tab) {
      return;
    }
    tab.addEventListener('click', () => {
      loadStats().catch(() => {});
    });
  }

  function init() {
    renderFilters();
    setupTableSort();
    setupStatsTabRefresh();
    loadStats().catch(() => {});
  }

  document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', init)
    : init();

  window.MicroUiStats = {
    state,
    refresh: loadStats,
    renderTable,
    renderSessionDetail,
  };
})();
