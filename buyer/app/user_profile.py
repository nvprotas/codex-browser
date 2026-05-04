from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserProfileSnapshot:
    text: str | None
    truncated: bool = False


def load_user_profile(path: str, *, max_chars: int) -> UserProfileSnapshot:
    profile_path = Path(path).expanduser()

    try:
        raw_text = profile_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return UserProfileSnapshot(text=None)
    except OSError as exc:
        logger.warning('buyer_user_info_read_failed path=%s error=%s', profile_path, exc)
        return UserProfileSnapshot(text=None)

    text = raw_text.strip()
    if not text:
        return UserProfileSnapshot(text=None)

    if len(text) > max_chars:
        return UserProfileSnapshot(text=text[:max_chars].rstrip(), truncated=True)

    return UserProfileSnapshot(text=text)


def append_profile_updates(path: str, updates: list[str]) -> int:
    profile_path = Path(path).expanduser()
    normalized_updates = _normalize_profile_updates(updates)
    if not normalized_updates:
        return 0

    try:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        existing_text = profile_path.read_text(encoding='utf-8') if profile_path.is_file() else ''
    except OSError as exc:
        logger.warning('buyer_user_info_prepare_write_failed path=%s error=%s', profile_path, exc)
        return 0

    prefix = existing_text.rstrip('\n')
    appended_lines = [f'- {item}' for item in normalized_updates]
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    parts.extend(appended_lines)
    next_text = '\n'.join(parts) + '\n'

    try:
        profile_path.write_text(next_text, encoding='utf-8')
    except OSError as exc:
        logger.warning('buyer_user_info_write_failed path=%s error=%s', profile_path, exc)
        return 0

    return len(appended_lines)


def _normalize_profile_updates(updates: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in updates:
        text = ' '.join(str(raw).split()).strip()
        if not text:
            continue
        if text.startswith('- '):
            text = text[2:].strip()
        if text:
            normalized.append(text)
    return normalized
