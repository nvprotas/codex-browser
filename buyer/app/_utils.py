from __future__ import annotations

import os
from datetime import datetime, timezone


def tail_text(text: str, limit: int = 500) -> str:
    compact = ' '.join(text.replace('\n', ' ').split())
    if len(compact) <= limit:
        return compact
    return compact[-limit:]


def head_text(text: str, limit: int = 500) -> str:
    compact = ' '.join(text.replace('\n', ' ').split())
    if len(compact) <= limit:
        return compact
    return f'{compact[:limit]}...'


def remove_file_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        return


def duration_ms_since(started_at: datetime) -> int:
    delta = datetime.now(timezone.utc) - started_at
    return max(int(delta.total_seconds() * 1000), 0)


def trace_date_dir_name(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime('%Y-%m-%d')


def trace_time_dir_name(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime('%H-%M-%S')
