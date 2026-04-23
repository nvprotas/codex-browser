const layout = document.querySelector('.layout');
const pollMs = Number(layout?.dataset.pollMs || 3000);

const statusNode = document.getElementById('global-status');
const sessionsNode = document.getElementById('sessions-list');
const sessionsEmptyNode = document.getElementById('sessions-empty');
const eventsNode = document.getElementById('events-list');
const eventsEmptyNode = document.getElementById('events-empty');
const noVncFrame = document.getElementById('novnc-frame');

const taskForm = document.getElementById('task-form');
const taskTextInput = document.getElementById('task-text');
const taskStartUrlInput = document.getElementById('task-start-url');
const taskMetadataInput = document.getElementById('task-metadata');
const taskResultNode = document.getElementById('task-result');

const replyForm = document.getElementById('reply-form');
const replySessionIdInput = document.getElementById('reply-session-id');
const replyIdInput = document.getElementById('reply-id');
const replyMessageInput = document.getElementById('reply-message');
const replyResultNode = document.getElementById('reply-result');

let selectedSessionId = null;
let selectedSession = null;
let pendingSessionSelectionId = null;

function normalizeUrl(url) {
  if (!url) {
    return '';
  }
  try {
    return new URL(url, window.location.origin).toString();
  } catch {
    return String(url);
  }
}

function setNoVncUrl(url) {
  if (!url) {
    return;
  }
  const next = normalizeUrl(url);
  const current = normalizeUrl(noVncFrame.getAttribute('src') || noVncFrame.src);
  if (current === next) {
    return;
  }
  noVncFrame.src = url;
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
    throw new Error(detail);
  }

  return body;
}

function fmtDate(value) {
  if (!value) {
    return '-';
  }
  const date = new Date(value);
  return date.toLocaleString('ru-RU');
}

function shortenText(value, maxLength = 220) {
  const text = String(value || '');
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
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

  sessionsNode.innerHTML = '';
  sessionsEmptyNode.style.display = sessions.length ? 'none' : 'block';

  for (const session of sessions) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = `session-item ${session.session_id === selectedSessionId ? 'active' : ''}`;
    item.innerHTML = `
      <div class="session-top">
        <span class="code">${session.session_id}</span>
        <span class="badge ${session.status || 'running'}">${session.status || 'running'}</span>
      </div>
      <div>Последнее событие: <strong>${session.last_event_type}</strong></div>
      <div class="session-message">${shortenText(session.last_message || 'Без сообщения')}</div>
      <div>reply_id: <span class="code">${session.waiting_reply_id || '-'}</span></div>
      <div>Обновлено: ${fmtDate(session.updated_at)}</div>
    `;
    item.addEventListener('click', () => {
      selectedSessionId = session.session_id;
      selectedSession = session;
      hydrateReplyForm();
      setNoVncUrl(session.novnc_url);
      renderSessions(sessions);
      refreshEvents().catch(showError);
    });
    sessionsNode.appendChild(item);
  }

  if (!selectedSessionId && sessions.length) {
    selectedSessionId = sessions[0].session_id;
    selectedSession = sessions[0];
    hydrateReplyForm();
    setNoVncUrl(selectedSession.novnc_url);
  }
}

function renderEvents(events) {
  eventsNode.innerHTML = '';
  eventsEmptyNode.style.display = events.length ? 'none' : 'block';

  for (const event of events) {
    const item = document.createElement('div');
    item.className = 'event-item';
    item.innerHTML = `
      <div class="event-top">
        <strong>${event.event_type}</strong>
        <span>${fmtDate(event.occurred_at)}</span>
      </div>
      <div class="code">event_id: ${event.event_id}</div>
      <pre>${JSON.stringify(event.payload, null, 2)}</pre>
    `;
    eventsNode.appendChild(item);
  }

  setNoVncUrl(selectedSession?.novnc_url);

  const last = events.at(-1);
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
    eventsNode.innerHTML = '';
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
  replyResultNode.textContent = 'Отправка...';

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
  taskResultNode.textContent = 'Запуск...';

  try {
    const rawMetadata = taskMetadataInput.value.trim();
    let metadata = {};
    if (rawMetadata) {
      metadata = JSON.parse(rawMetadata);
      if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
        throw new Error('metadata должен быть JSON-объектом, например {"city":"Москва"}');
      }
    }

    const payload = {
      task: taskTextInput.value.trim(),
      start_url: taskStartUrlInput.value.trim(),
      metadata,
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
