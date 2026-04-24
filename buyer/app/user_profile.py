from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger('uvicorn.error')


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
