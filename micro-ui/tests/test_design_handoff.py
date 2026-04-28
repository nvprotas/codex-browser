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


class MicroUiDesignHandoffStaticTests(unittest.TestCase):
    def test_template_uses_brand_assets_and_json_editor_shells(self) -> None:
        template = _read('app/templates/index.html')

        self.assertIn('/static/assets/favicon.svg', template)
        self.assertIn('/static/assets/logo-mark.svg', template)
        self.assertIn('class="brand-text"', template)
        self.assertIn('data-json-editor="task-metadata"', template)
        self.assertIn('data-json-editor="task-auth"', template)
        self.assertIn('id="agent-question"', template)

    def test_css_contains_handoff_components(self) -> None:
        css = _read('app/static/app.css')

        self.assertIn('.brand img', css)
        self.assertIn('.agent-question', css)
        self.assertIn('.json-editor', css)
        self.assertIn('.json-view', css)

    def test_js_contains_json_highlighting_and_question_hydration(self) -> None:
        js = _read('app/static/app.js')

        self.assertIn('function tokenizeJson', js)
        self.assertIn('function renderAgentQuestion', js)
        self.assertIn('function createJsonView', js)


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
