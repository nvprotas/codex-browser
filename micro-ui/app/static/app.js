const statusNode = document.getElementById('global-status');
const sessionsNode = document.getElementById('sessions-list');
const sessionsEmptyNode = document.getElementById('sessions-empty');
const sessionsCountNode = document.getElementById('sessions-count');
const eventsNode = document.getElementById('events-list');
const eventsEmptyNode = document.getElementById('events-empty');
const eventsCountNode = document.getElementById('events-count');
const eventTypeFiltersNode = document.getElementById('event-type-filters');
const noVncFrame = document.getElementById('novnc-frame');
const noVncSessionLabel = document.getElementById('novnc-session-label');
const noVncPlaceholderNode = document.getElementById('novnc-placeholder');
const tabButtons = document.querySelectorAll('[data-tab-target]');
const tabPanels = document.querySelectorAll('[data-tab-panel]');

const metricSessionsNode = document.getElementById('metric-sessions');
const metricWaitingNode = document.getElementById('metric-waiting');
const metricOrdersNode = document.getElementById('metric-orders');
const metricErrorsNode = document.getElementById('metric-errors');

const taskForm = document.getElementById('task-form');
const taskTextInput = document.getElementById('task-text');
const taskStartUrlInput = document.getElementById('task-start-url');
const taskMetadataInput = document.getElementById('task-metadata');
const taskAuthInput = document.getElementById('task-auth');
const taskResultNode = document.getElementById('task-result');

const replyForm = document.getElementById('reply-form');
const replySessionIdInput = document.getElementById('reply-session-id');
const replyIdInput = document.getElementById('reply-id');
const replyMessageInput = document.getElementById('reply-message');
const replyResultNode = document.getElementById('reply-result');
const agentQuestionNode = document.getElementById('agent-question');
const agentQuestionTextNode = document.getElementById('agent-question-text');
const agentQuestionTsNode = document.getElementById('agent-question-ts');
const agentQuestionOptionsNode = document.getElementById('agent-question-options');
const replyEmptyNode = document.getElementById('reply-empty');
const replyStateBadgeNode = document.getElementById('reply-state-badge');

const KNOWN_EVENT_TYPES = [
  'session_started',
  'agent_step_started',
  'agent_stream_event',
  'agent_step_finished',
  'ask_user',
  'handoff_requested',
  'handoff_resumed',
  'payment_ready',
  'scenario_finished',
];

let selectedSessionId = null;
let selectedSession = null;
let pendingSessionSelectionId = null;
let selectedEventTypeFilters = new Set(KNOWN_EVENT_TYPES);
let selectedEvents = [];
let seenEventIds = new Set();
let eventSource = null;
let streamNeedsRecovery = false;
const noVncBlank = `<!doctype html>
<html lang="ru">
  <body style="margin:0;min-height:100vh;background:#0d0f12;"></body>
</html>`;

function activateTab(panelId) {
  for (const panel of tabPanels) {
    const active = panel.id === panelId;
    panel.hidden = !active;
    panel.classList.toggle('active', active);
  }

  for (const button of tabButtons) {
    const active = button.dataset.tabTarget === panelId;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  }
}

for (const button of tabButtons) {
  button.addEventListener('click', () => {
    activateTab(button.dataset.tabTarget);
  });
}

function normalizeUrl(url) {
  if (!url) {
    return 'about:blank';
  }
  try {
    return new URL(url, window.location.origin).toString();
  } catch {
    return String(url);
  }
}

function setNoVncUrl(url) {
  if (!url) {
    noVncFrame.removeAttribute('src');
    noVncFrame.srcdoc = noVncBlank;
    noVncPlaceholderNode.hidden = false;
    return;
  }

  const next = normalizeUrl(url);
  const current = normalizeUrl(noVncFrame.getAttribute('src') || noVncFrame.src);
  if (current === next) {
    noVncPlaceholderNode.hidden = true;
    return;
  }
  noVncFrame.removeAttribute('srcdoc');
  noVncPlaceholderNode.hidden = true;
  noVncFrame.src = next;
}

