(() => {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const CONTRACT_PATHS = {
    cases: 'GET /cases',
    runs: 'GET /runs',
    createRun: 'POST /runs',
    runDetail: 'GET /runs/{eval_run_id}',
    judge: 'POST /runs/{eval_run_id}/judge',
    reply: 'POST /runs/{eval_run_id}/cases/{eval_case_id}/reply',
    caseDashboard: 'GET /dashboard/cases',
    hostDashboard: 'GET /dashboard/hosts',
  };

  const stubCases = [
    {
      eval_case_id: 'litres_book_odyssey_001',
      case_version: 'v1',
      variant_id: 'ebook_odyssey',
      title: 'Litres: электронная книга',
      host: 'litres.ru',
      start_url: 'https://www.litres.ru',
      auth_profile: 'sberid_litres',
      expected_outcome: 'payment_ready',
    },
    {
      eval_case_id: 'brandshop_sneakers_001',
      case_version: 'v1',
      variant_id: 'sneakers_smoke',
      title: 'Brandshop: кроссовки',
      host: 'brandshop.ru',
      start_url: 'https://brandshop.ru',
      auth_profile: 'sberid_brandshop',
      expected_outcome: 'payment_ready',
    },
    {
      eval_case_id: 'litres_book_gift_001',
      case_version: 'v1',
      variant_id: 'gift_book',
      title: 'Litres: подарок',
      host: 'litres.ru',
      start_url: 'https://www.litres.ru',
      auth_profile: null,
      expected_outcome: 'skipped_auth_missing',
    },
  ];

  const stubCaseDashboard = [
    {
      eval_case_id: 'litres_book_odyssey_001',
      title: 'Litres: электронная книга',
      status: 'payment_ready',
      duration_ms: [148000, 132000, 126000, 118000],
      buyer_tokens_used: [8200, 7900, 7350, 7100],
      baseline_duration_ms: 129000,
      baseline_tokens: 7625,
    },
    {
      eval_case_id: 'brandshop_sneakers_001',
      title: 'Brandshop: кроссовки',
      status: 'waiting_user',
      duration_ms: [176000, 164000, 161000, 158000],
      buyer_tokens_used: [9600, 9250, 9010, 8840],
      baseline_duration_ms: 162500,
      baseline_tokens: 9130,
    },
  ];

  const stubHostDashboard = [
    {
      host: 'litres.ru',
      status: 'payment_ready',
      duration_ms: [148000, 132000, 126000, 118000],
      buyer_tokens_used: [8200, 7900, 7350, 7100],
      success_rate: '3/4',
    },
    {
      host: 'brandshop.ru',
      status: 'waiting_user',
      duration_ms: [176000, 164000, 161000, 158000],
      buyer_tokens_used: [9600, 9250, 9010, 8840],
      success_rate: '1/2',
    },
  ];

  const nodes = {
    casesList: document.getElementById('eval-cases-list'),
    caseForm: document.getElementById('eval-case-form'),
    startRun: document.getElementById('eval-start-run'),
    startResult: document.getElementById('eval-start-result'),
    runLabel: document.getElementById('eval-run-label'),
    runDetail: document.getElementById('eval-run-detail'),
    runJudge: document.getElementById('eval-run-judge'),
    judgeResult: document.getElementById('eval-judge-result'),
    askQuestion: document.getElementById('eval-ask-user-question'),
    askForm: document.getElementById('eval-ask-user-form'),
    replyCaseId: document.getElementById('eval-reply-case-id'),
    replySessionId: document.getElementById('eval-reply-session-id'),
    replyId: document.getElementById('eval-reply-id'),
    replyMessage: document.getElementById('eval-reply-message'),
    replyResult: document.getElementById('eval-reply-result'),
    evaluationsBody: document.getElementById('eval-evaluations-body'),
    evaluationsEmpty: document.getElementById('eval-evaluations-empty'),
    caseDashboard: document.getElementById('eval-case-dashboard'),
    hostDashboard: document.getElementById('eval-host-dashboard'),
    metricRuns: document.getElementById('eval-metric-runs'),
    metricCases: document.getElementById('eval-metric-cases'),
    metricWaiting: document.getElementById('eval-metric-waiting'),
    metricJudged: document.getElementById('eval-metric-judged'),
  };

  if (!nodes.casesList) {
    return;
  }

  const state = {
    cases: [],
    activeRun: null,
    evaluations: [],
  };

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function runSuffix() {
    return nowIso().replace(/[-:.TZ]/g, '').slice(0, 14);
  }

  function formatMs(value) {
    const number = Number(value || 0);
    if (!number) {
      return '-';
    }
    return `${Math.round(number / 1000)}s`;
  }

  function formatNumber(value) {
    const number = Number(value || 0);
    if (!number) {
      return '-';
    }
    return new Intl.NumberFormat('ru-RU').format(number);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function seriesValues(value) {
    if (Array.isArray(value)) {
      return value.map((item) => Number(item || 0)).filter((item) => Number.isFinite(item));
    }
    const number = Number(value || 0);
    return Number.isFinite(number) && number > 0 ? [number] : [];
  }

  function lastSeriesValue(value) {
    const values = seriesValues(value);
    return values.length ? values[values.length - 1] : 0;
  }

  function formatList(value) {
    if (Array.isArray(value)) {
      return value.length ? value.join(', ') : '-';
    }
    return value || '-';
  }

  function extractList(data, keys = []) {
    if (Array.isArray(data)) {
      return data;
    }
    for (const key of keys) {
      if (Array.isArray(data?.[key])) {
        return data[key];
      }
    }
    return [];
  }

  function normalizeRunCase(item) {
    const callbacks = extractList(item, ['callbacks', 'callback_events']);
    return {
      ...item,
      runtime_status: item.runtime_status || item.state || 'pending',
      callbacks,
      title: item.title || item.eval_case_id || '-',
      host: item.host || '-',
      waiting_question: item.waiting_question || null,
      waiting_reply_id: item.waiting_reply_id || null,
      session_id: item.session_id || null,
    };
  }

  function normalizeRun(data) {
    const source = data?.run || data || {};
    return {
      ...source,
      cases: extractList(source, ['cases', 'case_results', 'items']).map(normalizeRunCase),
    };
  }

  function extractEvaluations(data) {
    return extractList(data, ['evaluations', 'results', 'items']);
  }

  async function loadRunDetail(evalRunId) {
    const data = await evalRequest(CONTRACT_PATHS.runDetail, { evalRunId });
    state.activeRun = normalizeRun(data);
    state.evaluations = extractEvaluations(data);
    return data;
  }

  function statusLabel(status) {
    const labels = {
      pending: 'ожидает',
      skipped_auth_missing: 'нет auth',
      starting: 'стартует',
      running: 'идет',
      waiting_user: 'ждет ответ',
      payment_ready: 'payment_ready',
      finished: 'finished',
      timeout: 'timeout',
      judge_pending: 'judge_pending',
      judged: 'judged',
      judge_failed: 'judge_failed',
    };
    return labels[status] || status || '-';
  }

  function statusClass(status) {
    return String(status || 'pending').toLowerCase().replace(/[^a-z0-9_-]/g, '_');
  }

  function getSelectedCaseIds() {
    return [...nodes.casesList.querySelectorAll('input[type="checkbox"]:checked')].map((item) => item.value);
  }

  function selectedWaitingCase() {
    if (!state.activeRun) {
      return null;
    }
    return asArray(state.activeRun.cases).find((item) => item.runtime_status === 'waiting_user' && item.waiting_reply_id) || null;
  }

  function routeFor(contract, options) {
    const runId = encodeURIComponent(options.evalRunId || '');
    const caseId = encodeURIComponent(options.evalCaseId || '');
    switch (contract) {
      case CONTRACT_PATHS.cases:
        return { method: 'GET', path: '/cases' };
      case CONTRACT_PATHS.runs:
        return { method: 'GET', path: '/runs' };
      case CONTRACT_PATHS.createRun:
        return { method: 'POST', path: '/runs' };
      case CONTRACT_PATHS.runDetail:
        return { method: 'GET', path: `/runs/${runId}` };
      case CONTRACT_PATHS.judge:
        return { method: 'POST', path: `/runs/${runId}/judge` };
      case CONTRACT_PATHS.reply:
        return { method: 'POST', path: `/runs/${runId}/cases/${caseId}/reply` };
      case CONTRACT_PATHS.caseDashboard:
        return { method: 'GET', path: '/dashboard/cases' };
      case CONTRACT_PATHS.hostDashboard:
        return { method: 'GET', path: '/dashboard/hosts' };
      default:
        throw new Error(`Неизвестный eval contract: ${contract}`);
    }
  }

  async function fetchEvalService(contract, options = {}) {
    const baseUrl = String(window.EVAL_SERVICE_BASE_URL || '').replace(/\/$/, '');
    const route = routeFor(contract, options);
    const response = await fetch(`${baseUrl}${route.path}`, {
      method: route.method,
      headers: {
        'Content-Type': 'application/json',
      },
      body: route.method === 'GET' ? undefined : JSON.stringify(options.payload || {}),
    });
    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = { raw: text };
    }
    if (!response.ok) {
      throw new Error(body?.detail || body?.raw || text || `HTTP ${response.status}`);
    }
    return body;
  }

  function createStubRun(caseIds) {
    const selectedCases = stubCases.filter((item) => caseIds.includes(item.eval_case_id));
    const evalRunId = `eval-local-${runSuffix()}`;
    return {
      eval_run_id: evalRunId,
      status: 'running',
      created_at: nowIso(),
      cases: selectedCases.map((item, index) => {
        const waiting = index === 0 && item.auth_profile;
        const sessionId = waiting ? `sess-${item.eval_case_id.replace(/_/g, '-')}` : null;
        const replyId = waiting ? `reply-${runSuffix()}` : null;
        return {
          ...item,
          runtime_status: waiting ? 'waiting_user' : item.auth_profile ? 'pending' : 'skipped_auth_missing',
          session_id: sessionId,
          waiting_reply_id: replyId,
          waiting_question: waiting ? 'Подтвердите город и допустимую платежную границу для сценария.' : null,
          callbacks: waiting
            ? [
                {
                  event_id: `${evalRunId}-task-created`,
                  event_type: 'task_created',
                  occurred_at: nowIso(),
                  session_id: sessionId,
                },
                {
                  event_id: `${evalRunId}-ask-user`,
                  event_type: 'ask_user',
                  occurred_at: nowIso(),
                  session_id: sessionId,
                  reply_id: replyId,
                },
              ]
            : [],
          artifacts: [],
        };
      }),
    };
  }

  function buildStubEvaluations(evalRunId) {
    const cases = asArray(state.activeRun?.cases);
    return cases.map((item, index) => {
      const dashboard = stubCaseDashboard.find((row) => row.eval_case_id === item.eval_case_id) || {};
      const hasAuth = Boolean(item.auth_profile);
      const runtimeStatus = item.runtime_status === 'skipped_auth_missing' ? 'skipped_auth_missing' : 'payment_ready';
      const callbacksCount = asArray(item.callbacks).length;

      return {
        eval_run_id: evalRunId,
        eval_case_id: item.eval_case_id,
        host: item.host,
        runtime_status: runtimeStatus,
        checks: hasAuth ? `session, callbacks:${callbacksCount}, payment_ready` : 'auth_missing',
        duration_ms: lastSeriesValue(dashboard.duration_ms) || 94000 + index * 17000,
        buyer_tokens_used: lastSeriesValue(dashboard.buyer_tokens_used) || 6200 + index * 850,
        recommendations_count: runtimeStatus === 'payment_ready' ? 0 : 1,
        artifacts: callbacksCount ? [`callbacks:${callbacksCount}`] : ['no_callbacks'],
      };
    });
  }

  async function stubRequest(contract, options = {}) {
    switch (contract) {
      case CONTRACT_PATHS.cases:
        return clone(stubCases);
      case CONTRACT_PATHS.createRun:
        return createStubRun(options.payload?.case_ids || []);
      case CONTRACT_PATHS.runDetail:
        return clone(state.activeRun);
      case CONTRACT_PATHS.judge:
        return {
          eval_run_id: options.evalRunId,
          status: 'judged',
          evaluations: buildStubEvaluations(options.evalRunId),
        };
      case CONTRACT_PATHS.reply:
        return {
          accepted: true,
          eval_run_id: options.evalRunId,
          eval_case_id: options.evalCaseId,
          reply_id: options.payload?.reply_id,
        };
      case CONTRACT_PATHS.caseDashboard:
        return clone(stubCaseDashboard);
      case CONTRACT_PATHS.hostDashboard:
        return clone(stubHostDashboard);
      default:
        return {};
    }
  }

  async function evalRequest(contract, options = {}) {
    if (window.EVAL_SERVICE_BASE_URL) {
      return fetchEvalService(contract, options);
    }
    return stubRequest(contract, options);
  }

  function renderCases() {
    nodes.casesList.replaceChildren();
    if (!state.cases.length) {
      nodes.casesList.appendChild(node('div', 'empty', 'Eval cases пока недоступны.'));
      updateStartButton();
      return;
    }
    for (const [index, item] of state.cases.entries()) {
      const label = node('label', 'eval-case-item');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = item.eval_case_id;
      checkbox.checked = index < 2;
      checkbox.addEventListener('change', updateStartButton);

      const content = node('span', 'eval-case-content');
      const title = node('span', 'eval-case-title', item.title);
      const meta = node(
        'span',
        'eval-case-meta',
        `${item.host} · ${item.eval_case_id} · ${item.case_version} · ${item.variant_id}`,
      );
      const expected = node('span', 'eval-case-outcome', item.expected_outcome);
      content.append(title, meta, expected);
      label.append(checkbox, content);
      nodes.casesList.appendChild(label);
    }
    updateStartButton();
  }

  function updateStartButton() {
    nodes.startRun.disabled = getSelectedCaseIds().length === 0;
  }

  function renderMetrics() {
    const cases = asArray(state.activeRun?.cases);
    const waitingCount = cases.filter((item) => item.runtime_status === 'waiting_user').length;
    nodes.metricRuns.textContent = state.activeRun ? '1' : '0';
    nodes.metricCases.textContent = String(state.cases.length);
    nodes.metricWaiting.textContent = String(waitingCount);
    nodes.metricJudged.textContent = String(state.evaluations.length);
  }

  function renderRunDetail() {
    nodes.runDetail.replaceChildren();
    nodes.runJudge.disabled = !state.activeRun;

    if (!state.activeRun) {
      nodes.runLabel.textContent = 'eval_run_id';
      nodes.runDetail.appendChild(node('div', 'empty', 'Нет активного eval-run.'));
      return;
    }

    nodes.runLabel.textContent = state.activeRun.eval_run_id;
    const cases = asArray(state.activeRun.cases);
    const summary = node('div', 'eval-run-summary');
    summary.append(
      node('span', `eval-status ${statusClass(state.activeRun.status)}`, statusLabel(state.activeRun.status)),
      node('span', 'code', state.activeRun.eval_run_id),
      node('span', null, `${cases.length} cases`),
    );
    nodes.runDetail.appendChild(summary);

    for (const item of cases) {
      const card = node('div', 'eval-run-case');
      const top = node('div', 'eval-run-case-top');
      top.append(
        node('strong', null, item.title),
        node('span', `eval-status ${statusClass(item.runtime_status)}`, statusLabel(item.runtime_status)),
      );

      const meta = node('div', 'eval-run-case-meta');
      meta.append(
        node('span', 'code', item.eval_case_id),
        node('span', null, item.host),
        node('span', 'code', item.session_id || 'session_id: -'),
        node('span', null, `callbacks: ${asArray(item.callbacks).length}`),
      );

      const callbacks = node('div', 'eval-callbacks');
      const caseCallbacks = asArray(item.callbacks);
      if (!caseCallbacks.length) {
        callbacks.appendChild(node('div', 'eval-callback-item', 'callbacks пока нет'));
      }
      for (const callback of caseCallbacks) {
        const callbackItem = node('div', 'eval-callback-item');
        callbackItem.append(
          node('span', 'code', callback.event_type),
          node('span', null, callback.event_id),
          node('span', null, new Date(callback.occurred_at).toLocaleTimeString('ru-RU')),
        );
        callbacks.appendChild(callbackItem);
      }

      card.append(top, meta, callbacks);
      nodes.runDetail.appendChild(card);
    }
  }

  function renderAskUser() {
    const waiting = selectedWaitingCase();
    const disabled = !waiting;
    nodes.askQuestion.textContent = waiting?.waiting_question || 'Нет ожидающего вопроса.';
    nodes.replyCaseId.value = waiting?.eval_case_id || '';
    nodes.replySessionId.value = waiting?.session_id || '';
    nodes.replyId.value = waiting?.waiting_reply_id || '';
    nodes.replyMessage.disabled = disabled;
    nodes.askForm.querySelector('button[type="submit"]').disabled = disabled;
  }

  function renderEvaluations() {
    nodes.evaluationsBody.replaceChildren();
    nodes.evaluationsEmpty.style.display = state.evaluations.length ? 'none' : 'block';

    for (const item of state.evaluations) {
      const row = document.createElement('tr');
      const cells = [
        item.eval_case_id,
        item.host,
        statusLabel(item.runtime_status),
        formatList(item.checks),
        formatMs(item.duration_ms),
        formatNumber(item.buyer_tokens_used),
        formatNumber(item.recommendations_count),
        formatList(item.artifacts),
      ];
      for (const cell of cells) {
        const td = document.createElement('td');
        td.textContent = cell;
        row.appendChild(td);
      }
      nodes.evaluationsBody.appendChild(row);
    }
  }

  function lineChart(values) {
    const normalized = seriesValues(values);
    const chart = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chart.classList.add('eval-line-chart');
    chart.setAttribute('viewBox', '0 0 120 48');
    chart.setAttribute('preserveAspectRatio', 'none');
    chart.setAttribute('aria-hidden', 'true');

    const grid = document.createElementNS(SVG_NS, 'g');
    grid.classList.add('eval-line-grid');
    for (const y of [12, 24, 36]) {
      const line = document.createElementNS(SVG_NS, 'line');
      line.setAttribute('x1', '0');
      line.setAttribute('x2', '120');
      line.setAttribute('y1', String(y));
      line.setAttribute('y2', String(y));
      grid.appendChild(line);
    }
    chart.appendChild(grid);

    if (!normalized.length) {
      return chart;
    }

    const min = Math.min(...normalized);
    const max = Math.max(...normalized);
    const spread = max - min || 1;
    const step = normalized.length > 1 ? 120 / (normalized.length - 1) : 120;
    const points = normalized
      .map((value, index) => {
        const x = normalized.length === 1 ? 60 : index * step;
        const y = 42 - ((value - min) / spread) * 36;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');

    const polyline = document.createElementNS(SVG_NS, 'polyline');
    polyline.classList.add('eval-line-path');
    polyline.setAttribute('points', points);
    chart.appendChild(polyline);

    return chart;
  }

  function dashboardItem(item, titleKey) {
    const card = node('div', 'eval-dashboard-item');
    const top = node('div', 'eval-dashboard-top');
    top.append(
      node('strong', null, item[titleKey]),
      node('span', `eval-status ${statusClass(item.status)}`, statusLabel(item.status)),
    );

    const metrics = node('div', 'eval-dashboard-metrics');
    metrics.append(
      node('span', null, `duration ${formatMs(item.baseline_duration_ms || lastSeriesValue(item.duration_ms))}`),
      node('span', null, `tokens ${formatNumber(item.baseline_tokens || lastSeriesValue(item.buyer_tokens_used))}`),
      node('span', null, item.success_rate ? `ok ${item.success_rate}` : ''),
    );

    const charts = node('div', 'eval-dashboard-charts');
    const duration = node('div', 'eval-chart-block');
    duration.append(node('span', null, 'duration_ms'), lineChart(item.duration_ms));
    const tokens = node('div', 'eval-chart-block');
    tokens.append(node('span', null, 'buyer_tokens_used'), lineChart(item.buyer_tokens_used));
    charts.append(duration, tokens);

    card.append(top, metrics, charts);
    return card;
  }

  function renderDashboard(target, rows, titleKey) {
    target.replaceChildren();
    const items = asArray(rows);
    if (!items.length) {
      target.appendChild(node('div', 'empty', 'Данных dashboard пока нет.'));
      return;
    }
    for (const item of items) {
      target.appendChild(dashboardItem(item, titleKey));
    }
  }

  async function loadDashboards() {
    const [caseRows, hostRows] = await Promise.all([
      evalRequest(CONTRACT_PATHS.caseDashboard),
      evalRequest(CONTRACT_PATHS.hostDashboard),
    ]);
    renderDashboard(nodes.caseDashboard, extractList(caseRows, ['rows', 'cases', 'items']), 'eval_case_id');
    renderDashboard(nodes.hostDashboard, extractList(hostRows, ['rows', 'hosts', 'items']), 'host');
  }

  function renderAll() {
    renderMetrics();
    renderRunDetail();
    renderAskUser();
    renderEvaluations();
  }

  nodes.caseForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    nodes.startResult.textContent = 'Запуск…';

    try {
      const caseIds = getSelectedCaseIds();
      const data = await evalRequest(CONTRACT_PATHS.createRun, {
        payload: {
          case_ids: caseIds,
        },
      });
      state.activeRun = normalizeRun(data);
      state.evaluations = [];
      if (state.activeRun.eval_run_id && window.EVAL_SERVICE_BASE_URL) {
        await loadRunDetail(state.activeRun.eval_run_id);
      }
      nodes.startResult.textContent = JSON.stringify(
        {
          eval_run_id: state.activeRun.eval_run_id,
          status: state.activeRun.status,
          cases: asArray(state.activeRun.cases).length,
        },
        null,
        2,
      );
      renderAll();
    } catch (error) {
      nodes.startResult.textContent = String(error.message || error);
    }
  });

  nodes.askForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const waiting = selectedWaitingCase();
    const message = nodes.replyMessage.value.trim();
    if (!waiting || !message) {
      return;
    }

    try {
      const data = await evalRequest(CONTRACT_PATHS.reply, {
        evalRunId: state.activeRun.eval_run_id,
        evalCaseId: waiting.eval_case_id,
        payload: {
          reply_id: waiting.waiting_reply_id,
          message,
        },
      });
      nodes.replyResult.textContent = JSON.stringify(data, null, 2);
      nodes.replyMessage.value = '';
      if (state.activeRun?.eval_run_id && window.EVAL_SERVICE_BASE_URL) {
        await loadRunDetail(state.activeRun.eval_run_id);
      } else {
        waiting.runtime_status = 'running';
        waiting.callbacks.push({
          event_id: `${state.activeRun.eval_run_id}-operator-reply`,
          event_type: 'operator_reply',
          occurred_at: nowIso(),
          session_id: waiting.session_id,
          reply_id: waiting.waiting_reply_id,
        });
        waiting.waiting_reply_id = null;
        waiting.waiting_question = null;
      }
      renderAll();
    } catch (error) {
      nodes.replyResult.textContent = String(error.message || error);
    }
  });

  nodes.runJudge.addEventListener('click', async () => {
    if (!state.activeRun) {
      return;
    }
    nodes.judgeResult.textContent = 'Запуск…';
    try {
      const data = await evalRequest(CONTRACT_PATHS.judge, {
        evalRunId: state.activeRun.eval_run_id,
      });
      state.activeRun.status = data.status || 'judge_pending';
      state.evaluations = extractEvaluations(data);
      nodes.judgeResult.textContent = JSON.stringify(data, null, 2);
      renderAll();
    } catch (error) {
      nodes.judgeResult.textContent = String(error.message || error);
    }
  });

  async function init() {
    try {
      state.cases = extractList(await evalRequest(CONTRACT_PATHS.cases), ['cases', 'items']);
      renderCases();
      await loadDashboards();
      renderAll();
    } catch (error) {
      nodes.casesList.replaceChildren(node('div', 'empty', String(error.message || error)));
      renderAll();
    }
  }

  window.MicroUiEval = {
    contracts: CONTRACT_PATHS,
  };

  init();
})();
