from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from json import JSONDecodeError
from pathlib import Path
from typing import Any


EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONTRACT = 2
DEFAULT_TIMEOUT_SEC = 3600
MANIFEST_SCHEMA_VERSION = "buyer-evolve-patch-manifest-v1"

Runner = Callable[..., subprocess.CompletedProcess[str]]
HeadReader = Callable[[Path], str]


class PatchCommandError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_CONTRACT) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    head_reader: HeadReader | None = None,
) -> int:
    del argv
    runtime_env = dict(os.environ if env is None else env)
    process_runner = runner or subprocess.run
    try:
        context = _load_context(runtime_env)
        read_head = head_reader or (lambda path: _git_head(path, runner=process_runner))
        base_head = read_head(context.worktree)
        prompt = build_prompt(context)
        _write_text(_prompt_log_path(context), prompt)
        completed = process_runner(
            _codex_command(runtime_env),
            cwd=context.worktree,
            env=_codex_process_env(runtime_env),
            capture_output=True,
            text=True,
            timeout=_timeout_sec(runtime_env),
            input=prompt,
        )
        _write_codex_logs(context, completed)
        if completed.returncode != 0:
            raise PatchCommandError(
                (
                    f"codex exec failed with exit code {completed.returncode}: {_tail(_combined_output(completed))}\n"
                    f"codex logs: {_log_paths_message(context)}"
                ),
                EXIT_RUNTIME,
            )
        candidate_head = read_head(context.worktree)
        if candidate_head == base_head:
            touched_paths = _git_worktree_changed_paths(context.worktree, runner=process_runner)
            patch_slug = _patch_slug_from_request(context)
        else:
            touched_paths = _git_changed_paths(context.worktree, base_head, candidate_head, runner=process_runner)
            patch_slug = _patch_slug_from_subject(_git_commit_subject(context.worktree, runner=process_runner))
        if not touched_paths:
            raise PatchCommandError("codex exec finished without candidate changes", EXIT_CONTRACT)
        _write_manifest(context, patch_slug=patch_slug, touched_paths=touched_paths)
        manifest = _read_json(context.manifest_path)
        _validate_manifest(manifest)
    except subprocess.TimeoutExpired as exc:
        if "context" in locals():
            _write_timeout_logs(context, exc)
            _print_error(f"codex exec timeout after {_timeout_sec(runtime_env)}s; codex logs: {_log_paths_message(context)}")
        else:
            _print_error(f"codex exec timeout after {_timeout_sec(runtime_env)}s")
        return EXIT_RUNTIME
    except (OSError, PatchCommandError, JSONDecodeError, FileNotFoundError) as exc:
        exit_code = exc.exit_code if isinstance(exc, PatchCommandError) else EXIT_CONTRACT
        if "context" in locals():
            _print_error(f"{exc}; codex logs: {_log_paths_message(context)}")
        else:
            _print_error(str(exc))
        return exit_code
    return EXIT_OK


class PatchContext:
    def __init__(
        self,
        *,
        worktree: Path,
        request_path: Path,
        manifest_path: Path,
        request: dict[str, Any],
        allowed_paths: list[str],
    ) -> None:
        self.worktree = worktree
        self.request_path = request_path
        self.manifest_path = manifest_path
        self.request = request
        self.allowed_paths = allowed_paths


def _load_context(env: Mapping[str, str]) -> PatchContext:
    worktree = _required_path(env, "EVOLVE_CANDIDATE_WORKTREE")
    request_path = _required_path(env, "EVOLVE_PATCH_REQUEST")
    manifest_path = _required_path(env, "EVOLVE_PATCH_MANIFEST")
    if not worktree.is_dir():
        raise PatchCommandError(f"candidate worktree is missing: {worktree}")
    request = _read_json(request_path)
    allowed_paths = _allowed_paths(env, request)
    return PatchContext(
        worktree=worktree,
        request_path=request_path,
        manifest_path=manifest_path,
        request=request,
        allowed_paths=allowed_paths,
    )