function setNoVncLabel(session) {
  noVncSessionLabel.textContent = session?.session_id
    ? `session.novnc_url · ${shortId(session.session_id)}`
    : 'session.novnc_url';
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
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

function fmtDate(value) {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString('ru-RU');
}

function formatMetric(value) {
  return String(value).padStart(2, '0');
}

function shortId(value, maxLength = 18) {
  const text = String(value || '');
  return text ? text.slice(0, maxLength) : '-';
}

function shortenText(value, maxLength = 180) {
  const text = String(value || '');
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
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

function tokenizeJson(source) {
  const tokens = [];
  let i = 0;
  const text = String(source || '');

  while (i < text.length) {
    const character = text[i];

    if (/\s/.test(character)) {
      let j = i;
      while (j < text.length && /\s/.test(text[j])) {
        j += 1;
      }
      tokens.push({ type: 'ws', value: text.slice(i, j) });
      i = j;
      continue;
    }

    if (character === '"') {
      let j = i + 1;
      while (j < text.length) {
        if (text[j] === '\\') {
          j += 2;
          continue;
        }
        if (text[j] === '"') {
          j += 1;
          break;
        }
        j += 1;
      }

      let k = j;
      while (k < text.length && /\s/.test(text[k])) {
        k += 1;
      }
      tokens.push({ type: text[k] === ':' ? 'key' : 'string', value: text.slice(i, j) });
      i = j;
      continue;
    }

    if (/[-0-9]/.test(character)) {
      let j = i;
      if (text[j] === '-') {
        j += 1;
      }
      while (j < text.length && /[0-9.eE+-]/.test(text[j])) {
        j += 1;
      }
      tokens.push({ type: 'num', value: text.slice(i, j) });
      i = j;
      continue;
    }

    if (/[a-zA-Z]/.test(character)) {
      let j = i;
      while (j < text.length && /[a-zA-Z]/.test(text[j])) {
        j += 1;
      }
      const word = text.slice(i, j);
      if (word === 'true' || word === 'false') {
        tokens.push({ type: 'bool', value: word });
      } else if (word === 'null') {
        tokens.push({ type: 'null', value: word });
      } else {
        tokens.push({ type: 'ident', value: word });
      }
      i = j;
      continue;
    }

    if (character === '/' && text[i + 1] === '/') {
      let j = i;
      while (j < text.length && text[j] !== '\n') {
        j += 1;
      }
      tokens.push({ type: 'comment', value: text.slice(i, j) });
      i = j;
      continue;
    }

    tokens.push({ type: 'punct', value: character });
    i += 1;
  }

  return tokens;
}

function tokenClassName(type) {
  const classes = {
    key: 'tok-key',
    string: 'tok-string',
    num: 'tok-num',
    bool: 'tok-bool',
    null: 'tok-null',
    comment: 'tok-comment',
    punct: 'tok-punct',
    ident: 'tok-ident',
  };
  return classes[type] || '';
}

function appendHighlightedTokens(parent, tokens) {
  for (const token of tokens) {
    if (token.type === 'ws') {
      parent.append(document.createTextNode(token.value));
      continue;
    }
    const span = node('span', tokenClassName(token.type), token.value);
    parent.append(span);
  }
}

function splitTokensByLine(tokens) {
  const lines = [[]];

  for (const token of tokens) {
    if (token.type === 'ws' && token.value.includes('\n')) {
      const parts = token.value.split('\n');
      if (parts[0]) {
        lines[lines.length - 1].push({ type: 'ws', value: parts[0] });
      }
      for (let i = 1; i < parts.length; i += 1) {
        lines.push([]);
        if (parts[i]) {
          lines[lines.length - 1].push({ type: 'ws', value: parts[i] });
        }
      }
      continue;
    }
    lines[lines.length - 1].push(token);
  }

  if (lines.length > 1 && lines[lines.length - 1].length === 0) {
    lines.pop();
  }
  return lines;
}

function renderHighlightedJson(editor) {
  const textarea = editor.querySelector('.json-textarea');
  const highlight = editor.querySelector('.json-highlight code');
  if (!textarea || !highlight) {
    return;
  }

  highlight.replaceChildren();
  appendHighlightedTokens(highlight, tokenizeJson(textarea.value));
  highlight.append(document.createTextNode('\n'));
}

function renderJsonEditorState(editor) {
  const textarea = editor.querySelector('.json-textarea');
  const state = editor.querySelector('.json-editor-ok, .json-editor-error');
  if (!textarea || !state) {
    return;
  }

  const value = textarea.value.trim();
  let error = null;
  if (value) {
    try {
      JSON.parse(value);
    } catch (caught) {
      error = caught.message.replace(/^JSON\.parse: /, '');
    }
  }

  editor.classList.toggle('has-error', Boolean(error));
  state.className = error ? 'json-editor-error' : 'json-editor-ok';
  state.textContent = error ? `error: ${error}` : 'parsed ok';
}

function syncJsonEditor(editor) {
  renderHighlightedJson(editor);
  renderJsonEditorState(editor);
}

function setupJsonEditors() {
  document.querySelectorAll('[data-json-editor]').forEach((editor) => {
    const textarea = editor.querySelector('.json-textarea');
    const highlight = editor.querySelector('.json-highlight');
    if (!textarea || !highlight) {
      return;
    }

    textarea.addEventListener('input', () => syncJsonEditor(editor));
    textarea.addEventListener('scroll', () => {
      highlight.scrollTop = textarea.scrollTop;
      highlight.scrollLeft = textarea.scrollLeft;
    });
    syncJsonEditor(editor);
  });
}

function createJsonView(value) {
  let text = '';
  if (typeof value === 'string') {
    text = value;
  } else {
    try {
      text = JSON.stringify(value, null, 2);
    } catch {
      text = String(value);
    }
  }

  const pre = node('pre', 'json-view');
  const codeNode = node('code');
  const lines = splitTokensByLine(tokenizeJson(text));

  lines.forEach((lineTokens, lineIndex) => {
    const row = node('span', 'json-view-row');
    row.append(
      node('span', 'json-view-ln', String(lineIndex + 1)),
      node('span', 'json-view-content'),
    );
    appendHighlightedTokens(row.lastChild, lineTokens);
    codeNode.append(row);
  });

  pre.append(codeNode);
  return pre;
}

function meta(label, value) {
  const item = node('div', 'session-meta');
  const labelNode = document.createTextNode(`${label}: `);
  const valueNode = node('span', 'code', value || '-');
  item.append(labelNode, valueNode);
  return item;
}

function statusClass(status) {
  const normalized = String(status || 'running').toLowerCase().replace(/[^a-z0-9_-]/g, '_');
  const known = new Set(['running', 'waiting_user', 'failed', 'error', 'completed', 'success']);
  return known.has(normalized) ? normalized : 'unknown';
}

function isWaitingSession(session) {
  return session.status === 'waiting_user' || Boolean(session.waiting_reply_id);
}

function isErrorSession(session) {
  const status = String(session.status || '').toLowerCase();
  const eventType = String(session.last_event_type || '').toLowerCase();
  return status === 'failed' || status === 'error' || eventType.includes('failed') || eventType.includes('error');
}

function updateMetrics(sessions) {
  metricSessionsNode.textContent = formatMetric(sessions.length);
  metricWaitingNode.textContent = formatMetric(sessions.filter(isWaitingSession).length);
  metricOrdersNode.textContent = formatMetric(sessions.filter((item) => item.order_id).length);
  metricErrorsNode.textContent = formatMetric(sessions.filter(isErrorSession).length);
  sessionsCountNode.textContent = `${sessions.length} ACTIVE`;
}

function createSessionItem(session, sessions) {
  const item = node('button', `session-item ${session.session_id === selectedSessionId ? 'active' : ''}`);
  item.type = 'button';

  const top = node('div', 'session-top');
  const sessionIdNode = node('span', 'code', shortId(session.session_id));
  sessionIdNode.title = session.session_id || '';
  top.append(
    sessionIdNode,
    node('span', `badge ${statusClass(session.status)}`, session.status || 'running'),
  );

  const eventMeta = node('div', 'session-meta');
  eventMeta.append('Последнее событие: ', node('strong', null, session.last_event_type || '-'));

  const message = node('div', 'session-message', shortenText(session.last_message || 'Без сообщения'));

  const metaGrid = node('div', 'session-meta-grid');
  metaGrid.append(
    meta('reply_id', session.waiting_reply_id),
    meta('order_id', session.order_id),
    meta('Обновлено', fmtDate(session.updated_at)),
  );

  item.append(top, eventMeta, message, metaGrid);
  item.addEventListener('click', () => {
    selectedSessionId = session.session_id;
    selectedSession = session;
    hydrateReplyForm();
    setNoVncUrl(session.novnc_url);
    setNoVncLabel(session);
    renderSessions(sessions);
    refreshEvents().catch(showError);
  });

  return item;
}

function renderSessions(sessions) {
  if (pendingSessionSelectionId) {
    const created = sessions.find((item) => item.session_id === pendingSessionSelectionId) || null;
    if (created) {
      selectedSessionId = created.session_id;
      selectedSession = created;
      pendingSessionSelectionId = null;
    }
  }

  if (selectedSessionId) {
    const refreshed = sessions.find((item) => item.session_id === selectedSessionId) || null;
    selectedSession = refreshed;
    if (!refreshed) {
      selectedSessionId = null;
    }
  }

  sessionsNode.replaceChildren();
  sessionsEmptyNode.style.display = sessions.length ? 'none' : 'block';
  updateMetrics(sessions);

  for (const session of sessions) {
    sessionsNode.appendChild(createSessionItem(session, sessions));
  }

  if (!selectedSessionId && sessions.length) {
    selectedSessionId = sessions[0].session_id;
    selectedSession = sessions[0];
  }

  hydrateReplyForm();
  setNoVncUrl(selectedSession?.novnc_url);
  setNoVncLabel(selectedSession);
}

function createEventItem(event) {
  const item = node('div', 'event-item');

  const top = node('div', 'event-top');
  top.append(
    node('strong', null, event.event_type || '-'),
    node('span', 'event-meta code', fmtDate(event.occurred_at)),
  );

  const eventId = node('div', 'event-meta');
  eventId.append('event_id: ', node('span', 'code', event.event_id || '-'));

  item.append(top, eventId, createJsonView(event.payload || {}));
  return item;
}

function streamSummary(event) {
  const payload = event.payload || {};
  const items = Array.isArray(payload.items) ? payload.items : [];
  const last = items.length ? items[items.length - 1] : {};
  const source = payload.source || 'stream';
  const stream = payload.stream || '-';

  if (source === 'browser') {
    const command = last.command || last.event || payload.message || 'browser';
    const status = last.ok === false ? 'failed' : last.ok === true ? 'ok' : '';
    const target = last.details?.selector || last.details?.url || last.result?.url || '';
    return [command, status, target].filter(Boolean).join(' · ');
  }

  return payload.message || last.message || last.type || last.line || `${source}/${stream}`;
}

function createStreamItem(event) {
  const payload = event.payload || {};
  const item = node('div', 'stream-item');

  const top = node('div', 'stream-top');
  top.append(
    node('strong', null, streamSummary(event)),
    node('span', 'stream-meta code', `${payload.source || '-'}:${payload.stream || '-'}`),
  );

  const metaNode = node('div', 'stream-meta');
  metaNode.append(
    `step ${payload.step || '-'} · seq ${payload.sequence || '-'} · `,
    node('span', 'code', fmtDate(event.occurred_at)),
  );

  item.append(top, metaNode, createJsonView(payload.items || []));
  return item;
}

function eventTypeFilterValues(events) {
  const actualTypes = events.map((event) => event.event_type).filter(Boolean);
  return [...new Set([...KNOWN_EVENT_TYPES, ...actualTypes])];
}

function filterEventsByType(events) {
  return events.filter((event) => selectedEventTypeFilters.has(event.event_type));
}

function ensureKnownEventTypeFilters(types) {
  for (const type of types) {
    if (!KNOWN_EVENT_TYPES.includes(type) && !selectedEventTypeFilters.has(type)) {
      selectedEventTypeFilters.add(type);
    }
  }
}

function renderEventTypeFilters(events) {
  const filterTypes = eventTypeFilterValues(events);
  ensureKnownEventTypeFilters(filterTypes);

  const counts = new Map();
  for (const event of events) {
    counts.set(event.event_type, (counts.get(event.event_type) || 0) + 1);
  }

  eventTypeFiltersNode.replaceChildren();
  for (const type of filterTypes) {
    const count = counts.get(type) || 0;
    const active = selectedEventTypeFilters.has(type);
    const button = node('button', `event-type-filter ${active ? 'active' : ''}`);
    button.type = 'button';
    button.textContent = `${type} · ${count}`;
    button.setAttribute('aria-pressed', String(active));
    button.addEventListener('click', () => {
      if (selectedEventTypeFilters.has(type)) {
        selectedEventTypeFilters.delete(type);
      } else {
        selectedEventTypeFilters.add(type);
      }
      renderEvents(selectedEvents);
    });
    eventTypeFiltersNode.append(button);
  }
}

function emptyEventsMessage(events) {
  if (!selectedSessionId) {
    return 'Выберите сессию.';
  }
  if (!events.length) {
    return 'Пока нет событий в сессии.';
  }
  return 'Нет событий выбранных типов.';
}

function renderEvents(events) {
  selectedEvents = events;
  seenEventIds = new Set(events.map((event) => event.event_id).filter(Boolean));
  const filteredEvents = filterEventsByType(events);
  eventsNode.replaceChildren();
  eventsCountNode.textContent = `${filteredEvents.length}/${events.length} EVENTS`;
  eventsEmptyNode.textContent = emptyEventsMessage(events);
  eventsEmptyNode.style.display = filteredEvents.length ? 'none' : 'block';
  renderEventTypeFilters(events);

  for (const event of filteredEvents) {
    eventsNode.appendChild(event.event_type === 'agent_stream_event' ? createStreamItem(event) : createEventItem(event));
  }

  setNoVncUrl(selectedSession?.novnc_url);
  setNoVncLabel(selectedSession);

  const last = events.length ? events[events.length - 1] : null;
  if (last && last.event_type === 'ask_user') {
    replyIdInput.value = last.payload?.reply_id || '';
  }
}

function connectEventStream() {
  if (eventSource) {
    return;
  }

  eventSource = new EventSource('/api/events/stream');
  eventSource.onopen = () => {
    if (!streamNeedsRecovery) {
      return;
    }
    streamNeedsRecovery = false;
    refreshAll().catch(showError);
  };
  eventSource.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      const hadSelectedSession = Boolean(selectedSessionId);
      if (event.session_id === selectedSessionId && !seenEventIds.has(event.event_id)) {
        selectedEvents = [...selectedEvents, event];
        seenEventIds.add(event.event_id);
        renderEvents(selectedEvents);
      }
      refreshSessions()
        .then(() => {
          if (!hadSelectedSession && selectedSessionId) {
            return refreshEvents();
          }
          return null;
        })
        .catch(showError);
    } catch (error) {
      showError(error);
    }
  };
  eventSource.onerror = () => {
    streamNeedsRecovery = true;
    statusNode.textContent = 'SSE reconnect';
  };
}

