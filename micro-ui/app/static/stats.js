(() => {
  const STATS_SERVICE_PATH = '/api/eval/stats/sessions';
  const CDP_INFO = {
    url: { type: 'read', desc: 'Возвращает текущий URL страницы.', when: 'Проверка навигации и payment boundary.', risk: 'Дешевая read-команда.' },
    title: { type: 'read', desc: 'Возвращает title страницы.', when: 'Быстрая проверка нужной страницы.', risk: 'Дешевая read-команда.' },
    goto: { type: 'navigation', desc: 'Открывает URL.', when: 'Первый переход, recovery, явный переход.', risk: 'State-changing действие.' },
    click: { type: 'write', desc: 'Кликает по selector.', when: 'Кнопки, выбор опций, checkout.', risk: 'State-changing действие, нужен milestone.' },
    fill: { type: 'write', desc: 'Заполняет input/textarea.', when: 'Формы, поиск, адрес.', risk: 'Может вводить персональные данные.' },
    press: { type: 'write', desc: 'Нажимает клавишу.', when: 'Enter, Escape, Tab, hotkeys.', risk: 'Эффект зависит от focus.' },
    wait: { type: 'wait', desc: 'Ждет заданное время.', when: 'Fallback при нестабильной загрузке.', risk: 'Дорогая и непрозрачная команда.' },
    'wait-url': { type: 'wait', desc: 'Ждет URL по contains/regex.', when: 'После клика, submit или navigation.', risk: 'Может timeout.' },
    'wait-selector': { type: 'wait', desc: 'Ждет появления DOM selector.', when: 'DOM milestone после действия.', risk: 'Зависит от selector.' },
    snapshot: { type: 'read', desc: 'Возвращает структурный список видимых элементов.', when: 'Основной observe-инструмент.', risk: 'Средняя стоимость.' },
    links: { type: 'read', desc: 'Возвращает ссылки внутри selector.', when: 'Поиск навигационных кандидатов.', risk: 'Дешевая read-команда.' },
    text: { type: 'read', desc: 'Возвращает текст selector с лимитом.', when: 'Проверка содержимого блока.', risk: 'Может быть шумным.' },
    exists: { type: 'read', desc: 'Проверяет наличие selector.', when: 'Milestone/evidence без чтения всего DOM.', risk: 'Дешевая read-команда.' },
    attr: { type: 'read', desc: 'Читает атрибут selector.', when: 'Проверить state, href, value.', risk: 'Дешевая read-команда.' },
    screenshot: { type: 'evidence', desc: 'Сохраняет screenshot в файл.', when: 'Evidence для judge/debug.', risk: 'Дороже read; пишет artifact.' },
    html: { type: 'heavy', desc: 'Возвращает или сохраняет HTML.', when: 'Fallback после snapshot, text, links, attr.', risk: 'Самая шумная команда.' },
  };

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
          llm_duration_ms: number(step.llm_duration_ms),
          post_browser_idle_ms: number(step.post_browser_idle_ms),
          total_cmds: number(step.total_cmds) || totalFromBreakdown,
          command_breakdown: breakdown,
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
    ganttCol.append(panelHead('Timeline', 'Step Gantt'), renderGantt(session));
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

  function renderGantt(session) {
    const wrapper = node('div');
    if (!session.steps.length || !session.duration_ms) {
      wrapper.append(node('div', 'empty', 'Нет данных timeline.'));
      return wrapper;
    }
    wrapper.append(node('p', 'stats-gantt-notice', 'Шкала построена по duration_ms и browser action summaries.'));

    const totalMs = Math.max(number(session.duration_ms), 1);
    const pct = (ms) => `${Math.min(100, Math.max(0, (ms / totalMs) * 100)).toFixed(2)}%`;
    const width = (ms) => `${Math.max(0.3, (ms / totalMs) * 100).toFixed(2)}%`;

    const gantt = node('div', 'stats-gantt');
    const axisRow = node('div', 'stats-gantt-axis-row');
    const axis = node('div', 'stats-gantt-axis');
    for (const point of [0, 0.25, 0.5, 0.75, 1]) {
      axis.append(node('span', null, fmtMs(totalMs * point)));
    }
    axisRow.append(node('span', 'stats-gantt-label-col', 'Step / Lane'), axis);
    gantt.append(axisRow);

    for (const step of session.steps) {
      const group = node('div', 'stats-gantt-step-group');
      const stepOffset = number(step._stepOffset);
      const lanes = [];
      const llmDuration = number(step.llm_duration_ms) || Math.max(number(step.duration_ms) - number(step.command_duration_ms), 0);
      if (llmDuration > 0) {
        lanes.push({
          label: `step ${step.step} · LLM`,
          className: 'llm',
          segments: [{ off: stepOffset, dur: llmDuration, tip: `LLM/Codex\nstep: ${step.step}\ndur: ${fmtMs(llmDuration)}\nmodel: ${step.codex_model || '—'}` }],
        });
      }

      const totalCommands = Object.values(step.command_breakdown).reduce((sum, item) => sum + number(item.count), 0) || 1;
      const fallbackPerCommand = number(step.command_duration_ms) / totalCommands;
      let commandOffset = stepOffset + llmDuration;
      const grouped = {};
      for (const [command, stats] of Object.entries(step.command_breakdown)) {
        const type = cdpType(command);
        const duration = number(stats.duration_ms) || fallbackPerCommand * number(stats.count);
        grouped[type] = grouped[type] || [];
        grouped[type].push({
          off: commandOffset,
          dur: duration,
          tip: `${command}\ntype: ${type}\ncount: ${number(stats.count)}\ndur: ${fmtMs(duration)}\nerrors: ${number(stats.errors) || '—'}`,
        });
        commandOffset += duration;
      }
      for (const [type, segments] of Object.entries(grouped)) {
        lanes.push({ label: type, className: typeClass(type), segments });
      }
      if (number(step.post_browser_idle_ms) > 0) {
        lanes.push({
          label: 'idle',
          className: 'idle',
          segments: [{ off: commandOffset, dur: number(step.post_browser_idle_ms), tip: `post_browser_idle\ndur: ${fmtMs(step.post_browser_idle_ms)}` }],
        });
      }
      if (number(step.command_errors) > 0) {
        lanes.push({
          label: 'error',
          className: 'error',
          segments: [{ off: stepOffset + llmDuration, dur: Math.max(totalMs * 0.008, 1000), tip: `CDP errors: ${step.command_errors}\n${step.stderr_tail || ''}` }],
        });
      }

      for (const [index, lane] of lanes.entries()) {
        const row = node('div', 'stats-gantt-lane');
        const label = node('span', `stats-gantt-lane-label${index === 0 ? ' step-label' : ''}`, lane.label);
        const track = node('div', 'stats-gantt-track');
        for (const segment of lane.segments) {
          const bar = node('div', `stats-gantt-bar ${lane.className}`);
          bar.style.left = pct(segment.off);
          bar.style.width = width(segment.dur);
          attachGanttTooltip(bar, segment.tip);
          track.append(bar);
        }
        row.append(label, track);
        group.append(row);
      }
      gantt.append(group);
    }

    const legend = node('div', 'stats-gantt-legend');
    for (const [className, label] of [['llm', 'LLM/Codex'], ['read', 'read'], ['write', 'write'], ['navigation', 'navigation'], ['wait', 'wait'], ['evidence', 'evidence'], ['heavy', 'heavy'], ['idle', 'idle'], ['error', 'error']]) {
      const item = node('div', 'stats-gantt-legend-item');
      item.append(node('div', `stats-gantt-legend-swatch stats-gantt-bar ${className}`), node('span', null, label));
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
