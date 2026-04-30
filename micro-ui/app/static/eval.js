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
  const RUN_REFRESH_INTERVAL_MS = 2000;

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
    runRefreshTimer: null,
    judgePollingActive: false,
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

  function runTimestamp(run) {
    const raw = run?.updated_at || run?.created_at || '';
    const value = Date.parse(raw);
    return Number.isFinite(value) ? value : 0;
  }

  function latestRun(runs) {
    return asArray(runs)
      .slice()
      .sort((left, right) => runTimestamp(right) - runTimestamp(left))[0] || null;
  }

  async function loadLatestRun() {
    const data = await evalRequest(CONTRACT_PATHS.runs);
    const run = latestRun(extractList(data, ['runs', 'items']));
    if (!run?.eval_run_id) {
      return null;
    }
    await loadRunDetail(run.eval_run_id);
    return state.activeRun;
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

  function shouldRefreshRun(run = state.activeRun) {
    if (!window.EVAL_SERVICE_BASE_URL || !run?.eval_run_id) {
      return false;
    }
    const cases = asArray(run.cases);
    if (cases.some((item) => item.runtime_status === 'waiting_user')) {
      return false;
    }
    if (state.judgePollingActive) {
      return true;
    }
    if (hasJudgePending(run)) {
      return true;
    }
    if (run.status !== 'running') {
      return false;
    }
    return cases.some((item) =>
      ['pending', 'starting', 'running', 'payment_ready'].includes(item.runtime_status),
    );
  }

  function stopRunRefresh() {
    if (state.runRefreshTimer !== null) {
      window.clearTimeout(state.runRefreshTimer);
      state.runRefreshTimer = null;
    }
  }

  function scheduleRunRefresh() {
    stopRunRefresh();
    if (!shouldRefreshRun()) {
      return;
    }
    state.runRefreshTimer = window.setTimeout(async () => {
      state.runRefreshTimer = null;
      if (!state.activeRun?.eval_run_id) {
        return;
      }
      try {
        await loadRunDetail(state.activeRun.eval_run_id);
        updateJudgeProgressFromRun();
        renderAll();
      } catch (error) {
        nodes.startResult.textContent = String(error.message || error);
      }
      scheduleRunRefresh();
    }, RUN_REFRESH_INTERVAL_MS);
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

  function hasJudgePending(run = state.activeRun) {
    return asArray(run?.cases).some((item) => item.runtime_status === 'judge_pending');
  }

  function judgeProgressPayload(status = 'judge_pending') {
    const cases = asArray(state.activeRun?.cases);
    return {
      eval_run_id: state.activeRun?.eval_run_id || null,
      status,
      evaluations: state.evaluations.length,
      cases: cases.map((item) => ({
        eval_case_id: item.eval_case_id,
        status: item.runtime_status,
      })),
    };
  }

  function updateJudgeProgressFromRun() {
    const currentText = nodes.judgeResult.textContent.trim();
    const ownsResult = currentText.startsWith('Запуск') || currentText.startsWith('Judge') || currentText.startsWith('{');
    if (!ownsResult) {
      return;
    }
    if (hasJudgePending()) {
      nodes.judgeResult.textContent = JSON.stringify(judgeProgressPayload('judge_pending'), null, 2);
      return;
    }
    const hasFinalJudgeState = asArray(state.activeRun?.cases).some((item) =>
      ['judged', 'judge_failed'].includes(item.runtime_status),
    );
    if (state.evaluations.length || (state.judgePollingActive && hasFinalJudgeState)) {
      state.judgePollingActive = false;
      const status = state.evaluations.some((item) => item.status === 'judge_failed') ? 'judge_failed' : 'judged';
      nodes.judgeResult.textContent = JSON.stringify(
        {
          eval_run_id: state.activeRun?.eval_run_id,
          status,
          evaluations: state.evaluations,
        },
        null,
        2,
      );
    }
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

  function truncateText(value, limit = 140) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  }

  function callbackData(callback) {
    return {
      ...(callback || {}),
      ...(callback?.details || {}),
      ...(callback?.payload || {}),
    };
  }

  function callbackTime(callback) {
    const value = Date.parse(callback?.occurred_at || '');
    return Number.isFinite(value) ? new Date(value).toLocaleTimeString('ru-RU') : '-';
  }

  function streamSource(callback) {
    const data = callbackData(callback);
    return {
      source: data.source || callback?.source || '-',
      stream: data.stream || callback?.stream || 'default',
    };
  }

  function callbackMessage(callback) {
    const data = callbackData(callback);
    const direct = data.message || data.summary || data.text || data.action || data.command || callback?.message;
    if (direct) {
      return truncateText(direct);
    }

    const items = asArray(data.items);
    const item = items[items.length - 1] || {};
    const itemText = item.message || item.summary || item.action || item.command || item.type;
    return truncateText(itemText || callback?.event_id || 'без сообщения');
  }

  function streamEventSummaries(callbacks) {
    const summaries = [];
    for (const callback of callbacks) {
      const message = callbackMessage(callback);
      const last = summaries[summaries.length - 1];
      if (last?.message === message) {
        last.count += 1;
        last.time = callbackTime(callback);
      } else {
        summaries.push({ message, count: 1, time: callbackTime(callback) });
      }
    }
    return summaries.slice(-3);
  }

  function groupRunCallbacks(callbacks) {
    const groups = [];
    const streamGroups = new Map();
    for (const callback of callbacks) {
      if (callback.event_type === 'agent_stream_event') {
        const { source, stream } = streamSource(callback);
        const key = `${source}\u0000${stream}`;
        let group = streamGroups.get(key);
        if (!group) {
          group = { kind: 'agent_stream_event', source, stream, callbacks: [] };
          streamGroups.set(key, group);
          groups.push(group);
        }
        group.callbacks.push(callback);
      } else {
        groups.push({ kind: 'callback', callbacks: [callback] });
      }
    }
    return groups;
  }

  function renderCallbackRaw(callbacks) {
    const details = document.createElement('details');
    details.className = 'eval-callback-raw';
    details.appendChild(node('summary', null, 'raw payload/details'));
    const raw = node('pre', null, JSON.stringify(callbacks.length === 1 ? callbacks[0] : callbacks, null, 2));
    details.appendChild(raw);
    return details;
  }

  function renderCallbackItem(callback) {
    const callbackItem = node('div', 'eval-callback-item');
    callbackItem.append(
      node('span', 'code', callback.event_type || 'callback'),
      node('span', null, callback.event_id || callbackMessage(callback)),
      node('span', null, callbackTime(callback)),
    );
    if (callback.payload || callback.details) {
      callbackItem.appendChild(renderCallbackRaw([callback]));
    }
    return callbackItem;
  }

  function renderAgentStreamGroup(group) {
    const wrapper = node('div', 'eval-agent-stream-group');
    const summary = node('div', 'eval-agent-stream-summary');
    summary.append(
      node('span', 'code', 'agent_stream_event'),
      node('span', null, `source/stream: ${group.source}/${group.stream}`),
      node('span', 'eval-agent-stream-count', `${group.callbacks.length} events`),
    );

    const events = node('div', 'eval-agent-stream-events');
    events.appendChild(node('strong', null, 'последние события'));
    for (const item of streamEventSummaries(group.callbacks)) {
      const suffix = item.count > 1 ? ` ×${item.count}` : '';
      events.appendChild(node('span', null, `${item.time} · ${item.message}${suffix}`));
    }

    wrapper.append(summary, events, renderCallbackRaw(group.callbacks));
    return wrapper;
  }

  async function submitReply(evalRunId, evalCaseId, replyId, message, resultEl) {
    try {
      const data = await evalRequest(CONTRACT_PATHS.reply, {
        evalRunId,
        evalCaseId,
        payload: { reply_id: replyId, message },
      });
      if (resultEl) resultEl.textContent = JSON.stringify(data, null, 2);
      if (!window.EVAL_SERVICE_BASE_URL) {
        const caseItem = asArray(state.activeRun?.cases).find((c) => c.eval_case_id === evalCaseId);
        if (caseItem) {
          caseItem.runtime_status = 'running';
          caseItem.callbacks.push({
            event_id: `${evalRunId}-operator-reply`,
            event_type: 'operator_reply',
            occurred_at: nowIso(),
            session_id: caseItem.session_id,
            reply_id: replyId,
          });
          caseItem.waiting_reply_id = null;
          caseItem.waiting_question = null;
        }
      } else if (state.activeRun?.eval_run_id) {
        await loadRunDetail(state.activeRun.eval_run_id);
      }
      renderAll();
      scheduleRunRefresh();
    } catch (error) {
      if (resultEl) resultEl.textContent = String(error.message || error);
    }
  }

  function renderRunDetail() {
    nodes.runDetail.replaceChildren();
    nodes.runJudge.disabled = !state.activeRun || hasJudgePending(state.activeRun);

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
      const isWaiting = item.runtime_status === 'waiting_user';
      const card = node('div', `eval-run-case${isWaiting ? ' waiting' : ''}`);

      // Compact single row: status badge | title | host chip | N events chip
      const row = node('div', 'eval-run-case-row');
      const titleSpan = node('span', 'eval-run-case-title', item.title);
      row.append(
        node('span', `eval-status ${statusClass(item.runtime_status)}`, statusLabel(item.runtime_status)),
        titleSpan,
        node('span', 'eval-chip', item.host),
      );
      const cbCount = asArray(item.callbacks).length;
      if (cbCount > 0) {
        row.appendChild(node('span', 'eval-chip code', `${cbCount} ev`));
      }
      card.appendChild(row);

      // IDs row
      const idsRow = node('div', 'eval-run-case-ids');
      idsRow.appendChild(node('span', 'code', item.eval_case_id));
      if (item.session_id) {
        idsRow.appendChild(node('span', 'code', item.session_id));
      }
      card.appendChild(idsRow);

      // Callbacks as collapsible details
      if (cbCount > 0) {
        const details = document.createElement('details');
        details.className = 'eval-callbacks-details';
        details.appendChild(node('summary', 'eval-callbacks-summary', `${cbCount} callbacks`));
        const cbList = node('div', 'eval-callbacks');
        for (const group of groupRunCallbacks(asArray(item.callbacks))) {
          cbList.appendChild(
            group.kind === 'agent_stream_event' ? renderAgentStreamGroup(group) : renderCallbackItem(group.callbacks[0]),
          );
        }
        details.appendChild(cbList);
        card.appendChild(details);
      }

      // Inline reply for waiting_user
      if (isWaiting && item.waiting_reply_id) {
        const replyBlock = node('div', 'eval-inline-reply');
        if (item.waiting_question) {
          replyBlock.appendChild(node('p', 'eval-inline-reply-question', item.waiting_question));
        }
        const textarea = document.createElement('textarea');
        textarea.rows = 2;
        textarea.className = 'eval-inline-reply-textarea';
        textarea.placeholder = 'Ответ оператора…';
        const submitBtn = node('button', 'primary-action eval-inline-reply-submit', 'Ответить');
        const resultPre = node('pre', 'result');
        const controls = node('div', 'eval-inline-reply-controls');
        controls.append(textarea, submitBtn);
        replyBlock.append(controls, resultPre);
        submitBtn.addEventListener('click', async () => {
          const message = textarea.value.trim();
          if (!message || !state.activeRun) return;
          submitBtn.disabled = true;
          await submitReply(state.activeRun.eval_run_id, item.eval_case_id, item.waiting_reply_id, message, resultPre);
          submitBtn.disabled = false;
        });
        card.appendChild(replyBlock);
      }

      nodes.runDetail.appendChild(card);
    }
  }

  function makeTags(value, separator = ',') {
    const items = Array.isArray(value)
      ? value.map(String)
      : String(value || '')
          .split(separator)
          .map((s) => s.trim())
          .filter(Boolean);
    const wrap = document.createElement('span');
    if (!items.length) {
      wrap.textContent = '-';
      return wrap;
    }
    for (const text of items) {
      wrap.appendChild(node('span', 'eval-tag', text));
    }
    return wrap;
  }

  function renderEvaluations() {
    nodes.evaluationsBody.replaceChildren();
    nodes.evaluationsEmpty.style.display = state.evaluations.length ? 'none' : 'block';

    for (const item of state.evaluations) {
      const row = document.createElement('tr');

      // Status badge
      const statusTd = document.createElement('td');
      statusTd.appendChild(
        node('span', `eval-status ${statusClass(item.runtime_status)}`, statusLabel(item.runtime_status)),
      );

      // Case ID
      const caseTd = document.createElement('td');
      caseTd.appendChild(node('span', 'code', item.eval_case_id));

      // Host
      const hostTd = document.createElement('td');
      hostTd.textContent = item.host || '-';

      // Duration with color
      const durMs = Number(item.duration_ms || 0);
      const durClass = durMs > 0 && durMs < 100000 ? 'eval-dur-ok' : durMs < 150000 ? 'eval-dur-warn' : 'eval-dur-slow';
      const durTd = document.createElement('td');
      durTd.className = durClass;
      durTd.textContent = formatMs(item.duration_ms);

      // Tokens
      const tokensTd = document.createElement('td');
      tokensTd.textContent = formatNumber(item.buyer_tokens_used);

      // Checks as tags
      const checksTd = document.createElement('td');
      checksTd.appendChild(makeTags(item.checks));

      // Recommendations
      const recCount = Number(item.recommendations_count || 0);
      const recTd = document.createElement('td');
      recTd.className = recCount > 0 ? 'eval-rec-warn' : '';
      recTd.textContent = String(recCount || '-');

      // Artifacts as tags
      const artifactsTd = document.createElement('td');
      artifactsTd.appendChild(makeTags(item.artifacts));

      row.append(statusTd, caseTd, hostTd, durTd, tokensTd, checksTd, recTd, artifactsTd);
      nodes.evaluationsBody.appendChild(row);
    }
  }

  function lineChart(values, formatFn) {
    const normalized = seriesValues(values);
    const fmt = typeof formatFn === 'function' ? formatFn : String;

    const wrapper = document.createElement('div');
    wrapper.className = 'eval-chart-wrap';

    const chart = document.createElementNS(SVG_NS, 'svg');
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
      wrapper.appendChild(chart);
      return wrapper;
    }

    const min = Math.min(...normalized);
    const max = Math.max(...normalized);
    const spread = max - min || 1;
    const step = normalized.length > 1 ? 120 / (normalized.length - 1) : 120;

    const pts = normalized.map((v, i) => ({
      x: normalized.length === 1 ? 60 : i * step,
      y: 42 - ((v - min) / spread) * 36,
      value: v,
    }));

    const polyline = document.createElementNS(SVG_NS, 'polyline');
    polyline.classList.add('eval-line-path');
    polyline.setAttribute('points', pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '));
    chart.appendChild(polyline);

    // Hover dot
    const dot = document.createElementNS(SVG_NS, 'circle');
    dot.setAttribute('r', '3.5');
    dot.classList.add('eval-chart-dot');
    dot.style.display = 'none';
    chart.appendChild(dot);

    // Tooltip
    const tooltip = document.createElement('div');
    tooltip.className = 'eval-chart-tooltip';
    tooltip.style.display = 'none';

    chart.addEventListener('mousemove', (e) => {
      const svgRect = chart.getBoundingClientRect();
      if (!svgRect.width) return;
      const mouseX = ((e.clientX - svgRect.left) / svgRect.width) * 120;
      let closest = pts[0];
      let closestDist = Infinity;
      for (const p of pts) {
        const dist = Math.abs(mouseX - p.x);
        if (dist < closestDist) {
          closestDist = dist;
          closest = p;
        }
      }
      dot.setAttribute('cx', closest.x.toFixed(1));
      dot.setAttribute('cy', closest.y.toFixed(1));
      dot.style.display = '';
      tooltip.textContent = fmt(closest.value);
      tooltip.style.display = '';
      const wrapperRect = wrapper.getBoundingClientRect();
      const dotScreenX = (closest.x / 120) * svgRect.width + svgRect.left - wrapperRect.left;
      const dotScreenY = (closest.y / 48) * svgRect.height + svgRect.top - wrapperRect.top;
      const tw = tooltip.offsetWidth || 52;
      const tipLeft = Math.max(0, Math.min(dotScreenX - tw / 2, wrapperRect.width - tw));
      tooltip.style.left = `${tipLeft}px`;
      tooltip.style.top = `${Math.max(0, dotScreenY - 28)}px`;
    });

    chart.addEventListener('mouseleave', () => {
      dot.style.display = 'none';
      tooltip.style.display = 'none';
    });

    // Y-axis min/max labels
    if (normalized.length >= 2 && min !== max) {
      const axMax = document.createElement('span');
      axMax.className = 'eval-chart-axis-max';
      axMax.textContent = fmt(max);
      const axMin = document.createElement('span');
      axMin.className = 'eval-chart-axis-min';
      axMin.textContent = fmt(min);
      wrapper.append(axMax, axMin);
    }

    wrapper.append(chart, tooltip);
    return wrapper;
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
    duration.append(node('span', null, 'duration'), lineChart(item.duration_ms, formatMs));
    const tokens = node('div', 'eval-chart-block');
    tokens.append(node('span', null, 'tokens'), lineChart(item.buyer_tokens_used, formatNumber));
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
      scheduleRunRefresh();
    } catch (error) {
      nodes.startResult.textContent = String(error.message || error);
    }
  });

  nodes.runJudge.addEventListener('click', async () => {
    if (!state.activeRun) {
      return;
    }
    state.judgePollingActive = true;
    nodes.judgeResult.textContent = 'Запуск…';
    try {
      const data = await evalRequest(CONTRACT_PATHS.judge, {
        evalRunId: state.activeRun.eval_run_id,
        payload: {
          async: true,
        },
      });
      state.activeRun.status = data.status || 'judge_pending';
      state.evaluations = extractEvaluations(data);
      if (state.activeRun.eval_run_id && window.EVAL_SERVICE_BASE_URL) {
        await loadRunDetail(state.activeRun.eval_run_id);
      }
      updateJudgeProgressFromRun();
      if (!nodes.judgeResult.textContent.trim() || nodes.judgeResult.textContent.trim() === 'Запуск…') {
        nodes.judgeResult.textContent = JSON.stringify(data, null, 2);
      }
      renderAll();
      scheduleRunRefresh();
    } catch (error) {
      state.judgePollingActive = false;
      nodes.judgeResult.textContent = String(error.message || error);
    }
  });

  async function init() {
    try {
      state.cases = extractList(await evalRequest(CONTRACT_PATHS.cases), ['cases', 'items']);
      renderCases();
      await loadLatestRun();
      await loadDashboards();
      renderAll();
      scheduleRunRefresh();
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