function closeEventStream() {
  if (eventSource) {
    eventSource.close();
  }
  eventSource = null;
}

function setReplyEmpty(message) {
  replyEmptyNode.replaceChildren();
  replyEmptyNode.append(message);
}

function renderAgentQuestion(session) {
  const hasQuestion = Boolean(session?.waiting_reply_id && session?.ask_question);
  agentQuestionNode.hidden = !hasQuestion;
  replyStateBadgeNode.hidden = !isWaitingSession(session || {});
  replyEmptyNode.style.display = hasQuestion ? 'none' : 'block';
  agentQuestionOptionsNode.replaceChildren();

  if (!session) {
    setReplyEmpty('Выберите сессию.');
    return;
  }

  if (!session.waiting_reply_id) {
    setReplyEmpty('Сессия не ждёт ответа. Появится после ');
    replyEmptyNode.append(node('span', 'code', 'ask_user'), '.');
    return;
  }

  if (!hasQuestion) {
    setReplyEmpty('Агент ждёт ответа без текста вопроса.');
    return;
  }

  agentQuestionTextNode.textContent = session.ask_question;
  agentQuestionTsNode.textContent = session.ask_asked_at ? fmtDate(session.ask_asked_at) : '';
  const options = Array.isArray(session.ask_options) ? session.ask_options : [];
  for (const option of options) {
    const chip = node('button', 'agent-option-chip', option);
    chip.type = 'button';
    chip.addEventListener('click', () => {
      replyMessageInput.value = option;
      replyMessageInput.focus();
    });
    agentQuestionOptionsNode.append(chip);
  }
}

