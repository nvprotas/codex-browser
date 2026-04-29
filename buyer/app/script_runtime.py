from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._utils import tail_text


@dataclass(frozen=True)
class ScriptSpec:
    domain: str
    lifecycle: str
    relative_path: str


def registry_snapshot(registry: dict[str, ScriptSpec]) -> list[dict[str, str]]:
    return [
        {
            'domain': spec.domain,
            'lifecycle': spec.lifecycle,
            'script': spec.relative_path,
        }
        for spec in sorted(registry.values(), key=lambda item: item.domain)
    ]


def read_script_result_payload(output_path: Path, stdout_text: str) -> Any | None:
    for raw_payload in (_read_text_if_file(output_path), stdout_text):
        if not raw_payload:
            continue
        try:
            parsed = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        return parsed
    return None


def unique_script_output_path(session_dir: Path, stem: str) -> Path:
    return session_dir / f'{stem}-{uuid.uuid4().hex}.json'


def remove_script_output(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return


def script_stdio_artifacts(stdout_text: str, stderr_text: str) -> dict[str, str]:
    return {
        'stdout_tail': tail_text(stdout_text),
        'stderr_tail': tail_text(stderr_text),
    }


def _read_text_if_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
