from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import EventEnvelope
from app.store import CallbackStore


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding='utf-8')


def _css_block(css: str, selector: str) -> str:
    start = css.index(selector)
    block_start = css.index('{', start)
    block_end = css.index('}', block_start)
    return css[block_start + 1:block_end]


def _ask_event() -> EventEnvelope:
    return EventEnvelope(
        event_id='evt-ask',
        session_id='session-ask',
        event_type='ask_user',
        occurred_at=datetime(2026, 4, 28, 11, 18, 12, tzinfo=timezone.utc),
        idempotency_key='session-ask:ask',
        payload={
            'reply_id': 'reply-1',
            'question': 'Подтвердите адрес доставки?',
            'options': ['Да', 'Другой адрес'],
        },
    )


def _session_event(
    *,
    event_id: str,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, object] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        session_id='session-ask',
        event_type=event_type,
        occurred_at=occurred_at,
        idempotency_key=f'session-ask:{event_id}',
        payload=payload or {},
    )


class MicroUiDesignHandoffStaticTests(unittest.TestCase):
    def test_template_uses_brand_assets_and_json_editor_shells(self) -> None:
        template = _read('app/templates/index.html')

        self.assertIn('/static/assets/favicon.svg', template)
        self.assertIn('/static/assets/logo-mark.svg', template)
        self.assertIn('class="brand-text"', template)
        self.assertIn('data-json-editor="task-metadata"', template)
        self.assertIn('data-json-editor="task-auth"', template)
        self.assertIn('id="agent-question"', template)

    def test_template_matches_reference_telemetry_layout_and_copy(self) -> None:
        template = _read('app/templates/index.html')

        self.assertIn('value="https://www.litres.ru/"', template)
        self.assertIn('{"city":"Москва","budget":2500}', template)
        self.assertIn('<h2>Ответить агенту</h2>', template)
        self.assertIn('id="reply-state-badge"', template)
        self.assertIn('<section class="telemetry-grid two">', template)
        self.assertIn('<section class="stream-row">', template)
        self.assertLess(template.index('class="panel events"'), template.index('class="stream-row"'))

    def test_css_contains_handoff_components(self) -> None:
        css = _read('app/static/app.css')

        self.assertIn('.brand img', css)
        self.assertIn('.agent-question', css)
        self.assertIn('.json-editor', css)
        self.assertIn('.json-view', css)
        self.assertIn('.stream-row', css)
        self.assertIn('.telemetry-grid.two', css)
        self.assertIn('.badge[hidden]', css)
        self.assertIn('  .telemetry-grid.two {\n    grid-template-columns: 1fr;\n  }', css)
        self.assertIn('  .event-top,\n  .stream-top {\n    display: grid;', css)

    def test_js_contains_json_highlighting_and_question_hydration(self) -> None:
        js = _read('app/static/app.js')

        self.assertIn('function tokenizeJson', js)
        self.assertIn('function renderAgentQuestion', js)
        self.assertIn('function createJsonView', js)
        self.assertIn('function formatMetric', js)
        self.assertIn('function shortId', js)
        self.assertIn('replyStateBadgeNode.hidden', js)
        self.assertIn('STREAM EVENTS', js)
        self.assertIn("new EventSource('/api/events/stream')", js)
        self.assertNotIn('setInterval(', js)

    def test_callback_and_stream_items_expand_without_inner_scroll(self) -> None:
        css = _read('app/static/app.css')
        js = _read('app/static/app.js')

        list_block = _css_block(css, '.events-list,\n.stream-list')
        json_view_block = _css_block(css, '.json-view')
        json_content_block = _css_block(css, '.json-view-content')

        self.assertNotIn('max-height', list_block)
        self.assertNotIn('overflow: auto', list_block)
        self.assertIn('overflow: visible', json_view_block)
        self.assertIn('white-space: pre-wrap', json_content_block)
        self.assertIn('overflow-wrap: anywhere', json_content_block)
        self.assertNotIn('.event-item pre,\n.stream-item pre {\n  max-width: 100%;\n  max-height: 300px;', css)
        self.assertNotIn('pre.style.maxHeight', js)
        self.assertIn('createJsonView(event.payload || {})', js)
        self.assertIn('createJsonView(payload.items || [])', js)


class CallbackStoreAskUserSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_sessions_exposes_ask_user_context(self) -> None:
        store = CallbackStore()

        accepted = await store.add(_ask_event())
        summaries = await store.list_sessions()

        self.assertTrue(accepted)
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.status, 'waiting_user')
        self.assertEqual(summary.waiting_reply_id, 'reply-1')
        self.assertEqual(summary.last_message, 'Подтвердите адрес доставки?')
        self.assertEqual(summary.ask_question, 'Подтвердите адрес доставки?')
        self.assertEqual(summary.ask_options, ['Да', 'Другой адрес'])
        self.assertEqual(summary.ask_asked_at, datetime(2026, 4, 28, 11, 18, 12, tzinfo=timezone.utc))

    async def test_list_sessions_prefers_message_over_legacy_question(self) -> None:
        store = CallbackStore()

        await store.add(
            _session_event(
                event_id='evt-ask-message',
                event_type='ask_user',
                occurred_at=datetime(2026, 4, 28, 11, 18, 12, tzinfo=timezone.utc),
                payload={
                    'reply_id': 'reply-1',
                    'message': 'Канонический вопрос из message',
                    'question': 'Legacy question',
                    'options': ['Да'],
                },
            )
        )
        summaries = await store.list_sessions()

        summary = summaries[0]
        self.assertEqual(summary.last_message, 'Канонический вопрос из message')
        self.assertEqual(summary.ask_question, 'Канонический вопрос из message')
        self.assertEqual(summary.ask_options, ['Да'])

    async def test_list_sessions_clears_waiting_context_on_agent_progression(self) -> None:
        store = CallbackStore()

        await store.add(_ask_event())
        await store.add(
            _session_event(
                event_id='evt-step-started',
                event_type='agent_step_started',
                occurred_at=datetime(2026, 4, 28, 11, 19, 12, tzinfo=timezone.utc),
                payload={'step': 2, 'message': 'Продолжаю сценарий.'},
            )
        )
        summaries = await store.list_sessions()

        summary = summaries[0]
        self.assertEqual(summary.status, 'running')
        self.assertIsNone(summary.waiting_reply_id)
        self.assertIsNone(summary.ask_question)
        self.assertEqual(summary.ask_options, [])
        self.assertIsNone(summary.ask_asked_at)