function hydrateReplyForm() {
  replySessionIdInput.value = selectedSessionId || '';
  replyIdInput.value = selectedSession?.waiting_reply_id || '';
  renderAgentQuestion(selectedSession);
}

function showError(error) {
  statusNode.textContent = `Ошибка: ${error.message || error}`;
}

async function refreshSessions() {
  const sessions = await fetchJson('/api/sessions');
  renderSessions(sessions);

  if (sessions.length) {
    statusNode.textContent = `Сессий: ${sessions.length}. Активная: ${shortId(selectedSessionId || sessions[0].session_id)}`;
  } else {
    statusNode.textContent = 'Пока нет активных callback-событий.';
  }
}

async function refreshEvents() {
  if (!selectedSessionId) {
    selectedEvents = [];
    seenEventIds = new Set();
    eventsNode.replaceChildren();
    eventsCountNode.textContent = '0 EVENTS';
    eventsEmptyNode.textContent = 'Выберите сессию.';
    eventsEmptyNode.style.display = 'block';
    renderEventTypeFilters([]);
    return;
  }

  const events = await fetchJson(`/api/events?session_id=${encodeURIComponent(selectedSessionId)}`);
  renderEvents(events);
}

async function refreshAll() {
  await refreshSessions();
  await refreshEvents();
}

replyForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  replyResultNode.textContent = 'Отправка…';

  try {
    const payload = {
      session_id: replySessionIdInput.value.trim(),
      reply_id: replyIdInput.value.trim(),
      message: replyMessageInput.value.trim(),
    };

    const data = await fetchJson('/api/reply', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    replyResultNode.textContent = JSON.stringify(data, null, 2);
    replyMessageInput.value = '';
    await refreshAll();
  } catch (error) {
    replyResultNode.textContent = String(error.message || error);
    showError(error);
  }
});

taskForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  taskResultNode.textContent = 'Запуск…';

  try {
    const rawMetadata = taskMetadataInput.value.trim();
    const rawAuth = taskAuthInput.value.trim();
    let metadata = {};
    let auth = null;

    if (rawMetadata) {
      metadata = JSON.parse(rawMetadata);
      if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
        throw new Error('metadata должен быть JSON-объектом, например {"city":"Москва"}');
      }
    }

    if (rawAuth) {
      auth = JSON.parse(rawAuth);
      if (!auth || typeof auth !== 'object' || Array.isArray(auth)) {
        throw new Error('auth должен быть JSON-объектом, например {"provider":"sberid","storageState":{"cookies":[],"origins":[]}}');
      }
    }

    const payload = {
      task: taskTextInput.value.trim(),
      start_url: taskStartUrlInput.value.trim(),
      metadata,
      auth,
    };

    const data = await fetchJson('/api/tasks', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    taskResultNode.textContent = JSON.stringify(data, null, 2);
    pendingSessionSelectionId = data.session_id || null;
    if (data.novnc_url) {
      setNoVncUrl(data.novnc_url);
    }
    await refreshAll();
  } catch (error) {
    taskResultNode.textContent = String(error.message || error);
    showError(error);
  }
});

setupJsonEditors();
refreshAll()
  .then(() => connectEventStream())
  .catch(showError);
