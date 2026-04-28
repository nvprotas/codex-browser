const layout = document.querySelector('.layout');
const pollMs = Number(layout?.dataset.pollMs || 3000);

const statusNode = document.getElementById('global-status');
const sessionsNode = document.getElementById('sessions-list');
const sessionsEmptyNode = document.getElementById('sessions-empty');
const sessionsCountNode = document.getElementById('sessions-count');
const eventsNode = document.getElementById('events-list');
const eventsEmptyNode = document.getElementById('events-empty');
const eventsCountNode = document.getElementById('events-count');
const noVncFrame = document.getElementById('novnc-frame');
const noVncSessionLabel = document.getElementById('novnc-session-label');
const noVncPlaceholderNode = document.getElementById('novnc-placeholder');

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

let selectedSessionId = null;
let selectedSession = null;
let pendingSessionSelectionId = null;
const noVncBlank = `<!doctype html>
<html lang="ru">
  <body style="margin:0;min-height:100vh;background:#0d0f12;"></body>
</html>`;

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
  noVncSessionLabel.textContent = session?.session_id || 'session.novnc_url';
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

function meta(label, value) {
  const item = node('div', 'session-meta');
  const labelNode = document.createTextNode(`${label}: `);
  const valueNode = node('span', 'code', value || '-');
  item.append(labelNode, valueNode);
  return item;
}

function statusClass(status) {
  const normalized = String(status || 'running').toLowerCase().replace(/[^a-z0-9_-]/g, '_');
  const known = new Set(['queued', 'running', 'waiting_user', 'failed', 'error', 'completed', 'success']);
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
  metricSessionsNode.textContent = String(sessions.length);
  metricWaitingNode.textContent = String(sessions.filter(isWaitingSession).length);
  metricOrdersNode.textContent = String(sessions.filter((item) => item.order_id).length);
  metricErrorsNode.textContent = String(sessions.filter(isErrorSession).length);
  sessionsCountNode.textContent = `${sessions.length} active`;
}

function createSessionItem(session, sessions) {
  const item = node('button', `session-item ${session.session_id === selectedSessionId ? 'active' : ''}`);
  item.type = 'button';

  const top = node('div', 'session-top');
  top.append(
    node('span', 'code', session.session_id),
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

function renderEvents(events) {
  eventsNode.replaceChildren();
  eventsCountNode.textContent = `${events.length} events`;
  eventsEmptyNode.textContent = selectedSessionId ? 'Пока нет событий в сессии.' : 'Выберите сессию.';
  eventsEmptyNode.style.display = events.length ? 'none' : 'block';

  for (const event of events) {
    const item = node('div', 'event-item');

    const top = node('div', 'event-top');
    top.append(
      node('strong', null, event.event_type || '-'),
      node('span', 'event-meta code', fmtDate(event.occurred_at)),
    );

    const eventId = node('div', 'event-meta');
    eventId.append('event_id: ', node('span', 'code', event.event_id || '-'));

    const payload = node('pre');
    payload.textContent = JSON.stringify(event.payload || {}, null, 2);

    item.append(top, eventId, payload);
    eventsNode.appendChild(item);
  }

  setNoVncUrl(selectedSession?.novnc_url);
  setNoVncLabel(selectedSession);

  const last = events.length ? events[events.length - 1] : null;
  if (last && last.event_type === 'ask_user') {
    replyIdInput.value = last.payload?.reply_id || '';
  }
}

function hydrateReplyForm() {
  replySessionIdInput.value = selectedSessionId || '';
  replyIdInput.value = selectedSession?.waiting_reply_id || '';
}

function showError(error) {
  statusNode.textContent = `Ошибка: ${error.message || error}`;
}

async function refreshSessions() {
  const sessions = await fetchJson('/api/sessions');
  renderSessions(sessions);

  if (sessions.length) {
    statusNode.textContent = `Сессий: ${sessions.length}. Активная: ${selectedSessionId || sessions[0].session_id}`;
  } else {
    statusNode.textContent = 'Пока нет активных callback-событий.';
  }
}

async function refreshEvents() {
  if (!selectedSessionId) {
    eventsNode.replaceChildren();
    eventsCountNode.textContent = '0 events';
    eventsEmptyNode.textContent = 'Выберите сессию.';
    eventsEmptyNode.style.display = 'block';
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

refreshAll().catch(showError);
setInterval(() => {
  refreshAll().catch(showError);
}, pollMs);