def build_prompt(context: PatchContext) -> str:
    recommendations = json.dumps(
        context.request.get("judge_recommendations", []),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    allowed_paths = "\n".join(f"- {path}" for path in context.allowed_paths) or "- <empty>"
    return f"""Ты вложенный Codex patch-worker для self-evolve цикла buyer.

Рабочая директория уже установлена в candidate worktree:
{context.worktree}

Контекст:
- EVOLVE_PATCH_REQUEST: {context.request_path}
- selected_case_ids: {context.request.get("selected_case_ids")}

Прочитай patch-request JSON, eval summary, judge recommendations и доступные артефакты прогона.
Проанализируй код, инструкции buyer-agent, логи/eval и внеси минимальное исправление, которое должно улучшить следующий прогон.
Главная цель evolve patch: ускорять buyer-agent и уменьшать число agent/browser шагов до достижения цели без снижения качества, safety, payment boundary и evidence.
Если baseline уже quality_ok/success_rate=1.0, предпочитай efficiency patch: убрать лишние действия, закрепить более короткий надежный путь, улучшить селекторы/инструкции для меньшего числа CDP-команд или токенов.
Не оптимизируй скорость ценой риска финальной оплаты, неверного товара, неверного размера, потери verifier evidence или ухудшения диагностики.

Judge recommendations:
{recommendations}

Разрешенные пути изменений:
{allowed_paths}

Жесткие правила:
- Не меняй `.env`, `.env.*`, auth/profile/storageState, `.tmp`, `eval/runs`, secrets и платежные токены.
- Не добавляй секреты, cookies, raw headers/body или payment URLs в код, docs или commit message.
- Для документации и комментариев используй русский язык.
- Если исправление касается Litres, сохраняй границу SberPay: buyer не должен совершать финальную покупку сохраненной картой; нужен PayEcom/SberPay boundary evidence.
- Не делай широких рефакторингов. Измени минимальный набор файлов.
- По возможности запусти точечные тесты или статическую проверку затронутого места.

Обязательный результат:
1. Измени файлы только в разрешенном scope.
2. Не запускай `git add`, `git commit`, `git reset`, `git checkout` или другие команды, которые пишут в `.git`.
3. Оставь изменения в рабочем дереве; внешний wrapper сам запишет manifest, а evolve-loop выполнит git validation и commit.

Manifest в `.tmp/evolve/**` пишет внешний wrapper, не вложенный Codex. Если не можешь создать безопасный patch, не делай пустой commit; заверши с ненулевым кодом.
"""


def _codex_command(env: Mapping[str, str]) -> list[str]:
    cmd = [
        env.get("EVOLVE_CODEX_BIN") or env.get("CODEX_BIN") or "codex",
        "exec",
        "--json",
        "-s",
        env.get("EVOLVE_CODEX_SANDBOX_MODE") or "workspace-write",
    ]
    model = env.get("EVOLVE_CODEX_MODEL") or env.get("CODEX_MODEL")
    if model:
        cmd.extend(["-m", model])
    reasoning_effort = env.get("EVOLVE_CODEX_REASONING_EFFORT")
    if reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    return cmd


def _codex_process_env(env: Mapping[str, str]) -> dict[str, str]:
    codex_env = dict(env)
    codex_env.pop("EVOLVE_PATCH_MANIFEST", None)
    return codex_env


def _git_head(worktree: Path, *, runner: Runner) -> str:
    completed = runner(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise PatchCommandError(f"git rev-parse failed: {_tail(_combined_output(completed))}")
    return completed.stdout.strip()


def _git_commit_subject(worktree: Path, *, runner: Runner) -> str:
    completed = runner(
        ["git", "log", "-1", "--format=%s", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise PatchCommandError(f"git log failed: {_tail(_combined_output(completed))}")
    return completed.stdout.strip()


def _git_changed_paths(worktree: Path, base_head: str, candidate_head: str, *, runner: Runner) -> list[str]:
    completed = runner(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", base_head, candidate_head],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise PatchCommandError(f"git diff-tree failed: {_tail(_combined_output(completed))}")
    return sorted(path for path in completed.stdout.splitlines() if path.strip())


def _git_worktree_changed_paths(worktree: Path, *, runner: Runner) -> list[str]:
    paths: set[str] = set()
    commands = (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--name-only", "--cached", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    for cmd in commands:
        completed = runner(
            cmd,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise PatchCommandError(f"{' '.join(cmd)} failed: {_tail(_combined_output(completed))}")
        paths.update(path for path in completed.stdout.splitlines() if path.strip())
    return sorted(paths)


def _patch_slug_from_subject(subject: str) -> str:
    prefix = "evolve buyer:"
    if subject.lower().startswith(prefix):
        slug = subject[len(prefix) :].strip()
        if slug:
            return slug
    return "external-codex-patch"


def _patch_slug_from_request(context: PatchContext) -> str:
    case_ids = context.request.get("selected_case_ids")
    if isinstance(case_ids, list) and case_ids:
        case_slug = _slugify(str(case_ids[0]))
        if case_slug:
            return f"{case_slug}-patch"
    return "external-codex-patch"


def _slugify(value: str) -> str:
    normalized = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            normalized.append(char)
            previous_dash = False
        elif not previous_dash:
            normalized.append("-")
            previous_dash = True
    return "".join(normalized).strip("-")


def _write_manifest(context: PatchContext, *, patch_slug: str, touched_paths: list[str]) -> None:
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "patch_slug": patch_slug,
        "patch_kind": "mixed",
        "touched_paths": touched_paths,
        "rationale": _manifest_rationale(context),
        "expected_improvement": _manifest_expected_improvement(context),
    }
    context.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    context.manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _manifest_rationale(context: PatchContext) -> str:
    recommendations = context.request.get("judge_recommendations")
    if isinstance(recommendations, list):
        rationale_parts = [
            str(item.get("rationale")).strip()
            for item in recommendations
            if isinstance(item, dict) and str(item.get("rationale") or "").strip()
        ]
        if rationale_parts:
            return " ".join(rationale_parts)
    return "Вложенный Codex применил candidate patch по результатам eval/judge recommendations."


def _manifest_expected_improvement(context: PatchContext) -> str:
    case_ids = context.request.get("selected_case_ids")
    if isinstance(case_ids, list) and case_ids:
        cases = ", ".join(str(case_id) for case_id in case_ids)
        return f"Следующий eval должен улучшить или сохранить качество для case IDs: {cases}."
    return "Следующий eval должен улучшить или сохранить качество выбранных buyer scenarios."


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise PatchCommandError("patch manifest has invalid schema_version")
    if not str(manifest.get("patch_slug") or "").strip():
        raise PatchCommandError("patch manifest is missing patch_slug")
    touched_paths = manifest.get("touched_paths")
    if not isinstance(touched_paths, list) or not any(isinstance(path, str) and path for path in touched_paths):
        raise PatchCommandError("patch manifest is missing touched_paths")


def _allowed_paths(env: Mapping[str, str], request: dict[str, Any]) -> list[str]:
    raw = env.get("EVOLVE_ALLOWED_PATHS")
    if raw:
        return [item for item in raw.split(os.pathsep) if item]
    request_allowed = request.get("allowed_paths")
    if isinstance(request_allowed, list):
        return [str(item) for item in request_allowed if isinstance(item, str) and item]
    return []


def _required_path(env: Mapping[str, str], name: str) -> Path:
    value = env.get(name)
    if not value:
        raise PatchCommandError(f"missing required env {name}")
    return Path(value)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PatchCommandError(f"JSON object expected: {path}")
    return payload


def _timeout_sec(env: Mapping[str, str]) -> int:
    try:
        return max(1, int(env.get("EVOLVE_CODEX_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def _combined_output(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (completed.stdout, completed.stderr) if part)


def _prompt_log_path(context: PatchContext) -> Path:
    return context.manifest_path.with_name("codex-exec.prompt.txt")


def _stdout_log_path(context: PatchContext) -> Path:
    return context.manifest_path.with_name("codex-exec.stdout.jsonl")


def _stderr_log_path(context: PatchContext) -> Path:
    return context.manifest_path.with_name("codex-exec.stderr.log")


def _log_paths_message(context: PatchContext) -> str:
    return (
        f"stdout={_stdout_log_path(context)}, "
        f"stderr={_stderr_log_path(context)}, "
        f"prompt={_prompt_log_path(context)}"
    )


def _write_codex_logs(context: PatchContext, completed: subprocess.CompletedProcess[str]) -> None:
    _write_text(_stdout_log_path(context), completed.stdout)
    _write_text(_stderr_log_path(context), completed.stderr)


def _write_timeout_logs(context: PatchContext, exc: subprocess.TimeoutExpired) -> None:
    _write_text(_stdout_log_path(context), _timeout_output(exc.stdout))
    _write_text(_stderr_log_path(context), _timeout_output(exc.stderr))


def _timeout_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def _print_error(message: str) -> None:
    print(f"propose_buyer_patch: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
