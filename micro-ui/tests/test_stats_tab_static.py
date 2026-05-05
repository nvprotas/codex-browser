from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


def test_index_exposes_stats_tab_assets_and_panel() -> None:
    index_html = (BASE_DIR / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')

    assert 'id="tab-stats"' in index_html
    assert 'id="stats-tab-panel"' in index_html
    assert '/static/stats.css' in index_html
    assert '/static/stats.js' in index_html


def test_stats_js_uses_real_stats_api_not_handoff_mock_data() -> None:
    stats_js = (BASE_DIR / 'app' / 'static' / 'stats.js').read_text(encoding='utf-8')

    assert "STATS_SERVICE_PATH = '/api/eval/stats/sessions'" in stats_js
    assert 'generateMockSessions' not in stats_js
    assert 'window.MicroUiStats' in stats_js
