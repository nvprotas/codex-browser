from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from ._utils import trace_date_dir_name, trace_time_dir_name

TRACE_DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TRACE_TIME_DIR_RE = re.compile(r'^\d{2}-\d{2}-\d{2}$')


def resolve_trace_session_dir(*, trace_root: Path, session_id: str) -> tuple[str, str, Path]:
    trace_root = trace_root.expanduser()
    session_dir = find_existing_trace_session_dir(trace_root=trace_root, session_id=session_id)
    if session_dir is None:
        return build_new_trace_session_dir(trace_root=trace_root, session_id=session_id)
    return session_dir.parent.parent.name, session_dir.parent.name, session_dir


def find_existing_trace_session_dir(*, trace_root: Path, session_id: str) -> Path | None:
    trace_root = trace_root.expanduser()
    if not trace_root.is_dir():
        return None
    try:
        trace_root_resolved = trace_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    matches: list[Path] = []
    try:
        date_dirs = [
            item
            for item in trace_root.iterdir()
            if is_safe_existing_trace_dir(item, trace_root_resolved=trace_root_resolved, name_re=TRACE_DATE_DIR_RE)
        ]
    except OSError:
        return None
    for date_dir in date_dirs:
        try:
            time_dirs = [
                item
                for item in date_dir.iterdir()
                if is_safe_existing_trace_dir(item, trace_root_resolved=trace_root_resolved, name_re=TRACE_TIME_DIR_RE)
            ]
        except OSError:
            continue
        for time_dir in time_dirs:
            candidate = time_dir / session_id
            if is_safe_existing_trace_dir(candidate, trace_root_resolved=trace_root_resolved):
                matches.append(candidate)
    if not matches:
        return None
    return sorted(matches)[-1]


def build_new_trace_session_dir(*, trace_root: Path, session_id: str) -> tuple[str, str, Path]:
    trace_date = trace_date_dir_name()
    trace_time = trace_time_dir_name()
    for candidate_date, candidate_time in candidate_new_trace_dir_names(trace_date=trace_date, trace_time=trace_time):
        session_dir = trace_root / candidate_date / candidate_time / session_id
        try:
            ensure_new_trace_session_dir_is_safe(trace_root=trace_root, session_dir=session_dir)
        except ValueError:
            continue
        return candidate_date, candidate_time, session_dir
    raise ValueError('Не удалось подобрать безопасную директорию trace-сессии.')


def candidate_new_trace_dir_names(*, trace_date: str, trace_time: str) -> list[tuple[str, str]]:
    candidates = [(trace_date, trace_time)]
    try:
        base = datetime.strptime(f'{trace_date} {trace_time}', '%Y-%m-%d %H-%M-%S')
    except ValueError:
        return candidates
    for offset_seconds in range(1, 60):
        current = base + timedelta(seconds=offset_seconds)
        candidates.append((current.strftime('%Y-%m-%d'), current.strftime('%H-%M-%S')))
    return candidates


def ensure_new_trace_session_dir_is_safe(*, trace_root: Path, session_dir: Path) -> None:
    try:
        trace_root_resolved = trace_root.resolve(strict=False)
        session_dir_resolved = session_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError('Небезопасная директория trace-сессии.') from exc

    if not is_relative_to_path(session_dir_resolved, trace_root_resolved):
        raise ValueError('Директория trace-сессии должна находиться внутри trace_root.')

    try:
        relative_parts = session_dir.relative_to(trace_root).parts
    except ValueError as exc:
        raise ValueError('Директория trace-сессии должна находиться внутри trace_root.') from exc

    current = trace_root
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Директория trace-сессии не должна проходить через symlink.')


def is_safe_existing_trace_dir(
    path: Path,
    *,
    trace_root_resolved: Path,
    name_re: re.Pattern[str] | None = None,
) -> bool:
    if name_re is not None and not name_re.match(path.name):
        return False
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return is_relative_to_path(resolved, trace_root_resolved)


def is_relative_to_path(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
