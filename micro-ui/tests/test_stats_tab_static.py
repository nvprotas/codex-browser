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


def test_stats_gantt_uses_chronological_browser_action_timeline() -> None:
    stats_js = (BASE_DIR / 'app' / 'static' / 'stats.js').read_text(encoding='utf-8')

    assert 'command_timeline' in stats_js
    assert 'Шкала построена по timestamps из browser-actions JSONL' in stats_js
    assert 'step ${step.step} · LLM' not in stats_js
    assert 'LLM/Codex' not in stats_js


def test_stats_gantt_has_hover_highlighting_for_all_lanes() -> None:
    stats_js = (BASE_DIR / 'app' / 'static' / 'stats.js').read_text(encoding='utf-8')
    stats_css = (BASE_DIR / 'app' / 'static' / 'stats.css').read_text(encoding='utf-8')

    assert 'attachGanttHighlight' in stats_js
    assert 'dataset.ganttTypes' in stats_js
    assert 'is-highlighted' in stats_css
    assert 'stats-gantt-bar.highlighted' in stats_css
    for category in ['runtime', 'read', 'write', 'navigation', 'wait', 'evidence', 'heavy', 'error']:
        assert f"category: '{category}'" in stats_js


def test_stats_gantt_does_not_group_commands() -> None:
    stats_js = (BASE_DIR / 'app' / 'static' / 'stats.js').read_text(encoding='utf-8')

    assert 'commandTimelineLane' in stats_js
    assert 'packCommandTimelineLanes' not in stats_js
    assert "label: 'commands'" not in stats_js
    assert 'commands 2' not in stats_js
    assert "for (const type of ['read', 'write', 'navigation', 'wait', 'evidence', 'heavy'])" not in stats_js


def test_stats_filters_separate_source_and_status_groups() -> None:
    stats_js = (BASE_DIR / 'app' / 'static' / 'stats.js').read_text(encoding='utf-8')
    stats_css = (BASE_DIR / 'app' / 'static' / 'stats.css').read_text(encoding='utf-8')

    assert 'stats-filter-group source' in stats_js
    assert 'stats-filter-group status' in stats_js
    assert 'stats-filter-group-label' in stats_css
    assert 'Source' in stats_js
    assert 'Status' in stats_js
